from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest
from aiogram.methods import GetBusinessConnection, SendMessage

from assistant.claude_cli import ClaudeAuthError, ClaudeLimitError, ClaudeTimeout
from assistant.handlers import STRANGER_START_TEXT
from assistant.service import Outcome, describe

from .conftest import OWNER_ID, START_TIME

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


# ---- preview mode -------------------------------------------------------------


async def test_preview_shows_reply_and_sends_nothing(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue("Yaxshi, o'zing-chi?")

    assert await h.business_message("Salom, qalesan?") == Outcome.PREVIEWED

    assert h.session.requests == []
    assert len(h.previews) == 1
    preview = h.previews[0]
    assert (preview.chat_id, preview.incoming, preview.reply) == (777, "Salom, qalesan?", "Yaxshi, o'zing-chi?")
    assert preview.language == "uz"

    lines = (h.data_dir / "previews.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["reply"] == "Yaxshi, o'zing-chi?"
    # An unsent reply never becomes part of the history.
    assert [item["role"] for item in h.assistant.history.get("conn-1", 777)] == ["other"]


@posix_only
async def test_preview_file_is_private(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message()
    assert stat.S_IMODE(os.stat(h.data_dir / "previews.jsonl").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(h.data_dir).st_mode) == 0o700


async def test_unknown_connection_is_looked_up_once_then_cached(make_harness):
    h = make_harness()
    h.set_connection("conn-1")

    assert await h.business_message("Hello") == Outcome.PREVIEWED
    assert await h.business_message("Are you there?") == Outcome.PREVIEWED

    assert len(h.lookups) == 1
    assert h.lookups[0].business_connection_id == "conn-1"
    assert h.sent == []


async def test_failed_lookup_is_no_connection(make_harness):
    h = make_harness()
    assert await h.business_message("Hello", connection_id="missing") == Outcome.NO_CONNECTION
    assert h.generator.calls == []


async def test_foreign_and_disabled_connections_never_reach_claude(make_harness):
    h = make_harness()
    await h.connect("foreign", user_id=999)
    await h.connect("off", enabled=False)

    assert await h.business_message("Hi", connection_id="foreign") == Outcome.FOREIGN_CONNECTION
    assert await h.business_message("Hi", connection_id="off") == Outcome.CONNECTION_DISABLED
    assert h.generator.calls == []
    assert h.session.requests == []


async def test_any_chat_gets_a_reply_without_allowlist(make_harness):
    h = make_harness()
    await h.connect()
    for chat_id in (1, 555, 10**12):
        assert await h.business_message("Hi there", from_id=chat_id) == Outcome.PREVIEWED
    assert len(h.generator.calls) == 3


# ---- owner messages and takeover --------------------------------------------


async def test_owner_message_is_recorded_and_never_answered(make_harness):
    h = make_harness()
    await h.connect()

    outcome = await h.business_message("Kechqurun gaplashamiz", from_id=OWNER_ID, chat_id=777)

    assert outcome == Outcome.OWNER_MESSAGE
    assert h.generator.calls == []
    assert h.assistant.history.get("conn-1", 777)[0]["role"] == "owner"


async def test_bot_stays_quiet_while_owner_is_active_then_answers_again(make_harness):
    h = make_harness(OWNER_TAKEOVER_MINUTES="15")
    await h.connect()
    await h.business_message("Men hozir yozaman", from_id=OWNER_ID, chat_id=777)

    assert await h.business_message("Ok, kutaman", from_id=777) == Outcome.OWNER_ACTIVE
    # Another chat is not affected.
    assert await h.business_message("Salom", from_id=888) == Outcome.PREVIEWED

    h.clock.advance(15 * 60 + 1)
    assert await h.business_message("Hali ham bormisan?", from_id=777) == Outcome.PREVIEWED
    prompt = h.generator.calls[-1]["user"]
    assert "[You]: Men hozir yozaman" in prompt
    assert "[Ali]: Ok, kutaman" in prompt


async def test_takeover_can_be_disabled(make_harness):
    h = make_harness(OWNER_TAKEOVER_MINUTES="0")
    await h.connect()
    await h.business_message("Salom", from_id=OWNER_ID, chat_id=777)
    assert await h.business_message("Salom!", from_id=777) == Outcome.PREVIEWED


# ---- bots, duplicates, unsupported --------------------------------------------


async def test_messages_from_bots_are_ignored(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Auto reply", via_business_bot=True, from_id=OWNER_ID, chat_id=777) == Outcome.IGNORED_BOT
    assert await h.business_message("I am a bot", is_bot=True, from_id=31337) == Outcome.IGNORED_BOT
    assert h.generator.calls == []


async def test_duplicates_in_parallel_and_in_sequence(make_harness):
    h = make_harness()
    await h.connect()
    payload = h.message_payload("Hello", message_id=5)

    await asyncio.gather(h.feed("business_message", payload), h.feed("business_message", payload))
    await h.feed("business_message", payload)

    assert sorted(h.assistant.outcomes) == sorted([Outcome.PREVIEWED, Outcome.DUPLICATE, Outcome.DUPLICATE])
    assert len(h.generator.calls) == 1


async def test_duplicates_are_caught_after_restart(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Hello", message_id=9) == Outcome.PREVIEWED
    h.restart()
    await h.connect()
    assert await h.business_message("Hello", message_id=9) == Outcome.DUPLICATE
    assert len(h.generator.calls) == 1


async def test_service_message_is_unsupported(make_harness):
    h = make_harness()
    await h.connect()
    extra = {"new_chat_title": "New title"}
    assert await h.business_message(None, extra=extra) == Outcome.UNSUPPORTED
    assert h.generator.calls == []


async def test_sticker_and_voice_are_described(make_harness):
    h = make_harness()
    await h.connect()
    sticker = {
        "sticker": {
            "file_id": "s1", "file_unique_id": "u1", "type": "regular",
            "width": 512, "height": 512, "is_animated": False, "is_video": False, "emoji": "😀",
        }
    }
    voice = {"voice": {"file_id": "v1", "file_unique_id": "u2", "duration": 3}}

    await h.business_message(None, extra=sticker)
    await h.business_message(None, extra=voice)

    texts = [item["text"] for item in h.assistant.history.get("conn-1", 777)]
    assert texts == ["[sticker] 😀", "[voice message]"]


def test_describe_photo_with_caption():
    from aiogram.types import Message

    message = Message.model_validate(
        {
            "message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
            "photo": [{"file_id": "p", "file_unique_id": "p", "width": 1, "height": 1}],
            "caption": "look",
        }
    )
    assert describe(message) == "[photo] look"


# ---- history and language -----------------------------------------------------


async def test_history_is_separate_per_connection_and_chat(make_harness):
    h = make_harness()
    await h.connect("conn-1")
    await h.connect("conn-2")

    await h.business_message("secret for chat A", from_id=1)
    await h.business_message("message in chat B", from_id=2)
    await h.business_message("same chat id, other connection", from_id=1, connection_id="conn-2")

    prompt_b = h.generator.calls[1]["user"]
    prompt_other_conn = h.generator.calls[2]["user"]
    assert "secret for chat A" not in prompt_b
    assert "secret for chat A" not in prompt_other_conn
    assert len(h.assistant.history.get("conn-1", 1)) == 1
    assert len(h.assistant.history.get("conn-2", 1)) == 1


async def test_language_hint_and_emoji_fallback(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Привет, как дела?", from_id=1)
    await h.business_message("Hi, are you free tonight?", from_id=2)
    await h.business_message("Bugun bo'shmisan?", from_id=3)
    await h.business_message("👍", from_id=1)

    hints = [call["user"].rsplit("\n", 1)[-1] for call in h.generator.calls]
    assert "Russian" in hints[0]
    assert "English" in hints[1]
    assert "Uzbek" in hints[2]
    assert "Russian" in hints[3]  # emoji: falls back to the chat's last language


async def test_profile_reaches_system_prompt_without_comments(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message()
    system = h.generator.calls[0]["system"]
    assert "Ism: Aziz" in system
    assert "template comment" not in system


async def test_skip_is_not_shown(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue("[SKIP]")
    assert await h.business_message("ok") == Outcome.SKIPPED
    assert h.previews == []
    assert not (h.data_dir / "previews.jsonl").exists()


# ---- auto-send ----------------------------------------------------------------


async def test_auto_send_rechecks_connection_then_sends(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue("Yaxshi, rahmat!")

    assert await h.business_message("Salom, qalesan?") == Outcome.SENT

    assert [type(r) for r in h.session.requests] == [GetBusinessConnection, SendMessage]
    sent = h.sent[0]
    assert (sent.chat_id, sent.text, sent.business_connection_id) == (777, "Yaxshi, rahmat!", "conn-1")
    history = h.assistant.history.get("conn-1", 777)
    assert [(i["role"], i["text"]) for i in history] == [("other", "Salom, qalesan?"), ("owner", "Yaxshi, rahmat!")]
    assert h.previews == []


async def test_auto_send_splits_long_replies(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue("a" * 9000)

    assert await h.business_message("Tell me everything") == Outcome.SENT
    assert [len(m.text) for m in h.sent] == [4000, 4000, 1000]
    assert all(m.business_connection_id == "conn-1" for m in h.sent)


@pytest.mark.parametrize("change", [{"can_reply": False}, {"enabled": False}])
async def test_auto_send_respects_current_rights(make_harness, change):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.set_connection("conn-1", **change)  # changed in Telegram, the cache still says ok

    assert await h.business_message("Hello") == Outcome.PERMISSION_DENIED
    assert h.sent == []
    assert h.previews and h.previews[0].note.startswith("not sent")


async def test_auto_send_holds_back_when_owner_writes_during_generation(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()

    async def owner_writes():
        await h.business_message("Men o'zim javob beraman", from_id=OWNER_ID, chat_id=777)

    h.generator.during = owner_writes
    assert await h.business_message("Salom") == Outcome.OWNER_ACTIVE
    assert h.sent == []
    assert "you wrote" in h.previews[0].note


# ---- owner commands and /start ------------------------------------------------


async def test_pause_and_resume_only_for_owner_and_persist(make_harness):
    h = make_harness()
    await h.connect()

    await h.direct_message("/pause", from_id=999)
    assert h.sent == []
    assert await h.business_message("Hi") == Outcome.PREVIEWED

    await h.direct_message("/pause")
    assert h.sent[-1].chat_id == OWNER_ID
    assert await h.business_message("Hi again") == Outcome.PAUSED

    h.restart()
    await h.connect()
    assert await h.business_message("Still paused?") == Outcome.PAUSED

    await h.direct_message("/resume", from_id=999)
    assert await h.business_message("And now?") == Outcome.PAUSED
    await h.direct_message("/resume")
    assert await h.business_message("Now?") == Outcome.PREVIEWED


async def test_strangers_get_no_answer_to_commands_or_text(make_harness):
    h = make_harness()
    for text in ("/status", "/pause", "/resume", "hello bot", "tell me about the owner"):
        await h.direct_message(text, from_id=999)
    assert h.session.requests == []
    assert h.generator.calls == []


async def test_status_for_owner(make_harness):
    h = make_harness()
    await h.direct_message("/status")
    text = h.sent[-1].text
    assert "PREVIEW" in text and "running" in text


async def test_start_for_owner_explains_setup_in_uzbek(make_harness):
    h = make_harness()
    await h.direct_message("/start")
    text = h.sent[-1].text
    assert h.sent[-1].chat_id == OWNER_ID
    for expected in ("PREVIEW", "ishlayapti", "Chatbots", "@test_assistant_bot", "/pause", "/resume", "/status", "AUTO_SEND=true", "profile.md", "15 daqiqa"):
        assert expected in text


async def test_start_shows_auto_send_and_paused(make_harness):
    h = make_harness(AUTO_SEND="true")
    h.assistant.pause()
    await h.direct_message("/start")
    text = h.sent[-1].text
    assert "AUTO-SEND" in text
    assert "pauzada" in text
    assert "AUTO_SEND=true qiling" not in text


async def test_start_for_stranger_is_short_and_private(make_harness):
    h = make_harness()
    await h.direct_message("/start", from_id=999)
    assert [m.text for m in h.sent] == [STRANGER_START_TEXT]
    assert h.sent[0].chat_id == 999
    assert "Aziz" not in h.sent[0].text and "Toshkent" not in h.sent[0].text
    assert h.generator.calls == []


# ---- limits and failures ------------------------------------------------------


async def test_limit_waits_until_reset_time(make_harness):
    h = make_harness()
    await h.connect()
    reset = START_TIME + 3600
    h.generator.queue(ClaudeLimitError("usage limit reached", resets_at=reset))

    assert await h.business_message("one") == Outcome.LIMITED
    h.clock.advance(1800)
    assert await h.business_message("two") == Outcome.LIMITED
    assert len(h.generator.calls) == 1

    h.restart()
    await h.connect()
    assert await h.business_message("three") == Outcome.LIMITED

    h.clock.advance(1801)
    assert await h.business_message("four") == Outcome.PREVIEWED
    assert h.sent == []  # preview mode: the owner is not messaged


async def test_limit_without_reset_uses_cooldown(make_harness):
    h = make_harness(LIMIT_COOLDOWN_MINUTES="10")
    await h.connect()
    h.generator.queue(ClaudeLimitError("rate limit"))

    assert await h.business_message("one") == Outcome.LIMITED
    assert h.assistant.state.limit_until == START_TIME + 600
    h.clock.advance(599)
    assert await h.business_message("two") == Outcome.LIMITED
    h.clock.advance(2)
    assert await h.business_message("three") == Outcome.PREVIEWED


async def test_limit_in_auto_send_notifies_owner_once_and_resume_clears(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue(ClaudeLimitError("usage limit", resets_at=START_TIME + 3600))

    assert await h.business_message("one") == Outcome.LIMITED
    assert await h.business_message("two") == Outcome.LIMITED
    owner_messages = [m for m in h.sent if m.chat_id == OWNER_ID]
    assert len(owner_messages) == 1
    assert "usage limit" in owner_messages[0].text
    assert all(m.business_connection_id is None for m in h.sent)

    await h.direct_message("/resume")
    assert h.assistant.state.limit_until is None
    assert await h.business_message("three") == Outcome.SENT


@pytest.mark.parametrize("error", [ClaudeTimeout("too slow"), ClaudeAuthError("not logged in", "401")])
async def test_failures_send_nothing_to_chat_and_alert_owner_once(make_harness, error):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue(error, error)

    assert await h.business_message("one") == Outcome.ERROR
    assert await h.business_message("two", from_id=888) == Outcome.ERROR

    assert all(m.chat_id == OWNER_ID and m.business_connection_id is None for m in h.sent)
    assert len(h.sent) == 1


async def test_failures_in_preview_mode_message_nobody(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue(ClaudeTimeout("too slow"))
    assert await h.business_message("one") == Outcome.ERROR
    assert h.session.requests == []
