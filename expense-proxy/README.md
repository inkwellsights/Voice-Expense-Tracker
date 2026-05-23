# expense-proxy

Tiny FastAPI reverse proxy that filters ExpenseOwl's `GET /expenses`
response by tag, so each Cloudflare-Access-authenticated user sees a
personalized dashboard at the same URL.

## What it does

```
expenses.example.com
   → Cloudflare Tunnel
     → expense-proxy:5007   ← reads `Cf-Access-Authenticated-User-Email`
       ↳ maps email → tag (EMAIL_TO_TAG env)
       ↳ filters /expenses; passes everything else
     → expenseowl:8080
```

The pie chart on `/` and the table on `/table` both aggregate
client-side from `GET /expenses`, so filtering one endpoint personalizes
the whole dashboard with no UI changes.

## Configuration

| Env | Required | Notes |
|---|---|---|
| `EXPENSEOWL_URL` | yes | Upstream URL. Inside compose: `http://expenseowl:8080`. |
| `EMAIL_TO_TAG` | for personalization | Comma list of `<email>:<tag>` pairs. Tag must match what the bot writes (a Telegram first name unless `USER_TAGS` overrides). Leave blank to act as a pure pass-through. |

Example:

```
EMAIL_TO_TAG=inkwell.sights@gmail.com:Saiful,partner@example.com:Aisha
```

## Admin override

Append `?all=1` to any dashboard URL (e.g. `/?all=1`, `/table?all=1`) to
disable filtering and see the unified household view. Useful for
audits — anyone with Cloudflare Access can use it (household-grade
trust, not a security boundary).

## Why not just give each user a deep link?

ExpenseOwl `/table` does **not** accept `?tag=<name>` URL params — the
tag filter is purely UI state with no query-string handling (verified
in `internal/web/templates/functions.js`). So a URL-only approach
doesn't work without forking ExpenseOwl.

## Deploy

The service is wired into the project's root `docker-compose.yml`. From
the repo root:

```bash
docker compose up -d --build proxy
docker compose logs -f proxy
```

Once the proxy is healthy, point the Cloudflare Tunnel ingress at host
port **5007** instead of 5006:

```yaml
- hostname: expenses.example.com
  service: http://localhost:5007
```

LAN access via `http://<host>:5006` continues to hit ExpenseOwl directly
(unfiltered) — only the public tunnel route is personalized.
