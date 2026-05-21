"""Slash-command handlers."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..config import CATEGORIES, TIMEZONE
from ..services.expenseowl import ExpenseOwlError, parse_expense_date
from .common import (
    expense_has_tag,
    format_amount,
    get_owl,
    get_settings,
    is_authorised,
    user_tag,
)

logger = logging.getLogger(__name__)


WELCOME = (
    "👋 *Voice Expense Tracker*\n\n"
    "Send me a *voice note*, *text*, or *photo of a receipt* and I'll log it.\n\n"
    "Examples:\n"
    "• Voice: \"lunch 350 and uber 280\" (English, Bangla, or both)\n"
    "• Text: `coffee 120, groceries 1500`\n"
    "• Photo: snap a receipt\n\n"
    "Commands:\n"
    "/today — your spend today (add `all` for everyone)\n"
    "/month — your spend this month (add `all` for everyone)\n"
    "/categories — available categories\n"
    "/undo — delete your last logged expense\n\n"
    "_Each entry is tagged with your name on the shared dashboard — "
    "filter by your tag to see only your own spend._"
)


def _wants_all_view(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the command was invoked like '/today all'."""
    args = getattr(context, "args", None) or []
    return any(a.strip().lower() == "all" for a in args)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorised(update, get_settings(context)):
        return
    await update.effective_message.reply_markdown(WELCOME)


async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorised(update, get_settings(context)):
        return
    listing = "\n".join(f"• {c}" for c in CATEGORIES)
    await update.effective_message.reply_text(f"Categories:\n{listing}")


async def _fetch_expenses(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    return await get_owl(context).list_all()


def _filter_today(expenses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    today = datetime.now(TIMEZONE).date()
    out = []
    for exp in expenses:
        when = parse_expense_date(exp.get("date"))
        if when and when.astimezone(TIMEZONE).date() == today:
            out.append(exp)
    return out


def _filter_month(expenses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(TIMEZONE)
    out = []
    for exp in expenses:
        when = parse_expense_date(exp.get("date"))
        if not when:
            continue
        local = when.astimezone(TIMEZONE)
        if local.year == now.year and local.month == now.month:
            out.append(exp)
    return out


def _owner_label(exp: dict[str, Any]) -> str:
    """First tag wins as the 'owner' for per-person grouping in /month all."""
    tags = exp.get("tags") or []
    if isinstance(tags, list) and tags:
        return str(tags[0])
    return "?"


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_authorised(update, settings):
        return
    try:
        expenses = _filter_today(await _fetch_expenses(context))
    except ExpenseOwlError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return

    show_all = _wants_all_view(context)
    tag = user_tag(update, settings)
    if not show_all:
        expenses = [e for e in expenses if expense_has_tag(e, tag)]

    if not expenses:
        scope = "today" if show_all else f"for {tag} today"
        await update.effective_message.reply_text(f"No expenses logged {scope}. 🎉")
        return

    total = sum(float(e.get("amount") or 0) for e in expenses)
    header = "📅 *Today — everyone*" if show_all else f"📅 *Today — {tag}*"
    lines = [f"{header} ({len(expenses)} expense(s))\n"]
    for exp in expenses:
        amount = float(exp.get("amount") or 0)
        name = str(exp.get("name") or "?")
        if show_all:
            owner = _owner_label(exp)
            lines.append(
                f"• {format_amount(amount, settings.currency_symbol)} → "
                f"{exp.get('category', 'Other')} ({name}) — _{owner}_"
            )
        else:
            lines.append(
                f"• {format_amount(amount, settings.currency_symbol)} → "
                f"{exp.get('category', 'Other')} ({name})"
            )
    lines.append(f"\n*Total:* {format_amount(total, settings.currency_symbol)}")
    if not show_all:
        lines.append("_Tip: send `/today all` for the shared view._")
    await update.effective_message.reply_markdown("\n".join(lines))


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_authorised(update, settings):
        return
    try:
        expenses = _filter_month(await _fetch_expenses(context))
    except ExpenseOwlError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return

    show_all = _wants_all_view(context)
    tag = user_tag(update, settings)
    if not show_all:
        expenses = [e for e in expenses if expense_has_tag(e, tag)]

    if not expenses:
        scope = "this month" if show_all else f"for {tag} this month"
        await update.effective_message.reply_text(f"Nothing logged {scope} yet.")
        return

    by_category: dict[str, float] = defaultdict(float)
    by_person: dict[str, float] = defaultdict(float)
    total = 0.0
    for exp in expenses:
        amount = float(exp.get("amount") or 0)
        by_category[exp.get("category", "Other")] += amount
        by_person[_owner_label(exp)] += amount
        total += amount

    now = datetime.now(TIMEZONE)
    header = (
        f"📊 *{now.strftime('%B %Y')} — everyone*"
        if show_all
        else f"📊 *{now.strftime('%B %Y')} — {tag}*"
    )
    lines = [f"{header} ({len(expenses)} expense(s))\n", "*By category:*"]
    for category in sorted(by_category, key=lambda c: abs(by_category[c]), reverse=True):
        pct = (by_category[category] / total * 100) if total else 0
        lines.append(
            f"• {category}: {format_amount(by_category[category], settings.currency_symbol)} "
            f"({pct:.0f}%)"
        )
    if show_all and len(by_person) > 1:
        lines.append("\n*By person:*")
        for person in sorted(by_person, key=lambda p: abs(by_person[p]), reverse=True):
            pct = (by_person[person] / total * 100) if total else 0
            lines.append(
                f"• {person}: {format_amount(by_person[person], settings.currency_symbol)} "
                f"({pct:.0f}%)"
            )
    lines.append(f"\n*Total:* {format_amount(total, settings.currency_symbol)}")
    if not show_all:
        lines.append("_Tip: send `/month all` for the shared view._")
    await update.effective_message.reply_markdown("\n".join(lines))


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_authorised(update, settings):
        return

    owl = get_owl(context)
    user = update.effective_user
    user_id = user.id if user else 0
    tag = user_tag(update, settings)

    try:
        expenses = await owl.list_all()
    except ExpenseOwlError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return
    if not expenses:
        await update.effective_message.reply_text("Nothing to undo.")
        return

    own_expenses = [e for e in expenses if expense_has_tag(e, tag)]
    if not own_expenses:
        await update.effective_message.reply_text(
            f"Nothing of yours to undo, {tag}."
        )
        return

    own_expenses.sort(
        key=lambda e: parse_expense_date(e.get("date")) or datetime.min.replace(tzinfo=TIMEZONE),
        reverse=True,
    )

    last_map = context.application.bot_data.get("last_expense_id") or {}
    hint_id = last_map.get(user_id)
    target = None
    if hint_id:
        target = next(
            (e for e in own_expenses if str(e.get("id") or e.get("ID")) == str(hint_id)),
            None,
        )
    if target is None:
        target = own_expenses[0]

    target_id = str(target.get("id") or target.get("ID") or "")
    if not target_id:
        await update.effective_message.reply_text(
            "❌ Could not figure out which expense to remove."
        )
        return

    try:
        await owl.delete(target_id)
    except ExpenseOwlError as exc:
        await update.effective_message.reply_text(f"❌ Delete failed: {exc}")
        return

    last_map.pop(user_id, None)
    summary = (
        f"{format_amount(float(target.get('amount') or 0), settings.currency_symbol)} "
        f"→ {target.get('category', 'Other')} ({target.get('name', '?')})"
    )
    await update.effective_message.reply_text(f"↩️ Removed: {summary}")
