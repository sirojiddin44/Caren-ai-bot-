from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetBusinessConnection, GetMe, SendMessage
from aiogram.types import BusinessConnection, Message, Update, User

from assistant.config import load_settings
from assistant.handlers import build_router
from assistant.service import Assistant, Outcome, Preview

OWNER_ID = 42
BOT_ID = 123456
BOT_TOKEN = f"{BOT_ID}:TEST-token"
START_TIME = 1_800_000_000.0

PROFILE = """<!-- template comment that must not reach Claude -->
# Men haqimda
- Ism: Aziz
- Shahar: Toshkent
"""


class RecordingSession(BaseSession):
    """Fake Telegram API: records every request, never touches the network."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[Any] = []
        self.connections: Dict[str, Dict[str, Any]] = {}
        self._message_ids = itertools.count(10_000)

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: Any, timeout: Optional[int] = None) -> Any:
        self.requests.append(method)
        if isinstance(method, GetBusinessConnection):
            payload = self.connections.get(method.business_connection_id)
            if payload is None:
                raise TelegramBadRequest(method=method, message="Bad Request: business connection not found")
            return BusinessConnection.model_validate(payload, context={"bot": bot})
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Assistant", username="test_assistant_bot")
        if isinstance(method, SendMessage):
            return Message.model_validate(
                {
                    "message_id": next(self._message_ids),
                    "date": int(START_TIME),
                    "chat": {"id": method.chat_id, "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Assistant"},
                    "text": method.text,
                    "business_connection_id": method.business_connection_id,
                },
                context={"bot": bot},
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise AssertionError("tests must not download anything")
        yield b""


class FakeGenerator:
    """Returns queued replies or raises queued exceptions; records every call."""

    def __init__(self) -> None:
        self.results: List[Any] = []
        self.default = "Yaxshi, rahmat!"
        self.calls: List[Dict[str, str]] = []
        self.during: Optional[Callable[[], Awaitable[None]]] = None

    def queue(self, *results: Any) -> None:
        self.results.extend(results)

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append({"system": system_prompt, "user": user_prompt})
        if self.during is not None:
            hook, self.during = self.during, None
            await hook()
        result = self.results.pop(0) if self.results else self.default
        if isinstance(result, BaseException):
            raise result
        return result


class Clock:
    def __init__(self, now: float = START_TIME) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def connection_payload(
    connection_id: str, user_id: int = OWNER_ID, *, enabled: bool = True, can_reply: bool = True
) -> Dict[str, Any]:
    return {
        "id": connection_id,
        "user": {"id": user_id, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": user_id,
        "date": int(START_TIME),
        "is_enabled": enabled,
        "can_reply": can_reply,  # older Bot API field, kept for older aiogram versions
        "rights": {"can_reply": can_reply},
    }


class Harness:
    def __init__(self, tmp_path: Path, overrides: Optional[Dict[str, str]] = None) -> None:
        self.tmp_path = tmp_path
        self.profile_path = tmp_path / "profile.md"
        if not self.profile_path.exists():
            self.profile_path.write_text(PROFILE, encoding="utf-8")
        values = {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "OWNER_ID": str(OWNER_ID),
            "DATA_DIR": str(tmp_path / "data"),
            "PROFILE_PATH": str(self.profile_path),
            "CLAUDE_BIN": str(tmp_path / "claude-is-never-run"),
        }
        values.update(overrides or {})
        self.settings = load_settings(tmp_path / ".env", source={}, overrides=values)
        self.session = RecordingSession()
        self.bot = Bot(BOT_TOKEN, session=self.session)
        self.generator = FakeGenerator()
        self.clock = Clock()
        self.previews: List[Preview] = []
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        self._build()

    def _build(self) -> None:
        self.assistant = Assistant(
            self.settings, self.generator, clock=self.clock, preview_sink=self.previews.append
        )
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.assistant))

    def restart(self) -> None:
        """A new Assistant over the same data folder, like a process restart."""
        self._build()

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    @property
    def sent(self) -> List[SendMessage]:
        return [r for r in self.session.requests if isinstance(r, SendMessage)]

    @property
    def lookups(self) -> List[GetBusinessConnection]:
        return [r for r in self.session.requests if isinstance(r, GetBusinessConnection)]

    @property
    def last_outcome(self) -> Optional[Outcome]:
        return self.assistant.outcomes[-1] if self.assistant.outcomes else None

    async def feed(self, kind: str, payload: Dict[str, Any]) -> None:
        update = Update.model_validate(
            {"update_id": next(self._update_ids), kind: payload}, context={"bot": self.bot}
        )
        await self.dp.feed_update(self.bot, update)

    def set_connection(self, connection_id: str = "conn-1", user_id: int = OWNER_ID, **kwargs: Any) -> None:
        """Change what Telegram answers to getBusinessConnection, without sending an update."""
        self.session.connections[connection_id] = connection_payload(connection_id, user_id, **kwargs)

    async def connect(self, connection_id: str = "conn-1", user_id: int = OWNER_ID, **kwargs: Any) -> None:
        self.set_connection(connection_id, user_id, **kwargs)
        await self.feed("business_connection", self.session.connections[connection_id])

    def message_payload(
        self,
        text: Optional[str] = "Salom, qalesan?",
        *,
        from_id: int = 777,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        connection_id: str = "conn-1",
        is_bot: bool = False,
        via_business_bot: bool = False,
        first_name: str = "Ali",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        chat_id = from_id if chat_id is None else chat_id
        payload: Dict[str, Any] = {
            "message_id": next(self._message_ids) if message_id is None else message_id,
            "date": int(self.clock.now),
            "chat": {"id": chat_id, "type": "private", "first_name": first_name},
            "from": {"id": from_id, "is_bot": is_bot, "first_name": first_name},
            "business_connection_id": connection_id,
        }
        if text is not None:
            payload["text"] = text
        if via_business_bot:
            payload["sender_business_bot"] = {"id": BOT_ID, "is_bot": True, "first_name": "Assistant"}
        payload.update(extra or {})
        return payload

    async def business_message(self, text: Optional[str] = "Salom, qalesan?", **kwargs: Any) -> Optional[Outcome]:
        before = len(self.assistant.outcomes)
        await self.feed("business_message", self.message_payload(text, **kwargs))
        return self.assistant.outcomes[-1] if len(self.assistant.outcomes) > before else None

    async def direct_message(self, text: str, *, from_id: int = OWNER_ID) -> None:
        payload: Dict[str, Any] = {
            "message_id": next(self._message_ids),
            "date": int(self.clock.now),
            "chat": {"id": from_id, "type": "private", "first_name": "Someone"},
            "from": {"id": from_id, "is_bot": False, "first_name": "Someone"},
            "text": text,
        }
        if text.startswith("/"):
            payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.feed("message", payload)


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Harness]:
    def factory(**overrides: str) -> Harness:
        return Harness(tmp_path, overrides)

    return factory
