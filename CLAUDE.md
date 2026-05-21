# CLAUDE.md — Voice Expense Tracker

## Overview

A self-hosted, zero-cost personal expense tracker. ExpenseOwl runs in Docker as the visual dashboard and JSON store. A Python Telegram bot accepts voice notes (transcribed via Groq Whisper), text messages, and receipt photos, parses them with Gemini Flash, and POSTs structured entries into ExpenseOwl. The bot itself is stateless — ExpenseOwl is the database.

## How to run

ExpenseOwl (dashboard + API):

```bash
docker compose up -d
# Visit http://localhost:5006 for the dashboard
```

Telegram bot:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env            # then fill in the three keys
python -m bot.main
```

Run the bot as a module (`python -m bot.main`) so relative imports resolve. Running `python bot/main.py` directly will fail.

## Environment setup

1. Create a bot with [@BotFather](https://t.me/BotFather) → grab `TELEGRAM_BOT_TOKEN`.
2. Sign up at [console.groq.com](https://console.groq.com) → create `GROQ_API_KEY`.
3. Sign up at [aistudio.google.com](https://aistudio.google.com/apikey) → create `GEMINI_API_KEY`.
4. (Optional) Set `ALLOWED_TELEGRAM_USER_IDS` to a comma-separated list of numeric Telegram IDs so only those people can talk to the bot. Leave blank for "anyone".
5. (Optional) Set `USER_TAGS=<id>:<name>,<id>:<name>` to override the auto-tag that defaults to each user's Telegram first name.

If the bot and ExpenseOwl run in the same Docker network, set `EXPENSEOWL_URL=http://expenseowl:5006`. If the bot runs on the host while ExpenseOwl runs in Docker, leave it as `http://localhost:5006`.

## ExpenseOwl API reference

No auth required. The container serves on port **8080** internally; we publish it on the host at **5006** (see `docker-compose.yml`). No `/api/` prefix — these paths were discovered by reading the live UI's `fetch(...)` calls on ExpenseOwl v4+.

| Method | Path | Body / Notes |
|---|---|---|
| `PUT` | `/expense` | Create. Body `{ "name": string, "amount": number, "category": string, "date": ISO-8601 }`. Response echoes the row but `id` is blank — fetch `/expenses` to retrieve the real UUID. |
| `GET` | `/expenses` | List. Returns a JSON array (or `{ "expenses": [...] }` on some builds — client handles both). |
| `DELETE` | `/expense/delete?id=<uuid>` | Delete by UUID (query string, NOT path segment). |
| `GET` | `/config` | Runtime config: categories, currency, start date. Useful for sanity-checking what the dashboard expects. |

Inside the compose network the bot reaches the API at `http://expenseowl:8080`; from your laptop/phone the dashboard is at `http://<casaos-ip>:5006`.

Categories are constrained to: `Food, Transport, Shopping, Bills, Health, Entertainment, Subscriptions, Other`.

## Voice notes — language

Whisper-large-v3-turbo auto-detects language, so voice notes can be in English, Bengali (Bangla), or Banglish (code-switched). The Gemini system prompt also tells the parser to extract the numeric amount regardless of language.

## Timezone

All date logic uses `Asia/Dhaka` (UTC+6) via `zoneinfo`. `/today` and `/month` filter by local date in this timezone, and expense timestamps are written in local ISO-8601.

## Free-tier limits

- **Groq Whisper** (`whisper-large-v3-turbo`): ~28,800 seconds of audio per day on the free tier — well over personal usage.
- **Gemini 2.0 Flash**: 15 RPM, 1,500 requests/day, 1M tokens/day on the free tier.

If either provider returns a 429 the bot replies with the error verbatim — it does not silently swallow failures.

## Project layout

```
bot/
  config.py                # env loading, categories, timezone
  main.py                  # entry point
  handlers/
    common.py              # shared auth + reply helpers
    voice.py               # voice → Whisper → Gemini → ExpenseOwl
    text.py                # text → Gemini → ExpenseOwl
    photo.py               # photo → Gemini Vision → ExpenseOwl
    commands.py            # /start, /today, /month, /categories, /undo
  services/
    transcriber.py         # Groq Whisper REST client (httpx)
    parser.py              # Gemini Flash REST client (httpx) — text + image
    expenseowl.py          # ExpenseOwl REST client (httpx)
data/expenseowl/           # ExpenseOwl volume (gitignored)
docker-compose.yml
requirements.txt
.env.example
```

## Design notes

- **Stateless bot**: `/undo` works by listing ExpenseOwl, sorting newest-first by date, and deleting the top entry that belongs to the calling user. An in-process `last_expense_id` map (per Telegram user id) is tracked as a hint, but the list-and-pick fallback survives bot restarts.
- **Multiple expenses per message**: the Gemini prompt returns a JSON array, so "coffee 120, uber 280, groceries 1500" produces three separate POSTs.
- **No silent failures**: every service exception (Groq, Gemini, ExpenseOwl) bubbles up as a reply prefixed with ❌.
- **No paid APIs**: Groq + Gemini free tiers. No OpenAI, no Anthropic, no Whisper local install needed.

## Multi-user (shared dashboard, scoped commands)

ExpenseOwl itself has no user model, but it does have **native per-expense tags** and the dashboard has a tag filter UI. The bot uses tags to mark ownership: every PUT sends `tags: ["<TelegramFirstName>"]`. One Telegram bot, one ExpenseOwl, multiple humans on the same dashboard — and each person can filter by their own tag to see only their spend.

- Anyone in `ALLOWED_TELEGRAM_USER_IDS` (or anyone at all, if it's blank) can message the bot.
- Each entry stores the sender as a tag on the expense (e.g. `tags: ["Saif"]`).
- On the dashboard (`/table` page), use the tag filter to scope the view to one person.
- `/today` and `/month` in Telegram default to the *caller's* entries only. Add `all` (e.g. `/today all`) for the household view, which also shows a per-person breakdown derived from the first tag on each entry.
- `/undo` only deletes the caller's own entries — you can't accidentally remove someone else's.
- The tag defaults to the user's Telegram first name; override via `USER_TAGS=<id>:<name>,<id>:<name>` if first names collide.

If you ever want fully private dashboards per user, run one ExpenseOwl container per user and add a `user_id → expenseowl_url` map in `config.py`.
