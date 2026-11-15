# Mirai — a personal AI assistant on Telegram, powered by Claude Code

🇹🇭 ภาษาไทย: [README.th.md](README.th.md)

A self-hosted Telegram bot that lets me talk to Claude from my phone, with a persistent persona, web search, file reading, a daily 07:00 news briefing, and a built-in English-learning loop that automatically collects vocabulary from every reply.

**Why it exists:** I wanted a mobile AI assistant without paying for a separate API plan. Instead of calling the Anthropic API, the bot drives the **Claude Code CLI** (`claude -p`) that is already logged in on my PC — so it costs nothing beyond my existing subscription.

## Features

- **Chat with memory** — each conversation resumes the same Claude session (`--resume <session_id>`), so context carries across messages; `/new` starts fresh.
- **Web search + file reading, read-only by default** — tools are whitelisted through `--allowedTools` (`WebSearch, WebFetch, Read, Glob, Grep`). The bot cannot edit files or run shell commands unless explicitly configured.
- **Daily briefing** — a built-in scheduler sends a news summary at a configurable time (`DAILY_TIME`) and archives it in `reports/`.
- **Vocabulary harvesting** — replies mix in B1–B2 English words formatted as `**word** (Thai meaning)`; a regex extracts them into `workspace/vocab.md`, skipping duplicates. `/vocab` shows the latest 15.
- **Long-answer handling** — splits replies at Telegram's 4,096-character limit; anything longer than 6 messages is sent as a summary plus a full `.md` attachment.
- **Files & images in** — send a document or photo and Claude reads it (the caption becomes the instruction).
- **Push reports from the PC** — `notify.py` sends any file or message to my phone, so other scripts can report results.
- **Access control** — only chat IDs listed in `ALLOWED_CHAT_IDS` get a response.
- **Self-healing** — `start_bot.bat` runs at Windows startup and restarts the bot if it crashes.

## Architecture

```
Telegram app ──► Telegram Bot API (long polling: getUpdates)
                        │
                        ▼
                 bot.py  (Bot loop, State, scheduler)
                        │  builds prompt = system_prompt.md + user message
                        ▼
             claude -p --resume <id> --allowedTools ...   (subprocess, UTF-8)
                        │
                        ▼
        reply ──► split / Markdown→HTML (common.py) ──► sendMessage
          └──► log_vocab() ──► workspace/vocab.md
```

| File | Role |
|---|---|
| `bot.py` | Main loop: polling, commands, Claude subprocess, session state, daily scheduler, vocab logging |
| `common.py` | Thin Telegram API client, `.env` loader, message splitting, Markdown→Telegram HTML |
| `notify.py` | CLI to push a file or text message to the phone |
| `system_prompt.example.md` | Template for the mobile-mode system prompt (copy to `system_prompt.md`) |
| `daily_prompt.md` | Prompt for the morning briefing |
| `start_bot.bat` | Auto-restart wrapper used at Windows startup |

**Stack:** Python 3 (standard library only — HTTP via `urllib`, no third-party packages), Telegram Bot API, Claude Code CLI.

## Setup

1. Install and log in to Claude Code: `claude auth login` (check with `claude auth status`).
2. Create a bot with **@BotFather** → `/newbot` → copy the token.
3. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN=...`.
4. Copy `system_prompt.example.md` to `system_prompt.md` and describe yourself / the persona.
5. Run `start_bot.bat`, message the bot once — it replies with your chat ID.
6. Put that ID in `.env` as `ALLOWED_CHAT_IDS=...` and restart.

Test Claude without Telegram: `python bot.py --selftest "question"`.

## Commands

| Send | Result |
|---|---|
| any text | Chat with the assistant (continuous session) |
| `ข่าว <topic>` ("news") | Latest news on a topic, with sources |
| `แปล ...` ("translate") | Translation + key vocabulary |
| `/new` | Start a new conversation |
| `/daily` | Send the briefing now |
| `/vocab` | Last 15 collected words |
| `/files` · `/get <name>` | List saved reports / download one |
| `/status` · `/id` | Bot status / your chat ID |

## Security notes

- Secrets live only in `.env` (git-ignored). If a token leaks: @BotFather → `/revoke`.
- Run only one instance per token — a second poller causes HTTP 409 conflicts.
- Claude runs with a read-only tool whitelist; widen `ALLOWED_TOOLS` only if you understand the risk.

## What I learned

- Wrapping an agentic CLI as a backend: session resumption, timeouts, UTF-8 handling on Windows, and hiding console windows for subprocesses.
- Designing around platform limits (Telegram message size, HTML subset, long polling).
- Prompt design for a consistent persona and a structured output format that code can parse (the vocabulary regex).
