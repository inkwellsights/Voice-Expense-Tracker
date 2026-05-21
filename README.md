# Voice Expense Tracker

Self-hosted, zero-cost personal expense tracker. Send a Telegram voice note, text, or receipt photo — get it logged to a private dashboard. Handles English, Bengali, and Banglish (mixed) natively. Detects income vs expenses automatically.

- **Input**: Telegram bot → voice / text / photo
- **Brain**: [Gemini](https://aistudio.google.com) (audio-native multimodal — single API call) with a Whisper + Gemini-text fallback if the audio path fails
- **Dashboard**: [ExpenseOwl](https://github.com/Tanq16/ExpenseOwl) in Docker — pie charts, table view, per-tag filtering
- **Privacy option**: optional Cloudflare Tunnel + Access for public HTTPS dashboard with email-PIN login
- **Resilience option**: optional watchdog that restores your tunnel route if Cloudflare's dashboard UI overwrites it
- **Cost**: $0/mo on free tiers for personal use (Gemini ~1000-1500 req/day, Groq ~2000 req/day)

## Quick start

### 1. Get three free API keys

| Key | Where |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` |
| `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> |
| `GROQ_API_KEY` | <https://console.groq.com/keys> (only used as audio fallback — still set it) |

### 2. Configure

```bash
git clone https://github.com/inkwellsights/Voice-Expense-Tracker.git
cd Voice-Expense-Tracker
cp .env.example .env
# Edit .env — paste the three keys, optionally set ALLOWED_TELEGRAM_USER_IDS
```

### 3. Run

```bash
docker compose up -d --build
```

That brings up two containers: `expenseowl` (the dashboard, port 5006) and `voice-expense-bot` (the Telegram bot).

- Dashboard: <http://localhost:5006>
- Bot: open Telegram, search for your bot's `@username`, tap Start

### 4. Tail logs (optional)

```bash
docker compose logs -f bot
```

## What it can do

| Input | Example |
|---|---|
| English voice | _"Coffee 200 and uber 280"_ |
| Bangla voice | _"দুপুরে খাবার ৩৫০ টাকা"_ |
| Banglish voice | _"Coffee duisho taka, uber 280 lagse"_ |
| Income | _"I got 200 today"_ or _"amake duisho taka dise"_ → logged as +৳200 income |
| Multi-event | _"salary 30000 paisi, burger saare paach sho, rickshaw 100"_ → income + 2 expenses |
| Text | `coffee 120, groceries 1500` |
| Photo | Snap a receipt |

The bot reply is a single message that morphs through stages — `🎧 Listening…` → `🤖 thinking…` → `✅ Logged: ...` — so you always see something happening.

## Commands

| Command | What it does |
|---|---|
| `/start` | Welcome + usage |
| `/today` | Your expenses today. Add `all` for the household view (everyone, with per-person breakdown). |
| `/month` | Your spend this month by category. Add `all` for the shared view. |
| `/categories` | List of valid categories. |
| `/undo` | Delete your most recently logged entry. Never touches anyone else's. |

## Multi-user (one bot, one dashboard)

ExpenseOwl is single-tenant by design, but the bot **tags every entry with the sender's name** (Telegram first name) so the same dashboard works for a household.

1. Each user opens the bot's `@username` in Telegram and taps Start.
2. They send their numeric Telegram id (from [@userinfobot](https://t.me/userinfobot)) to you.
3. You add their id to `ALLOWED_TELEGRAM_USER_IDS=` in `.env` and `docker compose up -d --build bot`.
4. They can now log expenses. On the dashboard, use the `Tags` filter to see one person's spend at a time.

If two users share a first name, override with `USER_TAGS=11111:Saif,22222:Aisha` in `.env`.

## Architecture

```
Telegram voice note
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ Bot container (python-telegram-bot)                          │
│   ├─ if USE_AUDIO_NATIVE_GEMINI=true (default):              │
│   │     POST audio → Gemini Flash (multimodal) → JSON        │
│   │     ↳ on failure, fall through to ↓                      │
│   └─ Groq Whisper → text → Gemini Flash (text) → JSON        │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ ExpenseOwl container (JSON store + dashboard)                │
│   PUT /expense   { name, amount, category, date, tags }      │
│   amount sign: positive = income, negative = expense         │
└──────────────────────────────────────────────────────────────┘
```

Resilience baked in:

- **Gemini model cascade.** Primary `gemini-2.5-flash-lite`, fallback `gemini-2.5-flash`. Per-model retry with backoff on 429/5xx.
- **Confidence-gated transcription.** Whisper's `no_speech_prob` and `avg_logprob` flagged transcripts get rejected with a "didn't catch that" reply instead of forwarding nonsense.
- **Audio-native primary, Whisper fallback.** If Gemini's audio path is down, the bot transcribes via Groq and feeds the text to Gemini.

## Optional: Cloudflare Tunnel + Access (public HTTPS dashboard)

ExpenseOwl ships with no authentication. To expose the dashboard safely on the public internet:

1. Set up a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) on your box, routing `expenses.yourdomain.com → http://localhost:5006`.
2. Put it behind a [Cloudflare Access self-hosted application](https://developers.cloudflare.com/cloudflare-one/policies/access/) with an Allow policy listing your emails — one-time PIN by default.

**Important:** the Cloudflare dashboard's "Public Hostname" form saves the full ingress list and can silently drop entries when you add another hostname. To self-heal, see `watchdog-bundle/` — a systemd timer that auto-restores your route every 5 min via the Cloudflare API. Run `watchdog-bundle/install.sh` as root on the box.

## File layout

```
bot/
  main.py                  # entry point
  config.py                # env loading, settings dataclass
  handlers/
    voice.py               # voice → Gemini-audio (or Whisper fallback) → ExpenseOwl
    text.py                # text → Gemini → ExpenseOwl
    photo.py               # receipt photo → Gemini vision → ExpenseOwl
    commands.py            # /start /today /month /categories /undo
    common.py              # shared auth, user-tagging, reply formatting
  services/
    parser.py              # Gemini text + image + audio (model cascade + retry)
    transcriber.py         # Groq Whisper (fallback only)
    expenseowl.py          # ExpenseOwl REST client
docker-compose.yml         # both containers
Dockerfile                 # bot image
scripts/flip_signs.py      # emergency tool: flip income/expense signs in bulk
watchdog-bundle/           # Cloudflare Tunnel ingress watchdog (optional)
RUNBOOK.md                 # ops runbook — read this when something breaks
CLAUDE.md                  # architecture deep-dive
.env.example               # config template — copy to .env
```

## Categories

`Food`, `Transport`, `Shopping`, `Bills`, `Health`, `Entertainment`, `Subscriptions`, `Other`.

If you want different categories, edit `CATEGORIES` in `bot/config.py` AND update them on the dashboard via Settings → Categories (or `PUT /categories/edit` on ExpenseOwl).

## Free-tier limits

- **Gemini 2.5 Flash-Lite (primary)**: ~1000-1500 requests/day on free tier. ~30+ messages/day forever, free.
- **Gemini 2.5 Flash (fallback)**: ~250 requests/day on free tier.
- **Groq Whisper (audio fallback only)**: 2000 requests/day, 28,800 sec of audio/day.

For most personal/household use you'll never hit a cap. If you do, add a billing account on your Gemini key (new GCP users get $300 free credit) — paid pricing is ~$0.075 per million input tokens, roughly $0.01/month at personal scale.

## Going local (optional, advanced)

If you have an NVIDIA GPU box on your LAN/Tailscale, you can self-host Whisper there for free unlimited transcription + better privacy. See [`docs/LOCAL_WHISPER_SETUP.md`](docs/LOCAL_WHISPER_SETUP.md) for the full recipe (native Windows + faster-whisper + FastAPI, no Docker needed).

## Troubleshooting

Read [`RUNBOOK.md`](RUNBOOK.md) — it's organised by failure mode, most likely first.

## Resetting to a clean state

```bash
docker compose down
rm -rf data/  # WARNING: deletes all logged expenses
docker compose up -d
```

## Acknowledgements

- [ExpenseOwl](https://github.com/Tanq16/ExpenseOwl) by Tanq16 — the dashboard
- [Groq](https://groq.com) — fast Whisper inference
- [Google AI Studio](https://aistudio.google.com) — Gemini models

## License

MIT.
