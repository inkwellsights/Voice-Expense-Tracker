"""Shared helpers used by every handler."""
from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..config import Settings
from ..services.expenseowl import ExpenseOwl, ExpenseOwlError

logger = logging.getLogger(__name__)


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def get_owl(context: ContextTypes.DEFAULT_TYPE) -> ExpenseOwl:
    return context.application.bot_data["owl"]


def is_authorised(update: Update, settings: Settings) -> bool:
    """Return True if the message sender is allowed to use the bot.

    If no allowlist is configured, the bot is open to anyone (useful for
    quick personal setups). Add ids to ALLOWED_TELEGRAM_USER_IDS to lock it down.
    """
    if not settings.allowed_user_ids:
        return True
    user = update.effective_user
    return bool(user and user.id in settings.allowed_user_ids)


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


def format_confirmation(entries: list[dict[str, Any]], currency: str) -> str:
    lines = ["✅ Logged:"]
    exp_total = 0.0
    inc_total = 0.0
    for entry in entries:
        kind = entry.get("type", "expense")
        amt = float(entry["amount"])
        if kind == "income":
            inc_total += amt
            lines.append(
                f"• +{format_amount(amt, currency)} → {entry['category']} "
                f"({entry['name']}) 💰"
            )
        else:
            exp_total += amt
            lines.append(
                f"• {format_amount(amt, currency)} → {entry['category']} "
                f"({entry['name']})"
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
        try:
            created = await owl.create(
                name=entry["name"],
                amount=entry["amount"],
                category=entry["category"],
                tags=[tag],
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

    await respond(format_confirmation(logged, settings.currency_symbol))
