# Voice Expense Tracker — Operational Runbook

You're here because something broke. This document is in failure-mode order: most likely problem first.

Replace `<HOST>` below with whatever you configured in your local `~/.ssh/config` (or use `user@ip` directly).

## How to access the box

```bash
ssh <HOST>
cd ~/apps/voice-expense-tracker
```

## Universal first move when something is broken

```bash
ssh <HOST> 'cd ~/apps/voice-expense-tracker && docker compose ps && echo --- && docker compose logs --tail 30 bot'
```

This shows whether both containers are up and the last 30 log lines from the bot. 80% of failures are diagnosed in those 30 lines.

---

## Failure mode 1 — Bot doesn't reply to anything

**Symptom:** You send a voice note or text in Telegram, no response, no `🎧 Listening…` message.

**Diagnose:**

```bash
ssh <HOST> 'docker compose -f ~/apps/voice-expense-tracker/docker-compose.yml ps'
```

| Container status | What it means | Fix |
|---|---|---|
| Both `Up` | Bot is running but can't reach Telegram | Check the logs; restart anyway: `docker compose restart bot` |
| `voice-expense-bot` is missing or `Exited` | Container crashed | `docker compose up -d bot` |
| Both missing | Compose stack is down | `docker compose up -d` |
| `expenseowl` running, bot not | Dependency cycle | `docker compose up -d` |

If after restart the bot still doesn't reply, check the logs (`docker compose logs --tail 50 bot`) and proceed to the matching failure mode below.

---

## Failure mode 2 — Bot replies "Every Gemini model failed (last status 429)"

**Cause:** Both `gemini-2.5-flash-lite` and `gemini-2.5-flash` hit their daily free-tier quotas. This means you (or family) sent more than ~1000-1500 voice notes/messages today.

**Fix options:**

- **Wait:** Free tier resets midnight Pacific time (~2pm Dhaka). Just retry tomorrow.
- **Add billing:** Go to <https://aistudio.google.com/apikey>, click your key, enable billing on the linked GCP project. Free tier limits go away (you'll pay ~$0.075 per million tokens — about $0.01/month for personal use).
- **Add a new Gemini key from a new Google account** as a stopgap (free tier resets per project).

**If the cascade itself is the problem** (e.g. one model is permanently nuked like 2.0-flash was), edit the MODELS list in `bot/services/parser.py`:

```python
MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
```

Replace with whatever's current at <https://ai.google.dev/gemini-api/docs/models>. Then redeploy.

---

## Failure mode 3 — Bot replies "Gemini returned 401" / "Groq returned 401"

**Cause:** API key rotated/revoked but not updated in `.env`.

**Fix:**

```bash
ssh <HOST>
cd ~/apps/voice-expense-tracker
nano .env
# Update GEMINI_API_KEY or GROQ_API_KEY
docker compose up -d --build bot
```

Where to get new keys:
- **Gemini:** <https://aistudio.google.com/apikey>
- **Groq:** <https://console.groq.com/keys>
- **Telegram bot token:** message `@BotFather` in Telegram → `/mybots` → choose bot → API Token → Regenerate

---

## Failure mode 4 — Dashboard hostname returns 404

**Cause:** The Cloudflare Tunnel ingress for your dashboard hostname got wiped. Almost always because someone added a different hostname via the Cloudflare dashboard UI, which saves the entire ingress list and drops anything not in the form.

**Fix (one-off):**

1. Create a Cloudflare API token at <https://dash.cloudflare.com/profile/api-tokens> with:
   - Account → Cloudflare Tunnel → Edit
   - Zone → DNS → Edit (resource: your zone)
2. PUT the missing ingress entry back via API. You'll need your account id and tunnel id (both visible in the Cloudflare Zero Trust dashboard URL when you open the tunnel).

**Fix (permanent):** Install the `watchdog-bundle/` from this repo on your box. It checks every 5 min and auto-restores the route. Requires a long-lived API token. See `watchdog-bundle/install.sh` — it prompts for the account/tunnel/hostname/origin interactively.

---

## Failure mode 5 — Voice notes get transcribed wrong, or end up in Gujarati script

**Cause:** Audio-native Gemini is failing and falling back to Whisper, OR Whisper's auto-detect picked the wrong language.

**Diagnose:** Check the logs after sending a problematic voice note:

```bash
ssh <HOST> 'docker compose -f ~/apps/voice-expense-tracker/docker-compose.yml logs --tail 20 bot'
```

