"""Environment configuration loader."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

TIMEZONE = ZoneInfo("Asia/Dhaka")

# Separator placed between the user tag and the original description in
# ExpenseOwl. Picked to be visually distinct and unlikely to appear inside
# a real expense name, so the bot can split on it later.
USER_TAG_SEPARATOR = " · "

CATEGORIES = [
    "Food",
    "Transport",
    "Shopping",
    "Bills",
    "Health",
    "Entertainment",
    "Subscriptions",
    "Other",
]


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    groq_api_key: str
    gemini_api_key: str
    expenseowl_url: str
    # User-facing dashboard URL shown in /help and welcome messages.
    # Distinct from expenseowl_url which is the bot's internal API endpoint.
    # Set DASHBOARD_URL in .env (e.g. https://expenses.example.com). Optional.
    dashboard_url: str
    currency_symbol: str
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    # Admin allowlist for /allow, /revoke, /users commands. If empty,
    # falls back to allowed_user_ids (i.e. anyone in the static allowlist
    # is also an admin). Set ADMIN_TELEGRAM_USER_IDS in .env to override.
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)
    user_tags: dict[int, str] = field(default_factory=dict)
    # If true, voice notes go straight to Gemini (audio-in, JSON out) — one
    # API call, no Whisper. Falls back to Whisper+Gemini-text on failure.
    # Flip to false in .env to revert to the Whisper-first pipeline.
    use_audio_native_gemini: bool = True
    # Second tag attached to every logged entry alongside the user tag.
    # Canonical name when nothing specific is mentioned in the input
    # (e.g. "personal").
    context_default: str = "personal"
    # Canonical-context-name → list of accepted synonyms (lower-cased,
    # canonical name itself included). Lets Gemini hear sloppy variations
    # ("masnoonhub", "mhubexpress", "masnoon hub express") and the bot
    # collapse all of them to one canonical tag ("MHUBEXP").
    context_synonyms: dict[str, list[str]] = field(default_factory=dict)
    # Optional self-hosted Whisper endpoint (OpenAI-compatible).
    # When set, the bot tries this before Groq; on any connect failure /
    # timeout / 5xx the cloud Groq path runs instead. Empty = cloud-only.
    local_whisper_url: str = ""
    local_whisper_timeout: float = 8.0


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _parse_user_id_csv(env_var: str, *legacy_names: str) -> frozenset[int]:
    """Parse a CSV of Telegram user ids from an env var (or legacy fallbacks)."""
    raw = os.getenv(env_var, "")
    for legacy in legacy_names:
        if not raw:
            raw = os.getenv(legacy, "")
    raw = raw.strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            raise RuntimeError(
                f"{env_var} contains a non-integer value: {chunk!r}"
            )
    return frozenset(ids)


def _parse_user_ids() -> frozenset[int]:
    """Static allowlist from ALLOWED_TELEGRAM_USER_IDS (or legacy singular)."""
    return _parse_user_id_csv("ALLOWED_TELEGRAM_USER_IDS", "ALLOWED_TELEGRAM_USER_ID")


def _parse_admin_ids() -> frozenset[int]:
    """Admin allowlist from ADMIN_TELEGRAM_USER_IDS. Empty = falls back to allowed."""
    return _parse_user_id_csv("ADMIN_TELEGRAM_USER_IDS")


def _parse_user_tags() -> dict[int, str]:
    """Optional explicit map of user id → display tag.

    Format: USER_TAGS=12345:Saif,67890:Alice
    Used when Telegram first names collide or you want to override them.
    """
    raw = os.getenv("USER_TAGS", "").strip()
    if not raw:
        return {}
    out: dict[int, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        uid_raw, name = pair.split(":", 1)
        try:
            out[int(uid_raw.strip())] = name.strip()
        except ValueError:
            raise RuntimeError(f"USER_TAGS entry is malformed: {pair!r}")
    return out


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_context_synonyms() -> dict[str, list[str]]:
    """Parse TAG_SYNONYMS into {canonical: [synonym1, synonym2, ...]}.

    Format: TAG_SYNONYMS=MHUBEXP:masnoonhubexpress|mhubexpress|masnoonhub,family:familyfund|family fund
    The canonical name itself is implicitly included in its own synonym list
    (lowercased) so Gemini can return either form and we normalize correctly.
    """
    raw = os.getenv("TAG_SYNONYMS", "").strip()
    if not raw:
        return {}
    out: dict[str, list[str]] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        canon, syns_part = chunk.split(":", 1)
        canon = canon.strip()
        if not canon:
            continue
        syns = [s.strip().lower() for s in syns_part.split("|") if s.strip()]
        if canon.lower() not in syns:
            syns.append(canon.lower())
        # De-dup while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for s in syns:
            if s not in seen:
                unique.append(s)
                seen.add(s)
        out[canon] = unique
    return out


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        groq_api_key=_require("GROQ_API_KEY"),
        gemini_api_key=_require("GEMINI_API_KEY"),
        expenseowl_url=os.getenv("EXPENSEOWL_URL", "http://localhost:5006").rstrip("/"),
        dashboard_url=os.getenv("DASHBOARD_URL", "").strip().rstrip("/"),
        currency_symbol=os.getenv("CURRENCY_SYMBOL", "৳"),
        allowed_user_ids=_parse_user_ids(),
        admin_user_ids=_parse_admin_ids(),
        user_tags=_parse_user_tags(),
        use_audio_native_gemini=_parse_bool(
            os.getenv("USE_AUDIO_NATIVE_GEMINI"), default=True
        ),
        context_default=(os.getenv("TAG_DEFAULT", "personal").strip() or "personal"),
        context_synonyms=_parse_context_synonyms(),
        local_whisper_url=os.getenv("LOCAL_WHISPER_URL", "").strip().rstrip("/"),
        local_whisper_timeout=float(
            os.getenv("LOCAL_WHISPER_TIMEOUT_SECONDS", "8") or 8
        ),
    )
