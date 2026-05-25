"""User-managed ASR vocabulary corrections.

Why this exists:
  Gemini and Whisper repeatedly mistranscribe the same words for a
  specific user's voice ("Claude" -> "cloth", "vape" -> "vev",
  "rickshaw" -> "discover"). Bumping the system prompt for each one
  doesn't scale and requires a code change.

  This service is a persistent, Telegram-managed substitution map.
  Symmetric to LoanAliases (and the same on-disk + lock pattern):
    /vocab add <wrong> <right>
    /vocab remove <wrong>
    /vocab list

  Substitution is applied with WORD BOUNDARIES and CASE INSENSITIVE,
  so "cloth" becomes "Claude" but "clothing" is left alone. Multi-key
  matches use longest-first to avoid partial overlap.

Where it applies:
  - Entry name (handler.common.log_entries)
  - Loan name (before /loan merge canonicalisation)
  - Heard text (so the 🎤 confirmation line shows the corrected version)

Map shape on disk:
    {"aliases": {"cloth": "Claude", "vev": "vape", ...}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class Vocabulary:
    def __init__(self, json_path: Path) -> None:
        self._json_path = json_path
        self._lock = asyncio.Lock()
        self._aliases: dict[str, str] = self._load()
        self._pattern: re.Pattern[str] | None = None
        self._lookup_lower: dict[str, str] = {}
        self._refresh_pattern()
        logger.info(
            "Vocabulary initialised: %d aliases (path=%s)",
            len(self._aliases), self._json_path,
        )

    def _load(self) -> dict[str, str]:
        if not self._json_path.exists():
            return {}
        try:
            data = json.loads(self._json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Could not read %s (%s); starting with empty vocab",
                self._json_path, exc,
            )
            return {}
        raw = data.get("aliases") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            ks = str(k).strip()
            vs = str(v).strip()
            if ks and vs:
                out[ks] = vs
        return out

    def _save(self) -> None:
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._json_path.with_suffix(self._json_path.suffix + ".tmp")
        payload = {"aliases": dict(sorted(self._aliases.items()))}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._json_path)

    def _refresh_pattern(self) -> None:
        if not self._aliases:
            self._pattern = None
            self._lookup_lower = {}
            return
        # Longest keys first so multi-word phrases ("loan from rahim")
        # win over single-word substrings ("loan") if both are aliased.
        keys = sorted(self._aliases.keys(), key=len, reverse=True)
        pattern_str = r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b"
        self._pattern = re.compile(pattern_str, re.IGNORECASE)
        self._lookup_lower = {k.lower(): v for k, v in self._aliases.items()}

    # ---- read API ---------------------------------------------------------

    def canonical(self, text: str) -> str:
        """Substitute alias keys in text with their replacements. Word
        boundaries, case insensitive. Safe for any input including
        empty strings and None-likes."""
        if not text or not self._pattern:
            return text or ""

        def _repl(m: re.Match[str]) -> str:
            return self._lookup_lower.get(m.group(1).lower(), m.group(1))

        return self._pattern.sub(_repl, text)

    def all_aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    # ---- write API --------------------------------------------------------

    async def add(self, wrong: str, right: str) -> str:
        """Returns 'added' | 'updated' | 'rejected-empty' | 'rejected-self'."""
        w = (wrong or "").strip()
        r = (right or "").strip()
        if not w or not r:
            return "rejected-empty"
        if w.lower() == r.lower():
            return "rejected-self"
        async with self._lock:
            # Drop any case-variant of the same key so the map stays clean.
            for k in list(self._aliases.keys()):
                if k.lower() == w.lower() and k != w:
                    del self._aliases[k]
            existed = w in self._aliases
            self._aliases[w] = r
            self._refresh_pattern()
            self._save()
            logger.info(
                "Vocab: %s -> %s (%s)", w, r, "updated" if existed else "added"
            )
            return "updated" if existed else "added"

    async def remove(self, wrong: str) -> str:
        """Returns 'removed' | 'not-found'."""
        w = (wrong or "").strip()
        if not w:
            return "not-found"
        async with self._lock:
            # Case-insensitive lookup since users won't remember casing.
            for k in list(self._aliases.keys()):
                if k.lower() == w.lower():
                    del self._aliases[k]
                    self._refresh_pattern()
                    self._save()
                    logger.info("Vocab: removed %s", k)
                    return "removed"
            return "not-found"
