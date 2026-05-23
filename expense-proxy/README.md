# expense-proxy

Tiny FastAPI reverse proxy that adds a tag-filter dropdown to every
ExpenseOwl dashboard page by injecting a `<script>` into HTML
responses. No server-side filtering — the script monkey-patches
`fetch('/expenses')` so the existing chart and table render whatever
subset the user picked.

## What it does

```
expenses.example.com
   → Cloudflare Tunnel
     → expense-proxy:5007
       ↳ for text/html responses: inject <script src="/_proxy/filter.js">
       ↳ serve /_proxy/filter.js (the widget + fetch hook)
       ↳ everything else: pass through untouched
     → expenseowl:8080
```

The injected script:

- Renders a floating pill top-right of every page (pie chart, table,
  settings, …).
- Click → multi-select checkbox menu listing every tag seen so far.
- Selecting tags filters the page's data using OR logic (an entry is
  shown if any of its tags matches any selected tag). Empty selection
  = show all.
- Persists in `sessionStorage` (sticky for the browser session;
  reverts to "show all" when the browser closes).
- Triggers a page reload on selection change — the simplest reliable
  way to re-render both ExpenseOwl's chart and its table after the
  filter changes.

## Configuration

| Env | Required | Notes |
|---|---|---|
| `EXPENSEOWL_URL` | yes | Upstream URL. Inside compose: `http://expenseowl:8080`. |

That's it. No per-user state, no email map, no secrets.

## Deploy

From the repo root:

```bash
docker compose up -d --build proxy
docker compose logs -f proxy
```

Then point the Cloudflare Tunnel ingress at host port **5007**:

```yaml
- hostname: expenses.example.com
  service: http://localhost:5007
```

LAN access via `http://<host>:5006` continues to hit ExpenseOwl
directly (no dropdown there).

## Why this approach

Building a dashboard fork or a CF Worker for one extra dropdown was
overkill. Injecting a single `<script>` is a tiny diff that:

- Works on every dashboard page automatically (anything served as
  `text/html`).
- Survives upstream ExpenseOwl upgrades unless they change `/expenses`
  response shape.
- Adds zero state to the proxy itself — selection lives in each
  browser's `sessionStorage`.

The earlier approach (server-side filtering by
`Cf-Access-Authenticated-User-Email`) was rolled back because explicit
user-controlled filtering is more transparent for a shared household
dashboard.
