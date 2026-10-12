"""Core logic: decide whether to answer a business message, draft the reply, preview or send it."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message

from .claude_cli import ClaudeCLI, ClaudeError, ClaudeLimitError
from .config import Settings
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import HistoryStore, RuntimeState, SeenMessages, ensure_private_dir

log = logging.getLogger(__name__)

TELEGRAM_CHUNK = 4000
OWNER_ALERT_INTERVAL = 600  # seconds between "Claude failed" messages to the owner


class Outcome(str, Enum):
    PREVIEWED = "previewed"
    SENT = "sent"
    DUPLICATE = "duplicate"
    IGNORED_BOT = "ignored_bot"
    NO_CONNECTION = "no_connection"
    FOREIGN_CONNECTION = "foreign_connection"
    CONNECTION_DISABLED = "connection_disabled"
    UNSUPPORTED = "unsupported"
    OWNER_MESSAGE = "owner_message"
    OWNER_ACTIVE = "owner_active"
    PAUSED = "paused"
    LIMITED = "limited"
    SKIPPED = "skipped"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"


@dataclass
class Preview:
    connection_id: str
    chat_id: int
    chat_name: str
    language: Optional[str]
    incoming: str
    reply: str
    note: str = ""


def print_preview(preview: Preview) -> None:
    lines = [
        "",
        "=" * 20 + " PREVIEW, not sent " + "=" * 20,
        f"Chat:     {preview.chat_name} (chat {preview.chat_id}, connection {preview.connection_id})",
        f"Language: {preview.language or 'unknown'}",
        f"They:     {preview.incoming}",
        f"Reply:    {preview.reply}",
    ]
    if preview.note:
        lines.append(f"Note:     {preview.note}")
    lines.append("=" * 59)
    log.info("\n".join(lines))


class ReplyGenerator(Protocol):
    async def generate(self, system_prompt: str, user_prompt: str) -> str: ...


def describe(message: Message) -> Optional[str]:
    """Text for the prompt: the message text, or a short label for media. None for service messages."""
    if message.text:
        return message.text
    caption = message.caption or ""

    def label(kind: str, extra: str = "") -> str:
        return " ".join(part for part in (f"[{kind}]", extra.strip()) if part)

    if message.photo:
        return label("photo", caption)
    if message.video:
        return label("video", caption)
    if message.animation:
        return label("GIF", caption)
    if message.voice:
        return label("voice message", caption)
    if message.video_note:
        return label("video message")
    if message.audio:
        title = " - ".join(p for p in (message.audio.performer, message.audio.title) if p)
        return label("audio", " ".join(p for p in (title, caption) if p))
    if message.document:
        return label("file", " ".join(p for p in (message.document.file_name or "", caption) if p))
    if message.sticker:
        return label("sticker", message.sticker.emoji or "")
    if message.dice:
        return label("dice", message.dice.emoji)
    if message.contact:
        return label("contact", message.contact.first_name)
    if message.venue:
        return label("place", message.venue.title)
    if message.location:
        return label("location")
    if message.poll:
        return label("poll", message.poll.question)
    if message.story:
        return label("story")
    if message.paid_media:
        return label("paid media", caption)
    return None


def can_reply(connection: BusinessConnection) -> bool:
    rights = getattr(connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def read_profile(path: os.PathLike) -> str:
    """Read profile.md fresh for every reply, without <!-- comments -->."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _COMMENT_RE.sub("", text).strip()


def profile_is_filled(profile: str) -> bool:
    """True if the profile has any content beyond the template's headings and empty labels."""
    for line in profile.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"[-*]\s*[^:]*:\s*", line):
            continue
        return True
    return False


def _chunks(text: str, size: int = TELEGRAM_CHUNK):
    for start in range(0, len(text), size):
        yield text[start : start + size]


def _format_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


