"""Shared helpers used by every handler."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..config import TIMEZONE, Settings
from ..services.allowlist import Allowlist
from ..services.expenseowl import ExpenseOwl, ExpenseOwlError

logger = logging.getLogger(__name__)


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def get_owl(context: ContextTypes.DEFAULT_TYPE) -> ExpenseOwl:
    return context.application.bot_data["owl"]


def get_allowlist(context: ContextTypes.DEFAULT_TYPE) -> Allowlist:
    return context.application.bot_data["allowlist"]


def is_authorised(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the message sender is allowed to use the bot.

    Consults the dynamic allowlist (env-static + runtime-dynamic via /allow).
    If the combined allowlist is empty, the bot is open to anyone (useful
    for first-run setup).
    """
    allowlist = get_allowlist(context)
    if allowlist.is_open():
        return True
    user = update.effective_user
    return bool(user and user.id in allowlist)


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the sender can run /allow, /revoke, /users.

    Admin set: ADMIN_TELEGRAM_USER_IDS if non-empty, otherwise the static
    ALLOWED_TELEGRAM_USER_IDS (i.e. anyone in the env-listed allowlist).
    Dynamically-added users (via /allow) are NEVER admins.
    """
    settings = get_settings(context)
    admin_ids = settings.admin_user_ids or settings.allowed_user_ids
    if not admin_ids:
        # No admins configured at all → first /allow is from the open bot,
        # caller is whoever sent the first message. Don't accidentally make
        # everyone an admin; require explicit setup instead.
        return False
    user = update.effective_user
    return bool(user and user.id in admin_ids)


def user_tag(update: Update, settings: Settings) -> str:
    """Pick a short, human-readable tag for the message sender.

    Precedence:
      1. Explicit USER_TAGS override in .env
      2. Telegram first name
      3. Telegram @username
      4. Numeric user id (last-ditch fallback)
    """
    user = update.effective_user
    if user is None:
        return "anon"
    if user.id in settings.user_tags:
        return settings.user_tags[user.id]
    if user.first_name:
        return user.first_name.strip()
    if user.username:
        return user.username
    return str(user.id)


def expense_has_tag(exp: dict[str, Any], tag: str) -> bool:
    """Case-insensitive match against the expense's tags array."""
    tags = exp.get("tags") or []
    if not isinstance(tags, list):
        return False
    norm = tag.strip().lower()
    return any(str(t).strip().lower() == norm for t in tags)


def format_amount(amount: float, currency: str) -> str:
    # ExpenseOwl stores expenses as negative amounts. The bot only deals with
    # outflows, so always show the unsigned magnitude in user-facing replies.
    magnitude = abs(float(amount))
    if magnitude.is_integer():
        return f"{currency}{int(magnitude):,}"
    return f"{currency}{magnitude:,.2f}"


def format_confirmation(
    entries: list[dict[str, Any]],
    currency: str,
    *,
    default_context: str = "personal",
) -> str:
    # Bot logs everything in real-time, so a single header date+time applies
    # to the whole batch. Asia/Dhaka local time, matches the timestamps the
    # entries get stored with.
    stamp = datetime.now(TIMEZONE).strftime("%Y-%m-%d · %H:%M")
    lines = [f"✅ Logged · {stamp}"]
    exp_total = 0.0
    inc_total = 0.0
    for entry in entries:
        kind = entry.get("type", "expense")
        amt = float(entry["amount"])
        # Only surface the context tag when it's not the default — keeps
        # "personal" entries visually clean and makes overrides (MHUBEXP,
        # etc.) stand out at a glance.
        ctx = (entry.get("context") or "").strip()
        ctx_suffix = f" · {ctx}" if ctx and ctx != default_context else ""
        if kind == "income":
            inc_total += amt
            lines.append(
                f"• +{format_amount(amt, currency)} → {entry['category']} "
                f"({entry['name']}){ctx_suffix} 💰"
            )
        else:
            exp_total += amt
            lines.append(
                f"• {format_amount(amt, currency)} → {entry['category']} "
                f"({entry['name']}){ctx_suffix}"
            )
    if len(entries) > 1 or (exp_total and inc_total):
        if inc_total:
            lines.append(f"\nIn: +{format_amount(inc_total, currency)}")
        if exp_total:
            lines.append(f"Out: {format_amount(exp_total, currency)}")
        if exp_total and inc_total:
            net = inc_total - exp_total
            lines.append(
                f"Net: {'+' if net >= 0 else '-'}{format_amount(net, currency)}"
            )
    return "\n".join(lines)


async def log_entries(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    entries: list[dict[str, Any]],
    status_message=None,
) -> None:
    """Send entries to ExpenseOwl and reply to the user.

    If `status_message` is provided, edits it in place rather than sending a
    new reply — used by the voice handler to show a single status bubble that
    transitions from "🎧 Listening…" to the final confirmation.
    """
    settings = get_settings(context)
    owl = get_owl(context)

    async def respond(text: str) -> None:
        if status_message is not None:
            try:
                await status_message.edit_text(text)
                return
            except Exception:
                logger.debug("edit_text failed, falling back to reply", exc_info=True)
        await update.effective_message.reply_text(text)

    if not entries:
        await respond(
            "Hmm — I couldn't pull an expense out of that. "
            "Try something like 'lunch 350' or 'coffee 120, uber 280'."
        )
        return

    tag = user_tag(update, settings)
    user_id = update.effective_user.id if update.effective_user else 0

    logged: list[dict[str, Any]] = []
    last_id: str | None = None
    for entry in entries:
        # Tag every entry with [<who>, <context>]. Skip empties so a
        # missing context (legacy entries) doesn't write a "" tag.
        entry_tags = [t for t in (tag, entry.get("context") or "") if t]
        try:
            created = await owl.create(
                name=entry["name"],
                amount=entry["amount"],
                category=entry["category"],
                tags=entry_tags,
                kind=entry.get("type", "expense"),
            )
        except ExpenseOwlError as exc:
            await respond(f"❌ ExpenseOwl error: {exc}")
            return
        logged.append(entry)
        if isinstance(created, dict):
            last_id = str(created.get("id") or created.get("ID") or "") or last_id

    if last_id:
        last_map = context.application.bot_data.setdefault("last_expense_id", {})
        last_map[user_id] = last_id

    await respond(
        format_confirmation(
            logged,
            settings.currency_symbol,
            default_context=settings.context_default,
        )
    )
