"""Entry point.

    python bot.py                      run the bot with settings from .env
    python bot.py --settings-from-env  run in a container (Railway) with environment variables
    python bot.py --check-claude       check the Claude CLI setup, no Telegram needed
    python bot.py --try "text"         draft a reply to a made-up message, no Telegram needed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from assistant.claude_cli import ClaudeError
from assistant.config import PROJECT_DIR, SETTING_KEYS, ConfigError, Settings, env_file_warning, load_settings
from assistant.language import detect_language
from assistant.prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from assistant.service import build_assistant, make_claude_cli, profile_is_filled, read_profile
from assistant.store import ensure_private_dir

log = logging.getLogger("bot")

BAD_KEY_SOURCES = {"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"}


def settings_from_process_env(*, require_telegram: bool = True) -> Settings:
    """Read SETTING_KEYS from os.environ, then remove them so child processes never inherit them."""
    try:
        source = {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}
        return load_settings(PROJECT_DIR / ".env", require_telegram=require_telegram, source=source)
    finally:
        for key in SETTING_KEYS:
            os.environ.pop(key, None)


async def check_claude(settings: Settings) -> int:
    cli = make_claude_cli(settings)
    try:
        report = await cli.inspect()
    except ClaudeError as exc:
        print(json.dumps({"auth": cli.auth_mode, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))

    problems: List[str] = []
    init = report.get("init")
    if not init:
        problems.append("the CLI sent no init event")
    else:
        for key in ("tools", "mcp_servers", "plugins", "skills"):
            if init.get(key):
                problems.append(f"{key} should be empty but is {init.get(key)!r}")
        if init.get("apiKeySource") in BAD_KEY_SOURCES:
            problems.append(f"the CLI used an API key ({init.get('apiKeySource')}), not the subscription")
        if init.get("apiProvider") not in ("firstParty", None):
            problems.append(f"unexpected API provider {init.get('apiProvider')!r}")
    if problems:
        for problem in problems:
            print(f"FAILED: {problem}", file=sys.stderr)
        return 1
    print(f"OK: Claude answered using {report['auth']}.")
    return 0


async def try_reply(settings: Settings, text: str) -> int:
    cli = make_claude_cli(settings)
    language = detect_language(text)
    system_prompt = build_system_prompt(read_profile(settings.profile_path), datetime.now())
    user_prompt = build_user_prompt([{"role": "other", "name": "Friend", "text": text}], language)
    print(f"Language hint: {language or 'unknown'}")
    print(f"Message:       {text}")
    try:
        reply = await cli.generate(system_prompt, user_prompt)
    except ClaudeError as exc:
        print(f"Error:         {exc}", file=sys.stderr)
        return 1
    if reply.startswith(SKIP_TOKEN):
        print("Reply:         [SKIP] (the bot would stay silent)")
    else:
        print(f"Reply:         {reply}")
    return 0


async def run_bot(settings: Settings) -> int:
    from aiogram import Bot, Dispatcher
    from aiogram.exceptions import TelegramUnauthorizedError

    from assistant.handlers import ALLOWED_UPDATES, build_router

    bot = Bot(settings.bot_token)
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram rejected TELEGRAM_BOT_TOKEN. Check it in @BotFather (or /revoke and set a new one).")
            return 2
        assistant = build_assistant(settings)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(assistant))

        log.info("Bot @%s is running", me.username)
        if settings.auto_send:
            log.warning("Mode: AUTO-SEND - replies are sent to people from your account")
        else:
            log.info("Mode: PREVIEW - replies are only shown in the logs, nothing is sent")
        log.info("Claude auth: %s", settings.claude_auth_mode)
        log.info("Claude CLI: %s", settings.claude_bin)
        if not Path(settings.claude_bin).exists():
            log.warning("Claude CLI not found at %s; replies will fail until it is installed", settings.claude_bin)
        if assistant.state.paused:
            log.warning("The bot starts PAUSED (send /resume to the bot to turn replies back on)")
        if not profile_is_filled(read_profile(settings.profile_path)):
            log.warning("%s is not filled in yet; replies will know nothing about you", settings.profile_path)

        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_signals=True)
        return 0
    finally:
        await bot.session.close()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram Business assistant that replies via the Claude Code CLI.")
    parser.add_argument("--settings-from-env", action="store_true",
                        help="read settings from environment variables instead of .env (container mode)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-claude", action="store_true", help="check the Claude CLI setup and exit")
    group.add_argument("--try", dest="try_text", metavar="TEXT", help="draft a reply to TEXT and exit")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    require_telegram = not (args.check_claude or args.try_text is not None)
    try:
        if args.settings_from_env:
            settings = settings_from_process_env(require_telegram=require_telegram)
        else:
            settings = load_settings(PROJECT_DIR / ".env", require_telegram=require_telegram)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if not settings.from_environment:
        warning = env_file_warning(settings.env_file)
        if warning:
            log.warning(warning)
    ensure_private_dir(settings.data_dir)
    ensure_private_dir(settings.claude_workdir)

    if args.check_claude:
        return asyncio.run(check_claude(settings))
    if args.try_text is not None:
        return asyncio.run(try_reply(settings, args.try_text))
    try:
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    # Replies contain Uzbek and Russian text; do not crash on a non-UTF-8 console.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
