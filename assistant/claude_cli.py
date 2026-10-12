"""Runs `claude -p` once per reply, isolated from the user's Claude Code setup.

The conversation goes in through stdin and the system prompt through a private temp
file, so neither shows up in `ps`. No shell is ever involved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .store import ensure_private_dir

log = logging.getLogger(__name__)

# Plugins built into the CLI, as reported in the stream-json init event of 2.1.296.
BUILTIN_PLUGINS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-plugin-authoring@builtin",
)

ISOLATION_SETTINGS: Dict[str, Any] = {
    "disableAllHooks": True,
    "enabledPlugins": {name: False for name in BUILTIN_PLUGINS},
}

# Removed from the child environment. ANTHROPIC_* is removed by prefix.
STRIPPED_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SIMPLE",  # set by --bare; it turns the OAuth login off
)

ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}

INIT_KEYS = ("tools", "mcp_servers", "plugins", "skills", "apiKeySource", "apiProvider", "model")

REDACTED = "[REDACTED]"

_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate[ _-]?limit|out of (extra )?usage|quota",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|/login|invalid api key|authenticat(e|ion)|unauthori[sz]ed|oauth token",
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\|\s*(\d{9,11})\b")
_RESET_TEXT_RE = re.compile(r"\bresets?\s+(?:at\s+)?([^\n|]+)", re.IGNORECASE)


class ClaudeError(Exception):
    """A failure that is safe to show to the bot owner."""


class ClaudeTimeout(ClaudeError):
    pass


class ClaudeAuthError(ClaudeError):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ClaudeLimitError(ClaudeError):
    def __init__(
        self, detail: str, resets_at: Optional[float] = None, reset_text: Optional[str] = None
    ) -> None:
        if reset_text:
            when = f" (resets {reset_text})"
        elif resets_at:
            when = " (resets {})".format(time.strftime("%Y-%m-%d %H:%M", time.localtime(resets_at)))
        else:
            when = ""
        super().__init__(f"Claude subscription usage limit reached{when}")
        self.detail = detail
        self.resets_at = resets_at
        self.reset_text = reset_text


def classify_failure(message: str, status: Optional[int] = None) -> ClaudeError:
    """Turn CLI error text into the matching exception (returned, not raised)."""
    message = (message or "").strip()
    if status == 429 or _LIMIT_RE.search(message):
        epoch = _EPOCH_RE.search(message)
        resets_at = float(epoch.group(1)) if epoch else None
        detail = _EPOCH_RE.sub("", message).strip()
        reset = _RESET_TEXT_RE.search(detail)
        reset_text = reset.group(1).strip(" .") if reset else None
        return ClaudeLimitError(detail, resets_at=resets_at, reset_text=reset_text or None)
    if status in (401, 403) or _AUTH_RE.search(message):
        return ClaudeAuthError(
            "Claude CLI is not logged in or its login was rejected: run `claude` on this "
            "machine and use /login.",
            detail=message[:300],
        )
    return ClaudeError(f"Claude CLI failed: {message[:500]}")


def parse_result(returncode: int, stdout: str, stderr: str) -> str:
    """Return the reply text from `--output-format json|stream-json` output, or raise."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    data: Any = None
    if lines:
        try:
            data = json.loads(lines[-1])
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise classify_failure(stderr.strip() or stdout.strip() or f"exit code {returncode}")

    result = data.get("result")
    text = result if isinstance(result, str) else ""
    if data.get("is_error") or data.get("subtype") != "success":
        message = " ".join(part for part in (text.strip(), stderr.strip()) if part)
        status = data.get("api_error_status")
        raise classify_failure(
            message or f"{data.get('subtype') or 'error'} (exit code {returncode})",
            status if isinstance(status, int) else None,
        )
    text = text.strip()
    if not text:
        raise ClaudeError("Claude CLI returned an empty reply")
    return text


