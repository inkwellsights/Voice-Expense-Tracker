"""Filtering reverse proxy for ExpenseOwl.

Sits in front of ExpenseOwl's HTTP API. For each authenticated request
(via Cloudflare Access), it filters GET /expenses so the dashboard only
shows that user's tagged entries. Everything else passes through.

Identity comes from the `Cf-Access-Authenticated-User-Email` header that
Cloudflare Access injects on every tunnel request. The email is mapped
to a tag (the same tag the bot writes) via the EMAIL_TO_TAG env var.

Use `?all=1` on any URL to bypass filtering and see the unified view.

EMAIL_TO_TAG=inkwell.sights@gmail.com:Saiful,user2@example.com:Aisha
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

UPSTREAM = os.getenv("EXPENSEOWL_URL", "http://expenseowl:8080").rstrip("/")

# Hop-by-hop headers and content-coding headers that must not be
# forwarded as-is. httpx returns already-decompressed bodies, so the
# upstream's Content-Encoding/Content-Length no longer applies once we
# re-serialize.
SKIP_RESPONSE_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
}


def _parse_email_map() -> dict[str, str]:
    raw = os.getenv("EMAIL_TO_TAG", "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        email, tag = chunk.split(":", 1)
        email = email.strip().lower()
        tag = tag.strip()
        if email and tag:
            out[email] = tag
    return out


EMAIL_TO_TAG = _parse_email_map()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("expense-proxy")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logger.info(
        "Booted. Upstream=%s. Mapped emails: %s",
        UPSTREAM,
        sorted(EMAIL_TO_TAG.keys()) or "(none — proxy is a pass-through)",
    )
    async with httpx.AsyncClient(
        base_url=UPSTREAM,
        timeout=httpx.Timeout(15.0),
    ) as client:
        app.state.client = client
        yield


app = FastAPI(title="expense-proxy", docs_url=None, redoc_url=None, lifespan=_lifespan)


def _resolve_tag(request: Request) -> str | None:
    """Return the tag to filter on, or None to skip filtering."""
    if request.query_params.get("all") == "1":
        return None
    email = request.headers.get("cf-access-authenticated-user-email", "").strip().lower()
    if not email:
        return None
    return EMAIL_TO_TAG.get(email)


def _filter_expenses(payload: Any, tag: str) -> Any:
    """Keep only entries whose tags array contains `tag`.

    Preserves the response shape — ExpenseOwl may return a bare array or
    `{"expenses": [...]}` depending on build.
    """
    def _keep(item: dict) -> bool:
        return tag in (item.get("tags") or [])

    if isinstance(payload, list):
        return [e for e in payload if _keep(e)]
    if isinstance(payload, dict) and isinstance(payload.get("expenses"), list):
        return {**payload, "expenses": [e for e in payload["expenses"] if _keep(e)]}
    return payload


def _scrub_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in SKIP_RESPONSE_HEADERS}


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
            content=json.dumps({"error": "upstream_unreachable"}).encode(),
            media_type="application/json",
            status_code=502,
        )

    if request.method == "GET" and path == "expenses":
        tag = _resolve_tag(request)
        if tag:
            try:
                payload = upstream_response.json()
                filtered = _filter_expenses(payload, tag)
                body_out = json.dumps(filtered).encode()
                return Response(
                    content=body_out,
                    status_code=upstream_response.status_code,
                    media_type="application/json",
                    headers={"x-expense-proxy-filter": tag},
                )
            except (ValueError, json.JSONDecodeError):
                logger.warning("Could not JSON-decode /expenses response; passing through unfiltered.")

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=_scrub_response_headers(upstream_response.headers),
        media_type=upstream_response.headers.get("content-type"),
    )