Look for "Audio-native Gemini failed" — if you see it, Gemini's audio path is hosed and the bot fell back to Whisper. The fallback transcript might be in Gujarati script for short Banglish.

**Toggle off audio-native (revert to Tier 1):**

```bash
ssh <HOST>
cd ~/apps/voice-expense-tracker
sed -i 's/USE_AUDIO_NATIVE_GEMINI=true/USE_AUDIO_NATIVE_GEMINI=false/' .env
docker compose up -d --build bot
```

To turn it back on later, flip to `true` and rebuild.

---

## Failure mode 6 — All entries logged as INCOME on the dashboard (positive amounts, green Income card)

**Cause:** ExpenseOwl uses `positive amount = income, negative = expense`. If the bot starts logging positive amounts again (e.g., after a code revert), entries land in Income.

**Fix:** Verify `bot/services/expenseowl.py:create()` is negating amount for `kind != "income"`. If not, restore the convention and re-run `scripts/flip_signs.py` on the box to flip any existing misclassified entries (set `BASE` at the top of the script to your ExpenseOwl URL if it isn't `http://localhost:5006`).

---

## Failure mode 7 — Pie chart shows "No expenses recorded this month" even though entries exist

**Cause:** ExpenseOwl is a PWA with aggressive caching.

**Fix:** Hard-refresh in browser — `Ctrl+Shift+R` (Windows/Linux) or `Cmd+Shift+R` (Mac).

If that doesn't help, check the underlying data:

```bash
ssh <HOST> 'curl -s http://localhost:5006/expenses | python3 -m json.tool | head -50'
```

If entries are there with current dates, it's purely a frontend caching issue — Incognito mode bypasses it.

---

## Failure mode 8 — Bot tells me "❌ Could not download your voice note"

**Cause:** Telegram's CDN rate-limited the bot, or network blip.

**Fix:** Just retry. Almost always transient.

---

## Common admin tasks

### Add a new allowed Telegram user

```bash
ssh <HOST>
cd ~/apps/voice-expense-tracker
nano .env
# Edit ALLOWED_TELEGRAM_USER_IDS=111,222,NEW_ID
docker compose up -d --build bot
```

### Force the bot to restart cleanly

```bash
ssh <HOST> 'cd ~/apps/voice-expense-tracker && docker compose restart bot'
```

### Watch the bot live

```bash
ssh <HOST> 'cd ~/apps/voice-expense-tracker && docker compose logs -f bot'
```

### Update the bot's code after editing on desktop

```bash
# From the Windows desktop (E:\msaai\Voice Expense Tracker)
tar -czf - bot/ | ssh <HOST> 'cd ~/apps/voice-expense-tracker && tar -xzf - && docker compose up -d --build bot'
```

### Add a new Gemini fallback model (in case Google deprecates one)

Edit `bot/services/parser.py`:

```python
MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
```

Add a new model in priority order. Redeploy.

### Manually log an entry (web form)

Open the dashboard, click `+ Add Expense`. Useful when the bot is down and you need to record an expense for later.

### Roll back from audio-native to Whisper-first

Set `USE_AUDIO_NATIVE_GEMINI=false` in `.env` and restart the bot.

---

## Routine maintenance schedule

| When | What |
|---|---|
| **Once a month** | Eyeball `/month all` in Telegram — sanity check that all categories look reasonable. |
| **Quarterly** | Check Gemini and Groq dashboards for usage trends. If approaching quota limits, add billing. |
| **Yearly** | Check if Google has deprecated models on your MODELS list. Update if needed. |
| **As needed** | Rotate API keys after any leak suspicion. |

---

## Files that matter for ops

- `~/apps/voice-expense-tracker/.env` — secrets and config
- `~/apps/voice-expense-tracker/docker-compose.yml` — container layout
- `~/apps/voice-expense-tracker/bot/services/parser.py` — Gemini model list, system prompt
- `~/apps/voice-expense-tracker/bot/services/transcriber.py` — Whisper config (only used as fallback now)
- `~/apps/voice-expense-tracker/scripts/flip_signs.py` — emergency tool to flip income/expense signs in bulk if convention breaks again
- ExpenseOwl data: `~/apps/voice-expense-tracker/data/expenseowl/` — back this up if you care about historical data

## Backup the data

```bash
ssh <HOST> 'tar -czf - ~/apps/voice-expense-tracker/data/expenseowl' > expenseowl-backup-$(date +%Y%m%d).tar.gz
```

Drop the resulting file somewhere safe. Restore by extracting it into the same path and `docker compose restart expenseowl`.
