#!/usr/bin/env python3
"""
cloudflared-watchdog — restores a Cloudflare Tunnel ingress rule if the
dashboard's full-list-replace behavior deletes it.

Idempotent. Silent on success. Logs only restore actions and errors.
Goes through the Cloudflare API directly — does NOT shell out to cloudflared.

CONFIGURATION (read from /etc/cloudflared-watchdog/config — KEY=VALUE per line,
created by install.sh):
  CF_ACCOUNT_ID         — Cloudflare account id
  CF_TUNNEL_ID          — Cloudflare tunnel id
  EXPECTED_HOSTNAME     — hostname that must be in the ingress (e.g. expenses.example.com)
  EXPECTED_SERVICE      — origin URL (e.g. http://localhost:5006)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_FILE = Path("/etc/cloudflared-watchdog/config")
TOKEN_PATH = Path("/etc/cloudflared-watchdog/token")
LOG_PATH = Path("/var/log/cloudflared-watchdog.log")

CATCHALL_SERVICE = "http_status:404"


def load_config() -> dict[str, str]:
    if not CONFIG_FILE.exists():
        raise SystemExit(
            f"config file {CONFIG_FILE} missing. Run install.sh first."
        )
    out: dict[str, str] = {}
    for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    required = ("CF_ACCOUNT_ID", "CF_TUNNEL_ID", "EXPECTED_HOSTNAME", "EXPECTED_SERVICE")
    missing = [k for k in required if not out.get(k)]
    if missing:
        raise SystemExit(f"config missing required keys: {missing}")
    return out


CONFIG = load_config()
ACCOUNT_ID = CONFIG["CF_ACCOUNT_ID"]
TUNNEL_ID = CONFIG["CF_TUNNEL_ID"]
EXPECTED_HOSTNAME = CONFIG["EXPECTED_HOSTNAME"]
EXPECTED_SERVICE = CONFIG["EXPECTED_SERVICE"]

API_BASE = "https://api.cloudflare.com/client/v4"
CONFIG_URL = f"{API_BASE}/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}/configurations"

RETRY_MAX = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 15


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("cloudflared-watchdog")
    logger.setLevel(logging.INFO)
    handler = logging.handlers.WatchedFileHandler(LOG_PATH)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger.addHandler(handler)
    # journalctl also gets a copy via stderr when run under systemd
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(fmt="%(levelname)s %(message)s"))
    logger.addHandler(stderr_handler)
    return logger


def read_token() -> str:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read token at {TOKEN_PATH}: {exc}") from exc
    if not token:
        raise SystemExit(f"token file {TOKEN_PATH} is empty")
    return token


def api_request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    """
    Make a Cloudflare API call with retry on 5xx / network errors.
    Raises RuntimeError on 4xx (non-retryable) and on exhausted retries.
    Never logs the token.
    """
    data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_error: str | None = None
    for attempt in range(1, RETRY_MAX + 1):
        req = urllib.request.Request(url=url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = "<unreadable>"
            # 401/403 — token problem, do not retry
            if status in (401, 403):
                raise RuntimeError(f"auth failed ({status}); check token scope") from exc
            # 4xx other than auth — also non-retryable (bad request, not found, etc.)
            if 400 <= status < 500:
                raise RuntimeError(f"client error {status}: {err_body[:300]}") from exc
            # 5xx — retry
            last_error = f"http {status}: {err_body[:200]}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = f"network: {exc}"

        if attempt < RETRY_MAX:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError(f"exhausted {RETRY_MAX} retries; last error: {last_error}")


def get_current_config(token: str) -> dict:
    response = api_request("GET", CONFIG_URL, token)
    if not response.get("success"):
        raise RuntimeError(f"GET configurations returned success=false: {response.get('errors')}")
    return response["result"]


def has_expenses_rule(ingress: list[dict]) -> bool:
    for rule in ingress:
        if rule.get("hostname") == EXPECTED_HOSTNAME and rule.get("service") == EXPECTED_SERVICE:
            return True
    return False


def build_restored_ingress(current_ingress: list[dict]) -> list[dict]:
    """
    Preserve every existing rule in its current order. Insert the expenses rule
    immediately BEFORE the catchall (http_status:404) — or append it if no
    catchall exists.

    Never deletes a rule. Never duplicates the expenses rule.
    """
    expenses_rule = {
        "hostname": EXPECTED_HOSTNAME,
        "service": EXPECTED_SERVICE,
    }

    catchall_index: int | None = None
    for idx, rule in enumerate(current_ingress):
        # Catchall in cloudflared ingress has no hostname/path and points to http_status:404
        if rule.get("service") == CATCHALL_SERVICE and not rule.get("hostname"):
            catchall_index = idx
            break

    new_ingress = list(current_ingress)
    if catchall_index is None:
        new_ingress.append(expenses_rule)
    else:
        new_ingress.insert(catchall_index, expenses_rule)
    return new_ingress


def put_config(token: str, current_result: dict, new_ingress: list[dict]) -> None:
    """
    PUT replaces the configuration. We send back the same config object with
    only the ingress array modified, so origin_request / warp_routing / etc.
    are preserved verbatim.
    """
    current_config = current_result.get("config") or {}
    new_config = dict(current_config)
    new_config["ingress"] = new_ingress

    payload = {"config": new_config}
    response = api_request("PUT", CONFIG_URL, token, body=payload)
    if not response.get("success"):
        raise RuntimeError(f"PUT configurations returned success=false: {response.get('errors')}")


def main() -> int:
    # Ensure log file exists with sane perms before logging setup grabs it
    try:
        LOG_PATH.touch(exist_ok=True)
        os.chmod(LOG_PATH, 0o640)
    except OSError:
        pass  # if we can't touch it, logging setup will surface the error

    logger = setup_logging()
    try:
        token = read_token()
        result = get_current_config(token)
        ingress = (result.get("config") or {}).get("ingress") or []

        if has_expenses_rule(ingress):
            return 0  # silent success — the whole point

        # Rule is missing. Restore it.
        logger.warning(
            "expenses rule MISSING from tunnel ingress (rules present: %d). Restoring.",
            len(ingress),
        )
        new_ingress = build_restored_ingress(ingress)

        # Sanity: we should now have exactly one expenses rule and one more rule than before
        if not has_expenses_rule(new_ingress):
            logger.error("build_restored_ingress produced ingress without expenses rule; aborting")
            return 2
        if len(new_ingress) != len(ingress) + 1:
            logger.error(
                "unexpected ingress length: before=%d after=%d; aborting",
                len(ingress),
                len(new_ingress),
            )
            return 2

        put_config(token, result, new_ingress)
        logger.warning(
            "RESTORED expenses rule (%s -> %s). Ingress now has %d rules.",
            EXPECTED_HOSTNAME,
            EXPECTED_SERVICE,
            len(new_ingress),
        )
        return 0

    except RuntimeError as exc:
        logger.error("watchdog failed: %s", exc)
        return 1
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.exception("unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
