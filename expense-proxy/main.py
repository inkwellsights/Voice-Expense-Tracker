"""HTML-injection proxy for ExpenseOwl.

Intercepts text/html responses and injects a single <script> tag that
adds a multi-select tag-filter dropdown to every dashboard page (pie
chart at /, table at /table, etc.). All filtering happens client-side
via a monkey-patched window.fetch, so both the chart and the table are
filtered uniformly by simply transforming the /expenses response that
their existing JS already consumes.

Filter selection persists in sessionStorage — sticky for the browser
session, reverts to "show all" when the browser closes.

Notes:
- Previously (briefly, 2026-05-23 morning) this proxy filtered
  /expenses server-side by Cf-Access-Authenticated-User-Email. That
  identity-based personalization was rolled back the same day in favor
  of explicit user-controlled filtering.
- The proxy makes no changes to non-HTML responses; they pass through
  unmodified so PUT/DELETE/JSON endpoints work exactly as before.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse

UPSTREAM = os.getenv("EXPENSEOWL_URL", "http://expenseowl:8080").rstrip("/")

# Headers that must not be forwarded as-is when proxying responses.
# httpx returns decompressed bodies, so the upstream's Content-Encoding
# and Content-Length no longer apply after we (potentially) inject HTML.
SKIP_RESPONSE_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
}

STATIC_DIR = Path(__file__).parent / "static"
FILTER_JS_PATH = STATIC_DIR / "filter.js"

# The injection. `defer` so it runs after parsing but before
# DOMContentLoaded — our fetch hook needs to be installed before
# ExpenseOwl's own JS fires the first /expenses request.
INJECT_TAG = b'<script src="/_proxy/filter.js" defer></script>'

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("expense-proxy")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logger.info(
        "Booted. Upstream=%s. Mode=html-injection (no server-side filtering).",
        UPSTREAM,
    )
    async with httpx.AsyncClient(
        base_url=UPSTREAM,
        timeout=httpx.Timeout(15.0),
    ) as client:
        app.state.client = client
        yield


app = FastAPI(title="expense-proxy", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.get("/_proxy/filter.js")
async def serve_filter_js() -> Response:
    if not FILTER_JS_PATH.exists():
        return Response(
            content=b"// filter.js missing on server",
            media_type="application/javascript",
            status_code=500,
        )
    return FileResponse(
        FILTER_JS_PATH,
        media_type="application/javascript",
        # Short cache so iterative edits propagate fast.
        headers={"cache-control": "no-cache"},
    )


def _scrub_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in SKIP_RESPONSE_HEADERS}


def _looks_like_html(content_type: str | None) -> bool:
    return bool(content_type and "text/html" in content_type.lower())


def _inject_before_close(body: bytes) -> bytes:
    """Inject the script tag before </body>, falling back to </html>, then append."""
    lowered = body.lower()
    for marker in (b"</body>", b"</html>"):
        idx = lowered.rfind(marker)
        if idx >= 0:
            return body[:idx] + INJECT_TAG + body[idx:]
    return body + INJECT_TAG


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request) -> Response:
    upstream_path = "/" + path
    query = request.url.query
    upstream_url = f"{upstream_path}?{query}" if query else upstream_path

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }
    body = await request.body()

    client: httpx.AsyncClient = request.app.state.client
    try:
        upstream_response = await client.request(
            request.method,
            upstream_url,
            headers=fwd_headers,
            content=body if body else None,
        )
    except httpx.RequestError as exc:
        logger.exception("Upstream request failed: %s", exc)
        return Response(
            content=b'{"error":"upstream_unreachable"}',
            media_type="application/json",
            status_code=502,
        )

    content_type = upstream_response.headers.get("content-type")
    if request.method == "GET" and _looks_like_html(content_type):
        injected = _inject_before_close(upstream_response.content)
        return Response(
            content=injected,
            status_code=upstream_response.status_code,
            headers=_scrub_headers(upstream_response.headers),
            media_type=content_type,
        )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=_scrub_headers(upstream_response.headers),
        media_type=content_type,
    )
