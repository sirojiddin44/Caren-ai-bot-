# Telegram Business assistant (Claude Code CLI)

A personal assistant for your own Telegram account. It connects through Telegram Business
(*Chatbots*). When someone writes to you, it drafts a reply in your name and in your style,
based on `profile.md`.

Replies are written by the **Claude Code CLI** (`claude -p`) installed next to the bot, so your
Claude **subscription** is used. An Anthropic API key is never used (an `sk-ant-api...` value is
rejected).

## What it does

- **Preview mode is the default** (`AUTO_SEND=false`). Drafted replies only appear in the logs
  (`PREVIEW, not sent`) and in `data/previews.jsonl`. Nothing is sent until you set `AUTO_SEND=true`.
- **Telegram decides which chats the bot sees**, in *Settings → Telegram Business → Chatbots*.
  The code has no chat allowlist: every chat Telegram delivers is handled.
- It runs on Railway from a private GitHub repo (Dockerfile build, long polling: no domain,
  webhook or port). Your computer is only for development and testing.

## Behaviour

| Situation | What happens |
|---|---|
| Business connection of another account | Ignored (warning in the log); Claude is not called |
| Which chats | Whatever you selected in Telegram *Business → Chatbots*; no list in the code |
| You write in a chat yourself | Saved to history as yours, never answered; the bot stays silent in that chat for `OWNER_TAKEOVER_MINUTES` (default 15) |
| Messages from bots (incl. this bot's own replies) | Ignored |
| The same update twice (parallel, repeated or after a restart) | Handled once; seen ids are kept in `data/seen.json` |
| Service messages (pins, title changes, ...) | Ignored. Media is described for Claude: `[photo] caption`, `[voice message]`, `[sticker] 😀` |
| `/pause` | No drafting and no sending until `/resume`; survives restarts |
| Subscription usage limit | No retries until the reset time (or `LIMIT_COOLDOWN_MINUTES` if unknown); survives restarts; in auto-send mode you get one message |
| Timeout, login error, crash | Logged; nothing goes to the chat; in auto-send mode you get at most one alert per 10 minutes |
| Claude answers `[SKIP]` | Nothing is shown or sent (e.g. after "ok" or a sticker) |
| Right before auto-sending | Pause/limit/takeover are checked again, and the connection is fetched **fresh from Telegram** (owner, enabled, `can_reply`). If anything is off, the reply is not sent and the preview says why |

Owner commands (in your private chat with the bot): `/pause`, `/resume` (also clears the limit and
takeover timers), `/status`. `/start` shows you a setup guide in Uzbek. Anyone else who sends `/start`
gets a short Uzbek note that this is a private assistant and they should write to you directly.
Claude is not called and nothing about you is revealed. Their other messages are ignored.

## How Claude is called

One process per reply, started with `asyncio.create_subprocess_exec`. No shell is ever used:

```
claude -p --safe-mode --settings '{"disableAllHooks": true, "enabledPlugins": {...: false}}'
       --tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands
       --no-session-persistence --system-prompt-file <temp file> --output-format json --effort low
       [--model ...]
```

- No tools, MCP servers, skills, hooks, slash commands, settings files, CLAUDE.md or memory. The CLI's
  built-in plugins (`cc-plugin-agents-md`, `cc-plugin-telemetry`, `cc-plugin-plugin-authoring` in
  2.1.296) are turned off through `--settings`.
- `--bare` is **not** used: it ignores the OAuth login, so the subscription would not work.
- The conversation goes through **stdin** only. The system prompt (with your profile) is written to a
  0600 temp file in `data/claude-run` (0700) and deleted afterwards, so neither appears in `ps`.
- The child environment drops `ANTHROPIC_*`, `TELEGRAM_BOT_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `CLAUDE_CODE_SIMPLE`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and the Bedrock/Vertex/Foundry switches,
  and sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`.
  `CLAUDE_CODE_OAUTH_TOKEN` is added back only when it is set in the bot's settings.
- On timeout the whole process group is killed. The token is replaced with `[REDACTED]` in CLI
  output before it is parsed or logged.
- **Token rules:** `CLAUDE_CODE_OAUTH_TOKEN` is the `sk-ant-oat...` output of `claude setup-token`
  (valid for 1 year). Locally it is read only from `.env` (the shell environment is ignored). On
  Railway it is a Variable. If it is empty, this machine's own `claude` login is used (local testing only).
- `python bot.py --check-claude` sends one tiny request and checks that tools, MCP servers, plugins and
  skills are empty, that no API key was used, and which auth was used.

## Deploy to Railway

1. On a computer with a browser run `claude setup-token`, confirm with your subscription account,
   copy the `sk-ant-oat...` token.
2. In @BotFather: `/newbot`, then *Bot Settings → Business Mode* → turn on. Get your numeric id from
   @userinfobot.
3. Create a **private** GitHub repo and push this code (`profile.md` is built into the image).
4. Railway → *New Project* → *GitHub Repository* → pick the repo. It builds from the `Dockerfile`.
5. Service → *Variables* → *Raw Editor*:
   ```
   TELEGRAM_BOT_TOKEN=...
   OWNER_ID=...
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
   AUTO_SEND=false
   ```
   Optional: `CLAUDE_MODEL`, `CLAUDE_EFFORT`, `CLAUDE_TIMEOUT_SECONDS`, `LIMIT_COOLDOWN_MINUTES`,
   `OWNER_TAKEOVER_MINUTES`, `HISTORY_LIMIT`. Do not set `CLAUDE_BIN`, `DATA_DIR` or `PROFILE_PATH`.
6. Volume: right-click the canvas (or Cmd+K) → *Volume* → choose the service → mount path exactly
   `/app/data` → *Deploy*. Without it, history, pause and limit state are lost on every deploy.
7. Logs should show `Bot @... is running`, `Mode: PREVIEW ...` and
   `Claude auth: subscription token from environment variables`. Red lines in Railway just mean
   stderr, not errors.
8. Send `/start` to the bot. Then Telegram *Settings → Telegram Business → Chatbots* (needs
   Telegram Premium): enter the bot's username, choose which chats it may see, allow it to reply.
   The log shows `Business connection ...: user=<id> enabled=True can_reply=True`.
9. When someone writes to you, the log shows a `PREVIEW, not sent` block. When the replies look
   right, set `AUTO_SEND=true` in Variables.

Notes:
- Run the bot in **one place only**. Two pollers with the same bot token interfere with each other,
  so do not start `python bot.py` locally while Railway runs it.
- Every push redeploys. Changing `profile.md` also means commit + push.
- Claude token expired or leaked: run `claude setup-token` again and update the Railway Variable.
  Bot token leaked: `/revoke` in @BotFather and update `TELEGRAM_BOT_TOKEN`.
- The image pins Claude Code `2.1.296` (`ARG CLAUDE_VERSION`); flags and built-in plugins were
  checked against that version. Check `--check-claude` again before raising it.

## Local development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip
cp .env.example .env && chmod 600 .env            # then fill it in with an editor
.venv/bin/python bot.py --check-claude
.venv/bin/python bot.py --try "Salom, qalesan?"
```

Locally only `.env` is read (`dotenv_values`), never the shell environment, and `os.environ` is not
modified. If `CLAUDE_CODE_OAUTH_TOKEN` is empty, the `claude` login of this machine is used.
`CLAUDE_BIN` defaults to `claude` on `PATH`, then `~/.local/bin/claude`.

## Commands

| Command | What it does | Exit codes |
|---|---|---|
| `python bot.py` | Run the bot with `.env` | 0 stopped, 2 bad config or bot token |
| `python bot.py --settings-from-env` | Container mode: settings from environment variables, which are then removed from `os.environ` so `claude` never inherits the bot token. `.env` is not read | same |
| `python bot.py --check-claude` | Check the CLI setup and auth, no Telegram needed | 0 ok, 1 check failed, 2 bad config |
| `python bot.py --try "text"` | Draft a reply to a made-up message, no Telegram needed | 0 ok, 1 Claude error, 2 bad config |

`--check-claude` and `--try` also work with `--settings-from-env`.

## Tests

```sh
.venv/bin/python -m pytest
```

All tests run offline: a recording fake Telegram session, a fake reply generator and a fake `claude`
executable. Permission-bit tests are skipped on Windows.

## Files

```
bot.py                     entry point and CLI modes
assistant/config.py        settings (.env or environment), validation
assistant/claude_cli.py    isolated `claude -p` runner, error classification
assistant/language.py      uz / ru / en hint
assistant/prompts.py       system and user prompts
assistant/store.py         history, seen ids, runtime state (JSON under data/)
assistant/service.py       the decision logic for every business message
assistant/handlers.py      aiogram router and owner commands
profile.md                 who you are and how you write (fill it in)
prompts/2-profil-toldirish.md  how to fill in the profile (Uzbek)
Dockerfile, railway.json, .dockerignore   Railway build and deploy
tests/                     offline test suite
```

Runtime data in `data/` (a Railway Volume at `/app/data`): `history.json`, `seen.json`,
`state.json`, `previews.jsonl`, `claude-run/`.