class Assistant:
    def __init__(
        self,
        settings: Settings,
        generator: ReplyGenerator,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], Any] = print_preview,
    ) -> None:
        self.settings = settings
        self.generator = generator
        self.clock = clock
        self.preview_sink = preview_sink
        data_dir = ensure_private_dir(settings.data_dir)
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState(data_dir / "state.json")
        self.previews_path = data_dir / "previews.jsonl"
        self.connections: Dict[str, BusinessConnection] = {}
        self.outcomes: Deque[Outcome] = deque(maxlen=100)
        self.takeover_until: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_owner_alert: Optional[float] = None

    # ---- incoming updates -------------------------------------------------

    def on_business_connection(self, connection: BusinessConnection) -> None:
        self.connections[connection.id] = connection
        if connection.user.id != self.settings.owner_id:
            log.warning(
                "Business connection %s belongs to user %s, not OWNER_ID; its messages are ignored",
                connection.id,
                connection.user.id,
            )
            return
        log.info(
            "Business connection %s: user=%s enabled=%s can_reply=%s",
            connection.id,
            connection.user.id,
            connection.is_enabled,
            can_reply(connection),
        )

    async def on_business_message(self, bot: Bot, message: Message) -> Outcome:
        outcome = await self._handle(bot, message)
        self.outcomes.append(outcome)
        return outcome

    async def _handle(self, bot: Bot, message: Message) -> Outcome:
        connection_id = message.business_connection_id
        if not connection_id:
            return Outcome.NO_CONNECTION
        chat_id = message.chat.id
        # Mark as seen before any await, so a parallel copy of the update is a duplicate.
        if not self.seen.first_time(f"{connection_id}:{chat_id}:{message.message_id}"):
            return Outcome.DUPLICATE
        sender = message.from_user
        if message.sender_business_bot is not None or (sender is not None and sender.is_bot):
            return Outcome.IGNORED_BOT

        connection = self.connections.get(connection_id)
        if connection is None:
            connection = await self._fetch_connection(bot, connection_id)
            if connection is None:
                return Outcome.NO_CONNECTION
            self.connections[connection_id] = connection
        if connection.user.id != self.settings.owner_id:
            return Outcome.FOREIGN_CONNECTION
        if not connection.is_enabled:
            return Outcome.CONNECTION_DISABLED

        text = describe(message)
        if text is None:
            return Outcome.UNSUPPORTED

        chat_key = HistoryStore.key(connection_id, chat_id)
        now = self.clock()
        if sender is not None and sender.id == self.settings.owner_id:
            self.history.add(connection_id, chat_id, role="owner", name="You", text=text, ts=now)
            if self.settings.owner_takeover_minutes > 0:
                self.takeover_until[chat_key] = now + self.settings.owner_takeover_minutes * 60
            return Outcome.OWNER_MESSAGE

        language = detect_language(text) or self.history.last_language(connection_id, chat_id)
        name = (sender.full_name if sender else "") or message.chat.full_name or "Them"
        self.history.add(
            connection_id, chat_id, role="other", name=name, text=text, lang=language, ts=now
        )

        lock = self._locks.setdefault(chat_key, asyncio.Lock())
        async with lock:
            blocked = self._blocked(chat_key)
            if blocked is not None:
                return blocked

            system_prompt = build_system_prompt(
                read_profile(self.settings.profile_path), datetime.fromtimestamp(self.clock())
            )
            user_prompt = build_user_prompt(self.history.get(connection_id, chat_id), language)
            try:
                reply = await self.generator.generate(system_prompt, user_prompt)
            except ClaudeLimitError as exc:
                return await self._on_limit(bot, exc)
            except ClaudeError as exc:
                log.error("Claude failed for chat %s: %s", chat_id, exc)
                await self._alert_owner(bot, f"Claude failed, nothing was sent to the chat: {exc}")
                return Outcome.ERROR
            except Exception:
                log.exception("Unexpected error while drafting a reply for chat %s", chat_id)
                return Outcome.ERROR

            reply = (reply or "").strip()
            if not reply or reply.startswith(SKIP_TOKEN):
                log.info("Claude chose not to reply in chat %s", chat_id)
                return Outcome.SKIPPED

            preview = Preview(connection_id, chat_id, name, language, text, reply)
            if not self.settings.auto_send:
                self._show_preview(preview)
                return Outcome.PREVIEWED

            blocked = self._blocked(chat_key)
            if blocked is not None:
                reasons = {
                    Outcome.OWNER_ACTIVE: "you wrote in this chat while the reply was being drafted",
                    Outcome.PAUSED: "the bot was paused while the reply was being drafted",
                    Outcome.LIMITED: "the Claude usage limit was hit while the reply was being drafted",
                }
                preview.note = f"not sent: {reasons[blocked]}"
                self._show_preview(preview)
                return blocked

            problem = await self._check_send_permission(bot, connection_id)
            if problem:
                preview.note = f"not sent: {problem}"
                self._show_preview(preview)
                return Outcome.PERMISSION_DENIED

            try:
                for part in _chunks(reply):
                    await bot.send_message(chat_id, part, business_connection_id=connection_id)
            except TelegramAPIError as exc:
                log.error("Telegram refused to send the reply to chat %s: %s", chat_id, exc)
                return Outcome.ERROR
            self.history.add(
                connection_id, chat_id, role="owner", name="You", text=reply, ts=self.clock()
            )
            log.info("Sent a reply to %s (chat %s)", name, chat_id)
            return Outcome.SENT

    # ---- helpers ----------------------------------------------------------

    async def _fetch_connection(self, bot: Bot, connection_id: str) -> Optional[BusinessConnection]:
        try:
            return await bot.get_business_connection(business_connection_id=connection_id)
        except TelegramAPIError as exc:
            log.warning("Could not look up business connection %s: %s", connection_id, exc)
            return None

    async def _check_send_permission(self, bot: Bot, connection_id: str) -> Optional[str]:
        """Ask Telegram again (not the cache) right before sending."""
        connection = await self._fetch_connection(bot, connection_id)
        if connection is None:
            return "could not re-check the business connection with Telegram"
        self.connections[connection_id] = connection
        if connection.user.id != self.settings.owner_id:
            return "the business connection belongs to another account"
        if not connection.is_enabled:
            return "the business connection is disabled"
        if not can_reply(connection):
            return "the bot has no permission to reply (Telegram Business -> Chatbots)"
        return None

    def _blocked(self, chat_key: str) -> Optional[Outcome]:
        now = self.clock()
        if self.state.paused:
            return Outcome.PAUSED
        limit_until = self.state.limit_until
        if limit_until is not None:
            if limit_until > now:
                return Outcome.LIMITED
            self.state.limit_until = None
            log.info("Claude usage limit period is over, replying again")
        if self.takeover_until.get(chat_key, 0) > now:
            return Outcome.OWNER_ACTIVE
        return None

    async def _on_limit(self, bot: Bot, exc: ClaudeLimitError) -> Outcome:
        now = self.clock()
        if exc.resets_at and exc.resets_at > now:
            until = exc.resets_at
        else:
            until = now + self.settings.limit_cooldown_minutes * 60
        self.state.limit_until = until
        log.warning(
            "%s. No replies until %s. CLI said: %s", exc, _format_time(until), exc.detail or "-"
        )
        if self.settings.auto_send:
            await self._notify_owner(
                bot,
                f"{exc}. The bot stays silent until {_format_time(until)}. "
                "Send /resume to try earlier.",
            )
        return Outcome.LIMITED

    async def _alert_owner(self, bot: Bot, text: str) -> None:
        if not self.settings.auto_send:
            return
        now = self.clock()
        if self._last_owner_alert is not None and now - self._last_owner_alert < OWNER_ALERT_INTERVAL:
            return
        self._last_owner_alert = now
        await self._notify_owner(bot, text)

    async def _notify_owner(self, bot: Bot, text: str) -> None:
        try:
            await bot.send_message(self.settings.owner_id, text[:TELEGRAM_CHUNK])
        except TelegramAPIError as exc:
            log.warning("Could not message the owner (send /start to the bot first): %s", exc)

    def _show_preview(self, preview: Preview) -> None:
        self.preview_sink(preview)
        record = dict(asdict(preview), ts=self.clock())
        ensure_private_dir(self.previews_path.parent)
        fd = os.open(self.previews_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.chmod(self.previews_path, 0o600)

    # ---- owner controls ---------------------------------------------------

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False
        self.state.limit_until = None
        self.takeover_until.clear()

    def _mode_line(self) -> str:
        if self.settings.auto_send:
            return "AUTO-SEND (replies are sent from your account)"
        return "PREVIEW (replies are only shown in the logs)"

    def status_text(self) -> str:
        limit_until = self.state.limit_until
        if limit_until is not None and limit_until > self.clock():
            limit = f"reached, silent until {_format_time(limit_until)}"
        else:
            limit = "ok"
        return "\n".join(
            [
                f"Mode: {self._mode_line()}",
                f"State: {'paused' if self.state.paused else 'running'}",
                f"Claude usage limit: {limit}",
                f"Chats in history: {self.history.chat_count()}",
            ]
        )

    def start_text(self, bot_username: str = "") -> str:
        username = f"@{bot_username}" if bot_username else "shu botning username'ini"
        if self.settings.auto_send:
            mode = "AUTO-SEND — javoblar sizning nomingizdan yuboriladi"
        else:
            mode = "PREVIEW — javoblar faqat logda ko'rsatiladi, hech kimga yuborilmaydi"
        state = "pauzada (/resume bilan yoqing)" if self.state.paused else "ishlayapti"
        minutes = self.settings.owner_takeover_minutes
        if minutes > 0:
            takeover = f"• Chatda o'zingiz yozsangiz, bot o'sha chatda {minutes} daqiqa jim turadi."
        else:
            takeover = "• O'zingiz yozganingizda ham bot jim turmaydi (OWNER_TAKEOVER_MINUTES=0)."
        lines = [
            "Assalomu alaykum! Men sizning shaxsiy yordamchingizman.",
            "Sizga Telegram'da kimdir yozsa, profile.md asosida sizning uslubingizda javob tayyorlayman.",
            "",
            f"Rejim: {mode}",
            f"Holat: {state}",
            "",
            "Qanday ulanadi:",
            "1. Telegram → Settings → Telegram Business → Chatbots (Telegram Premium kerak).",
            f"2. {username} ni kiriting.",
            "3. Bot qaysi chatlarni ko'rishini o'sha yerda tanlang.",
            "4. Javob berish ruxsatini (reply to messages) yoqing.",
            "",
            "Buyruqlar:",
            "/pause — javob berishni to'xtatish",
            "/resume — qayta yoqish (limit va jimlik taymerlarini ham tozalaydi)",
            "/status — hozirgi holat",
            "",
            "Bilib qo'ying:",
            takeover,
            "• Pul, qarz, uchrashuv va rejalarga rozilik bermaydi, va'da qilmaydi.",
            "• profile.md ni to'ldirishingiz kerak: bot faqat undagi ma'lumotlarni biladi.",
        ]
        if not self.settings.auto_send:
            lines.append(
                "• Hozir PREVIEW rejimi: javoblar yaxshi bo'lsa, AUTO_SEND=true qiling."
            )
        return "\n".join(lines)


def make_claude_cli(settings: Settings) -> ClaudeCLI:
    return ClaudeCLI(
        settings.claude_bin,
        workdir=settings.claude_workdir,
        model=settings.claude_model,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout,
        oauth_token=settings.claude_oauth_token,
        auth_label=settings.claude_auth_mode,
    )


def build_assistant(settings: Settings, **kwargs: Any) -> Assistant:
    return Assistant(settings, make_claude_cli(settings), **kwargs)