def build_env(
    oauth_token: Optional[str] = None, base: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """Environment for the claude child: no API keys, no bot token, no other providers."""
    env = {
        key: value
        for key, value in (os.environ if base is None else base).items()
        if not key.startswith("ANTHROPIC_") and key not in STRIPPED_ENV
    }
    env.update(ISOLATION_ENV)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def _spawn_options() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """Kill the CLI and everything it started."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session=True, so pgid == pid
    except (ProcessLookupError, PermissionError):
        pass


class ClaudeCLI:
    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: os.PathLike,
        model: str = "",
        effort: str = "low",
        timeout: float = 120,
        oauth_token: str = "",
        auth_label: Optional[str] = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.workdir = Path(workdir)
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self._oauth_token = oauth_token
        self._auth_label = auth_label

    def __repr__(self) -> str:
        return (
            f"ClaudeCLI(claude_bin={self.claude_bin!r}, workdir={str(self.workdir)!r}, "
            f"model={self.model!r}, effort={self.effort!r}, timeout={self.timeout!r}, "
            f"auth={self.auth_mode!r})"
        )

    @property
    def auth_mode(self) -> str:
        if self._auth_label:
            return self._auth_label
        return "subscription token" if self._oauth_token else "this machine's claude login"

    def build_command(self, system_prompt_file: str, output_format: str = "json") -> List[str]:
        command = [
            self.claude_bin,
            "-p",
            "--safe-mode",
            "--settings", json.dumps(ISOLATION_SETTINGS),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", system_prompt_file,
            "--output-format", output_format,
            "--effort", self.effort,
        ]  # fmt: skip
        if output_format == "stream-json":
            command.append("--verbose")
        if self.model:
            command += ["--model", self.model]
        return command

    def _redact(self, text: str) -> str:
        return text.replace(self._oauth_token, REDACTED) if self._oauth_token else text

    def _explain_auth(self, error: ClaudeAuthError) -> ClaudeAuthError:
        if not self._oauth_token:
            return error
        return ClaudeAuthError(
            "Claude rejected the subscription token or it has expired: run `claude setup-token` "
            "on a computer with a browser and update CLAUDE_CODE_OAUTH_TOKEN. "
            f"CLI said: {error.detail}",
            detail=error.detail,
        )

    async def _run(self, system_prompt: str, stdin_text: str, output_format: str) -> Tuple[int, str, str]:
        ensure_private_dir(self.workdir)
        fd, prompt_path = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=self.workdir)
        try:
            os.chmod(prompt_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(system_prompt)
            command = self.build_command(prompt_path, output_format)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workdir),
                    env=build_env(self._oauth_token or None),
                    **_spawn_options(),
                )
            except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
                raise ClaudeError(
                    f"Claude CLI not found or not executable at {self.claude_bin} "
                    f"({type(exc).__name__}). Install Claude Code or set CLAUDE_BIN."
                ) from None
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(stdin_text.encode("utf-8")), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                _kill_process_group(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                raise ClaudeTimeout(f"Claude CLI did not answer within {self.timeout} seconds") from None
            except asyncio.CancelledError:
                _kill_process_group(proc)
                raise
            return (
                proc.returncode if proc.returncode is not None else -1,
                self._redact(stdout.decode("utf-8", errors="replace")),
                self._redact(stderr.decode("utf-8", errors="replace")),
            )
        finally:
            try:
                os.unlink(prompt_path)
            except FileNotFoundError:
                pass

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        returncode, stdout, stderr = await self._run(system_prompt, user_prompt, "json")
        try:
            return parse_result(returncode, stdout, stderr)
        except ClaudeAuthError as exc:
            raise self._explain_auth(exc) from None

    async def inspect(self) -> Dict[str, Any]:
        """Send a tiny request and report what the CLI loaded and how it authenticated."""
        returncode, stdout, stderr = await self._run("Reply with exactly: OK", "ping", "stream-json")
        init: Optional[Dict[str, Any]] = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                init = {key: event.get(key) for key in INIT_KEYS}
        try:
            reply = parse_result(returncode, stdout, stderr)
        except ClaudeAuthError as exc:
            raise self._explain_auth(exc) from None
        return {"auth": self.auth_mode, "init": init, "reply": reply}
