"""Persistent loan-name alias map, manageable from Telegram via /loan merge.

Why this exists:
  Whisper occasionally mistranscribes the same person two different ways
  (e.g. "iqbal" once, "ikbal" the next time). Without this, every
  mistranscription becomes a separate loan bucket and the user is left
  to either ignore it or ask an engineer to merge them.

  The alias map lets the bot owner say "ikbal means iqbal" from inside
  Telegram, and the alias is persisted to data/loan_aliases.json. All
  future entries with `loan_name=ikbal` are written as `loan--iqbal`,
  and the rewrite step pulls historical entries' tags forward too so
  /loan shows one clean bucket.

  Symmetric to allowlist.py: file-backed, write-guarded with asyncio.Lock,
  atomic replace, picked up at startup, mutable at runtime.

Map shape on disk:
    {"aliases": {"ikbal": "iqbal", "rohim": "rahim", ...}}

Semantics:
  - Apply on write: log_entries normalises loan_name via canonical()
    before composing the loan--<slug> tag.
  - Apply on read: cmd_loan's _loan_name_from_tags also normalises so
    legacy entries written before the alias was set still collapse to
    the canonical bucket.
  - Transitive: if a→b and b→c, then canonical(a) returns c. Loops are
    broken by visiting each name at most once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


_KEEP = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def normalise_loan_slug(raw: str) -> str:
    """Same rules as parser._normalize_loan_name. Kept here to break the
    parser↔aliases dependency cycle and so /loan merge can normalise input."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    s = "-".join(s.split())
    s = "".join(c for c in s if c in _KEEP)
    return s.strip("-")


class LoanAliases:
    def __init__(self, json_path: Path) -> None:
        self._json_path = json_path
        self._lock = asyncio.Lock()
        self._aliases: dict[str, str] = self._load()
        logger.info(
            "LoanAliases initialised: %d aliases (path=%s)",
            len(self._aliases), self._json_path,
        )

    def _load(self) -> dict[str, str]:
        if not self._json_path.exists():
            return {}
        try:
            data = json.loads(self._json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Could not read %s (%s); starting with empty alias map",
                self._json_path, exc,
            )
            return {}
        raw = data.get("aliases") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            ks, vs = normalise_loan_slug(str(k)), normalise_loan_slug(str(v))
            if ks and vs and ks != vs:
                out[ks] = vs
        return out

    def _save(self) -> None:
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._json_path.with_suffix(self._json_path.suffix + ".tmp")
        payload = {"aliases": dict(sorted(self._aliases.items()))}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._json_path)

    # ---- read API ---------------------------------------------------------

    def canonical(self, name: str) -> str:
        """Resolve a name through the alias chain. Returns the canonical
        form. Safe for unknown names (returns input slug)."""
        slug = normalise_loan_slug(name)
        if not slug:
            return ""
        # Walk the chain, capped to avoid loops.
        seen: set[str] = set()
        cur = slug
        while cur in self._aliases and cur not in seen:
            seen.add(cur)
            cur = self._aliases[cur]
        return cur

    def all_aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    # ---- write API --------------------------------------------------------

    async def add(self, src: str, dst: str) -> tuple[str, str, str]:
        """Set `src → dst`. Returns (src_slug, dst_slug, status) where
        status ∈ {'added', 'updated', 'rejected-self', 'rejected-loop'}.
        """
        src_s = normalise_loan_slug(src)
        dst_s = normalise_loan_slug(dst)
        if not src_s or not dst_s:
            return src_s, dst_s, "rejected-empty"
        if src_s == dst_s:
            return src_s, dst_s, "rejected-self"
        # Reject if adding src→dst would close a loop (dst chains back to src).
        cur = dst_s
        seen = {src_s}
        depth = 0
        while cur in self._aliases and depth < 50:
            if cur == src_s:
                return src_s, dst_s, "rejected-loop"
            seen.add(cur)
            cur = self._aliases[cur]
            depth += 1
        async with self._lock:
            existed = src_s in self._aliases
            self._aliases[src_s] = dst_s
            self._save()
            logger.info("LoanAlias: %s → %s (%s)", src_s, dst_s, "updated" if existed else "added")
            return src_s, dst_s, ("updated" if existed else "added")

    async def remove(self, src: str) -> str:
        """Returns 'removed' | 'not-found'."""
        src_s = normalise_loan_slug(src)
        if not src_s:
            return "not-found"
        async with self._lock:
            if src_s not in self._aliases:
                return "not-found"
            del self._aliases[src_s]
            self._save()
            logger.info("LoanAlias: removed %s", src_s)
            return "removed"
