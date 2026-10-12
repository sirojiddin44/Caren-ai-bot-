from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from assistant.claude_cli import (
    BUILTIN_PLUGINS,
    ClaudeAuthError,
    ClaudeCLI,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    classify_failure,
    parse_result,
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")

TOKEN = "sk-ant-oat01-" + "T" * 60
SYSTEM = "You are the owner's assistant. <profile>Secret profile line</profile>"
USER = "<conversation>[Ali]: Salom, qalesan?</conversation>"

FAKE_CLAUDE = r'''
import json, os, subprocess, sys, time

args = sys.argv[1:]
stdin = sys.stdin.buffer.read().decode("utf-8")
prompt_file = args[args.index("--system-prompt-file") + 1] if "--system-prompt-file" in args else None
record = {
    "argv": args,
    "stdin": stdin,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "listdir": sorted(os.listdir(os.getcwd())),
    "pid": os.getpid(),
    "prompt_file": prompt_file,
}
if prompt_file:
    with open(prompt_file, encoding="utf-8") as fh:
        record["prompt"] = fh.read()
    record["prompt_mode"] = os.stat(prompt_file).st_mode & 0o777
mode = os.environ.get("FAKE_MODE", "ok")
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

if mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    record["grandchild_pid"] = child.pid

with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as fh:
    json.dump(record, fh)

def out(obj):
    sys.stdout.buffer.write((json.dumps(obj) + "\n").encode("utf-8"))
    sys.stdout.flush()

def err(text):
    sys.stderr.buffer.write(text.encode("utf-8"))
    sys.stderr.flush()

if "stream-json" in args:
    out({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "plugins": [],
         "skills": [], "apiKeySource": "none", "model": "fake-model"})

if mode == "ok":
    out({"type": "result", "subtype": "success", "is_error": False, "result": "  Hello from fake  \n"})
elif mode == "sleep":
    time.sleep(60)
elif mode == "limit_epoch":
    out({"type": "result", "subtype": "success", "is_error": True,
         "result": "Claude AI usage limit reached|1760000000"})
    sys.exit(1)
elif mode == "limit_text":
    err("You've hit your limit · resets 3pm (Europe/Berlin)\n")
    sys.exit(1)
elif mode == "auth":
    out({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
         "result": "Failed to authenticate. OAuth token " + token + " is invalid."})
    err("token was " + token + "\n")
    sys.exit(1)
elif mode == "crash":
    err("Segmentation fault\n")
    sys.exit(139)
elif mode == "empty":
    out({"type": "result", "subtype": "success", "is_error": False, "result": "   "})
elif mode == "echo_token":
    out({"type": "result", "subtype": "success", "is_error": False, "result": "token is " + token})
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":
        binary = tmp_path / "claude.cmd"
        binary.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        binary = tmp_path / "claude"
        binary.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
        binary.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_MODE", "ok")

    class Fake:
        bin = str(binary)
        workdir = tmp_path / "data" / "claude-run"

        @staticmethod
        def mode(name):
            monkeypatch.setenv("FAKE_MODE", name)

        @staticmethod
        def record():
            return json.loads(record.read_text(encoding="utf-8"))

        @staticmethod
        def cli(**kwargs):
            kwargs.setdefault("workdir", Fake.workdir)
            return ClaudeCLI(Fake.bin, **kwargs)

    return Fake


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/status") as fh:
            return not any(line.startswith("State:") and "Z" in line for line in fh)
    except OSError:
        return True


# ---- happy path and isolation -------------------------------------------------


async def test_reply_is_stripped(fake):
    assert await fake.cli().generate(SYSTEM, USER) == "Hello from fake"


async def test_message_goes_only_through_stdin_and_profile_only_through_file(fake):
    await fake.cli().generate(SYSTEM, USER)
    rec = fake.record()

    assert rec["stdin"] == USER
    joined = " ".join(rec["argv"])
    assert "Salom" not in joined and "Secret profile line" not in joined
    assert rec["prompt"] == SYSTEM
    assert not Path(rec["prompt_file"]).exists()
    assert list(fake.workdir.iterdir()) == []


@posix_only
async def test_prompt_file_and_workdir_are_private(fake):
    await fake.cli().generate(SYSTEM, USER)
    rec = fake.record()
    assert rec["prompt_mode"] == 0o600
    assert stat.S_IMODE(os.stat(fake.workdir).st_mode) == 0o700


async def test_prompt_file_is_removed_after_errors(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError):
        await fake.cli().generate(SYSTEM, USER)
    assert not Path(fake.record()["prompt_file"]).exists()


async def test_no_shell_is_involved(fake, tmp_path):
    marker = tmp_path / "pwned"
    text = f"$(touch {marker}) `touch {marker}` ; touch {marker} & echo hi | more"
    await fake.cli().generate(SYSTEM, text)
    assert fake.record()["stdin"] == text
    assert not marker.exists()


async def test_isolation_flags_in_order(fake):
    await fake.cli(effort="high", model="sonnet").generate(SYSTEM, USER)
    argv = fake.record()["argv"]

    prompt_index = argv.index("--system-prompt-file") + 1
    expected = [
        "-p", "--safe-mode", "--settings", argv[3], "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
        "--system-prompt-file", argv[prompt_index], "--output-format", "json", "--effort", "high",
        "--model", "sonnet",
    ]  # fmt: skip
    assert argv == expected
    assert "--mcp-config" not in argv and "--bare" not in argv and "--verbose" not in argv
    settings = json.loads(argv[3])
    assert settings["disableAllHooks"] is True
    assert settings["enabledPlugins"] == {name: False for name in BUILTIN_PLUGINS}


async def test_child_environment_is_scrubbed(fake, monkeypatch, tmp_path):
    for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TELEGRAM_BOT_TOKEN",
        "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
    ):  # fmt: skip
        monkeypatch.setenv(key, "leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-from-parent-shell")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    await fake.cli().generate(SYSTEM, USER)
    env = fake.record()["env"]

    assert not [key for key in env if key.startswith("ANTHROPIC_")]
    for key in ("TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SIMPLE",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):  # fmt: skip
        assert key not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_configured_token_only_reaches_child_env(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-from-parent-shell")
    cli = fake.cli(oauth_token=TOKEN)
    await cli.generate(SYSTEM, USER)
    rec = fake.record()

    assert rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(rec["argv"])
    assert TOKEN not in rec["stdin"] and TOKEN not in rec["prompt"]
    assert TOKEN not in repr(cli)


async def test_token_is_redacted_from_output(fake):
    fake.mode("echo_token")
    assert await fake.cli(oauth_token=TOKEN).generate(SYSTEM, USER) == "token is [REDACTED]"


async def test_rejected_token_message(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake.cli(oauth_token=TOKEN).generate(SYSTEM, USER)
    message = str(info.value)
    assert "claude setup-token" in message
    assert "CLI said:" in message and "[REDACTED]" in message
    assert TOKEN not in message and TOKEN not in info.value.detail


async def test_auth_error_without_token_points_to_login(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake.cli().generate(SYSTEM, USER)
    assert "/login" in str(info.value)


async def test_inspect_reports_auth_and_init(fake):
    cli = fake.cli(oauth_token=TOKEN, auth_label="subscription token from .env")
    report = await cli.inspect()
    assert report["auth"] == "subscription token from .env"
    assert report["init"]["plugins"] == [] and report["init"]["model"] == "fake-model"
    assert report["reply"] == "Hello from fake"
    argv = fake.record()["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert fake.record()["stdin"] == "ping"


async def test_runs_in_its_own_empty_folder(fake):
    await fake.cli().generate(SYSTEM, USER)
    rec = fake.record()
    assert Path(rec["cwd"]).resolve() == fake.workdir.resolve()
    assert len(rec["listdir"]) == 1 and rec["listdir"][0].startswith("system-")


async def test_timeout_kills_the_whole_process_group(fake):
    fake.mode("sleep")
    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        await fake.cli(timeout=1).generate(SYSTEM, USER)
    assert time.monotonic() - started < 5

    rec = fake.record()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (pid_alive(rec["pid"]) or pid_alive(rec["grandchild_pid"])):
        time.sleep(0.05)
    assert not pid_alive(rec["pid"])
    assert not pid_alive(rec["grandchild_pid"])
    assert not Path(rec["prompt_file"]).exists()


# ---- failures -----------------------------------------------------------------


async def test_limit_with_epoch(fake):
    fake.mode("limit_epoch")
    with pytest.raises(ClaudeLimitError) as info:
        await fake.cli().generate(SYSTEM, USER)
    assert info.value.resets_at == 1760000000
    assert "|" not in info.value.detail
    assert str(info.value).startswith("Claude subscription usage limit reached (resets ")


async def test_limit_with_text(fake):
    fake.mode("limit_text")
    with pytest.raises(ClaudeLimitError) as info:
        await fake.cli().generate(SYSTEM, USER)
    assert info.value.reset_text == "3pm (Europe/Berlin)"
    assert str(info.value) == "Claude subscription usage limit reached (resets 3pm (Europe/Berlin))"


async def test_crash(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError) as info:
        await fake.cli().generate(SYSTEM, USER)
    assert type(info.value) is ClaudeError
    assert "Segmentation fault" in str(info.value)


async def test_empty_reply(fake):
    fake.mode("empty")
    with pytest.raises(ClaudeError, match="empty"):
        await fake.cli().generate(SYSTEM, USER)


async def test_missing_binary(tmp_path):
    cli = ClaudeCLI(str(tmp_path / "no-such-claude"), workdir=tmp_path / "run")
    with pytest.raises(ClaudeError, match="not found"):
        await cli.generate(SYSTEM, USER)


# ---- classify_failure / parse_result ------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Claude AI usage limit reached|1760000000",
        "You've hit your limit · resets 3pm",
        "You've hit your usage limit",
        "rate_limit_error",
        "Rate limit exceeded",
        "You are out of extra usage",
        "Quota exceeded",
    ],
)
def test_classify_limit(text):
    assert isinstance(classify_failure(text), ClaudeLimitError)


def test_classify_limit_by_status_and_epoch():
    error = classify_failure("Too many requests", 429)
    assert isinstance(error, ClaudeLimitError) and error.resets_at is None
    error = classify_failure("Claude AI usage limit reached|1760000000")
    assert error.resets_at == 1760000000 and error.detail == "Claude AI usage limit reached"


@pytest.mark.parametrize(
    "text, status",
    [
        ("Not logged in · Please run /login", None),
        ("Invalid API key", None),
        ("Failed to authenticate", None),
        ("401 Unauthorized", None),
        ("OAuth token has expired", None),
        ("whatever", 401),
        ("whatever", 403),
    ],
)
def test_classify_auth(text, status):
    error = classify_failure(text, status)
    assert isinstance(error, ClaudeAuthError)
    assert "/login" in str(error) and error.detail == text


def test_classify_other_is_truncated():
    error = classify_failure("x" * 2000)
    assert type(error) is ClaudeError and len(str(error)) < 600
    assert len(classify_failure("authentication " + "y" * 1000).detail) == 300


def test_parse_result_variants():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": " hi "})
    assert parse_result(0, "noise\n" + ok + "\n\n", "") == "hi"

    with pytest.raises(ClaudeLimitError):
        parse_result(1, json.dumps({"subtype": "success", "is_error": True, "result": "x", "api_error_status": 429}), "")
    with pytest.raises(ClaudeAuthError):
        parse_result(1, json.dumps({"subtype": "success", "is_error": True, "result": "x"}), "Not logged in")
    with pytest.raises(ClaudeError, match="error_max_turns"):
        parse_result(1, json.dumps({"subtype": "error_max_turns", "is_error": False}), "")
    with pytest.raises(ClaudeError, match="empty"):
        parse_result(0, json.dumps({"subtype": "success", "is_error": False, "result": ""}), "")
    with pytest.raises(ClaudeError, match="boom"):
        parse_result(1, "not json", "boom")
    with pytest.raises(ClaudeError, match="not json"):
        parse_result(1, "not json", "")
    with pytest.raises(ClaudeError, match="exit code 3"):
        parse_result(3, "", "")
