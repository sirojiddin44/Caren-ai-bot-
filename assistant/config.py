"""Settings: read from .env (local) or from a given mapping (container), never both."""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent

SETTING_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

AUTH_LOCAL_LOGIN = "this machine's claude login"
AUTH_DOTENV_TOKEN = "subscription token from .env"
AUTH_ENVIRONMENT_TOKEN = "subscription token from environment variables"

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """Invalid or missing setting. Messages never contain secret values."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_id: int
    auto_send: bool
    claude_oauth_token: str = field(repr=False)
    claude_bin: str
    claude_model: str
    claude_effort: str
    claude_timeout: int
    limit_cooldown_minutes: int
    owner_takeover_minutes: int
    history_limit: int
    data_dir: Path
    profile_path: Path
    env_file: Path
    from_environment: bool = False

    @property
    def claude_auth_mode(self) -> str:
        if not self.claude_oauth_token:
            return AUTH_LOCAL_LOGIN
        return AUTH_ENVIRONMENT_TOKEN if self.from_environment else AUTH_DOTENV_TOKEN

    @property
    def claude_workdir(self) -> Path:
        return self.data_dir / "claude-run"


def default_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    if os.name == "nt" and not fallback.exists():
        fallback = fallback.with_suffix(".exe")
    return str(fallback)


def load_settings(
    env_file: Optional[os.PathLike] = None,
    *,
    require_telegram: bool = True,
    overrides: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, Optional[str]]] = None,
) -> Settings:
    """Load settings from ``source`` if given, otherwise only from the .env file.

    The shell environment is ignored in .env mode and ``os.environ`` is never written.
    ``env_file``'s folder is the base for relative paths in both modes.
    """
    env_path = Path(env_file) if env_file is not None else PROJECT_DIR / ".env"
    env_path = env_path.resolve()
    base_dir = env_path.parent

    if source is not None:
        raw: Dict[str, Optional[str]] = {key: source.get(key) for key in SETTING_KEYS}
        where = "the environment variables"
    else:
        if not env_path.is_file():
            raise ConfigError(
                f"{env_path} not found. Create it with: cp .env.example .env && chmod 600 .env"
            )
        raw = dict(dotenv_values(env_path))
        where = str(env_path)
    if overrides:
        raw.update(overrides)

    def value(key: str) -> str:
        item = raw.get(key)
        return item.strip() if item else ""

    def integer(key: str, default: int, minimum: int) -> int:
        text = value(key)
        if not text:
            return default
        if not re.fullmatch(r"-?\d+", text):
            raise ConfigError(f"{key} in {where} must be a whole number.")
        number = int(text)
        if number < minimum:
            raise ConfigError(f"{key} in {where} must be at least {minimum}.")
        return number

    bot_token = value("TELEGRAM_BOT_TOKEN")
    owner_text = value("OWNER_ID")
    if require_telegram:
        if not bot_token:
            raise ConfigError(f"TELEGRAM_BOT_TOKEN is empty in {where}. Get it from @BotFather.")
        if not owner_text:
            raise ConfigError(f"OWNER_ID is empty in {where}. Get your numeric id from @userinfobot.")
    if bot_token and not _BOT_TOKEN_RE.match(bot_token):
        raise ConfigError(
            f"TELEGRAM_BOT_TOKEN in {where} does not look like a bot token (<digits>:<text>)."
        )
    if owner_text and not owner_text.isdigit():
        raise ConfigError(f"OWNER_ID in {where} must contain only digits.")
    owner_id = int(owner_text) if owner_text else 0

    auto_text = value("AUTO_SEND").lower() or "false"
    if auto_text in _TRUE:
        auto_send = True
    elif auto_text in _FALSE:
        auto_send = False
    else:
        raise ConfigError(f"AUTO_SEND in {where} must be true or false.")

    oauth_token = value("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        if oauth_token.startswith("sk-ant-api"):
            raise ConfigError(
                f"CLAUDE_CODE_OAUTH_TOKEN in {where} is an Anthropic API key, not a subscription "
                "token. This bot never uses API keys: run `claude setup-token` and use the "
                "sk-ant-oat... token it prints."
            )
        if not oauth_token.startswith("sk-ant-oat") or re.search(r"\s", oauth_token):
            raise ConfigError(
                f"CLAUDE_CODE_OAUTH_TOKEN in {where} is not a valid `claude setup-token` token: it "
                "must start with sk-ant-oat and be on a single line without spaces."
            )

    effort = value("CLAUDE_EFFORT").lower() or "low"
    if effort not in EFFORT_LEVELS:
        raise ConfigError(f"CLAUDE_EFFORT in {where} must be one of: {', '.join(EFFORT_LEVELS)}.")

    claude_bin = value("CLAUDE_BIN")
    claude_bin = str(Path(claude_bin).expanduser()) if claude_bin else default_claude_bin()

    def path(key: str, default: str) -> Path:
        result = Path(value(key) or default).expanduser()
        return result if result.is_absolute() else base_dir / result

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=auto_send,
        claude_oauth_token=oauth_token,
        claude_bin=claude_bin,
        claude_model=value("CLAUDE_MODEL"),
        claude_effort=effort,
        claude_timeout=integer("CLAUDE_TIMEOUT_SECONDS", 120, 1),
        limit_cooldown_minutes=integer("LIMIT_COOLDOWN_MINUTES", 30, 1),
        owner_takeover_minutes=integer("OWNER_TAKEOVER_MINUTES", 15, 0),
        history_limit=integer("HISTORY_LIMIT", 30, 1),
        data_dir=path("DATA_DIR", "data"),
        profile_path=path("PROFILE_PATH", "profile.md"),
        env_file=env_path,
        from_environment=source is not None,
    )


def env_file_warning(path: os.PathLike) -> Optional[str]:
    """Return a warning if the .env file can be read by the group or other users."""
    if os.name == "nt":
        return None  # Windows has no POSIX permission bits to check.
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return (
            f"{path} is accessible by other users (mode {mode:o}). "
            f"It holds secrets, run: chmod 600 {path}"
        )
    return None
