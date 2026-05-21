"""Two-tier allowlist for Telegram users.

Tier 1 (static)  — comes from ALLOWED_TELEGRAM_USER_IDS in .env. Read once
                   at startup. Cannot be revoked at runtime (you'd edit .env
                   and rebuild).
Tier 2 (dynamic) — persisted to data/bot/allowed_users.json. Added/removed at
                   runtime by an admin via /allow and /revoke. Survives bot
                   restarts because the file is in a host-mounted volume.

A user is allowed if they appear in either tier. If both tiers are empty the
bot is open to anyone (useful during first-run setup).

Concurrency: writes are guarded by an asyncio.Lock so two /allow commands
running back-to-back can't lose an update.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class Allowlist:
    def __init__(self, static_ids: set[int], json_path: Path) -> None:
        self._static = frozenset(int(x) for x in static_ids)
        self._json_path = json_path
        self._lock = asyncio.Lock()
        self._dynamic: set[int] = self._load_dynamic()
        logger.info(
            "Allowlist initialised: %d static + %d dynamic (path=%s)",
            len(self._static), len(self._dynamic), self._json_path,
        )

    def _load_dynamic(self) -> set[int]:
        if not self._json_path.exists():
            return set()
        try:
            data = json.loads(self._json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Could not read %s (%s); starting with empty dynamic list",
                         self._json_path, exc)
            return set()
        ids = data.get("user_ids") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            return set()
        out: set[int] = set()
        for x in ids:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                continue
        return out

    def _save_dynamic(self) -> None:
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._json_path.with_suffix(self._json_path.suffix + ".tmp")
        payload = {"user_ids": sorted(self._dynamic)}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._json_path)

    # ---- read API (sync, no lock needed — sets are read atomically) -------

    def __contains__(self, user_id: int) -> bool:
        return user_id in self._static or user_id in self._dynamic

    def is_open(self) -> bool:
        """True when no one is on the allowlist → bot accepts anyone."""
        return not self._static and not self._dynamic

    def static_ids(self) -> frozenset[int]:
        return self._static

    def dynamic_ids(self) -> set[int]:
        return set(self._dynamic)

    def all_ids(self) -> set[int]:
        return set(self._static) | self._dynamic

    # ---- write API (async, guarded) ---------------------------------------

    async def add(self, user_id: int) -> str:
        """Returns a short status: 'added' | 'already-static' | 'already-dynamic'."""
        async with self._lock:
            if user_id in self._static:
                return "already-static"
            if user_id in self._dynamic:
                return "already-dynamic"
            self._dynamic.add(user_id)
            self._save_dynamic()
            logger.info("Allowlist: added %d", user_id)
            return "added"

    async def remove(self, user_id: int) -> str:
        """Returns: 'removed' | 'static-cannot-remove' | 'not-found'."""
        async with self._lock:
            if user_id in self._static:
                return "static-cannot-remove"
            if user_id not in self._dynamic:
                return "not-found"
            self._dynamic.remove(user_id)
            self._save_dynamic()
            logger.info("Allowlist: removed %d", user_id)
            return "removed"
