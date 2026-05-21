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
    get_allowlist,
    get_owl,
    get_settings,
    is_admin,
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
    "Type /help to see every command and the dashboard link."
)


def _build_help_text(
    dashboard_url: str,
    *,
    is_admin_viewer: bool,
) -> str:
    """Compose the /help message. Hides admin commands from non-admins."""
    lines = [
        "🤖 *Voice Expense Tracker — Help*",
        "",
        "Send me a *voice note*, *text*, or *photo of a receipt* and I'll log it.",
        "Each entry is tagged with your name; filter by tag on the dashboard.",
        "",
    ]
    if dashboard_url:
        lines.append(f"📊 *Dashboard*: {dashboard_url}")
        lines.append("")
    lines.extend(
        [
            "*Examples:*",
            "• Voice: _\"coffee 200 and uber 280\"_",
            "• Voice (Banglish): _\"burger saare paach sho taka\"_",
            "• Text: `lunch 350`",
            "• Photo: snap a receipt",
            "",
            "*Commands:*",
            "/start — quick welcome",
            "/help — this message",
            "/today — your spend today (`/today all` for everyone)",
            "/month — your month by category (`/month all` for everyone)",
            "/categories — list valid categories",
            "/undo — delete your last logged entry",
        ]
    )
    if is_admin_viewer:
        lines.extend(
            [
                "",
                "*Admin (you only):*",
                "/allow `<user_id>` — add a Telegram user to the allowlist",
                "/revoke `<user_id>` — remove a dynamically-added user",
                "/users — show the current allowlist",
            ]
        )
    return "\n".join(lines)


def _wants_all_view(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the command was invoked like '/today all'."""
    args = getattr(context, "args", None) or []
    return any(a.strip().lower() == "all" for a in args)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorised(update, context):
        return
    await update.effective_message.reply_markdown(WELCOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorised(update, context):
        return
    settings = get_settings(context)
    text = _build_help_text(
        settings.dashboard_url,
        is_admin_viewer=is_admin(update, context),
    )
    await update.effective_message.reply_markdown(text)


async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorised(update, context):
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
    if not is_authorised(update, context):
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
    if not is_authorised(update, context):
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
    if not is_authorised(update, context):
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


# ---------------------------------------------------------------------------
# Admin commands: /allow /revoke /users
# Add or remove Telegram user ids from the bot's allowlist at runtime.
# Only users in ADMIN_TELEGRAM_USER_IDS (or the static ALLOWED_TELEGRAM_USER_IDS
# if that env is unset) can run these.
# ---------------------------------------------------------------------------


def _parse_id_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    args = getattr(context, "args", None) or []
    if not args:
        return None
    try:
        return int(args[0].strip())
    except ValueError:
        return None


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_admin(update, context):
        await msg.reply_text("⛔ Admin-only command.")
        return
    target = _parse_id_arg(context)
    if target is None:
        await msg.reply_markdown(
            "Usage: `/allow <telegram_user_id>`\n"
            "Get an id from @userinfobot."
        )
        return
    allowlist = get_allowlist(context)
    result = await allowlist.add(target)
    if result == "added":
        await msg.reply_text(f"✅ Added user {target} to allowlist.")
    elif result == "already-static":
        await msg.reply_text(f"User {target} is already in the static (.env) allowlist.")
    elif result == "already-dynamic":
        await msg.reply_text(f"User {target} is already allowed.")


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_admin(update, context):
        await msg.reply_text("⛔ Admin-only command.")
        return
    target = _parse_id_arg(context)
    if target is None:
        await msg.reply_markdown("Usage: `/revoke <telegram_user_id>`")
        return
    allowlist = get_allowlist(context)
    result = await allowlist.remove(target)
    if result == "removed":
        await msg.reply_text(f"↩️ Removed user {target} from allowlist.")
    elif result == "static-cannot-remove":
        await msg.reply_text(
            f"User {target} is in the static (.env) allowlist. "
            f"Edit .env and rebuild to remove."
        )
    elif result == "not-found":
        await msg.reply_text(f"User {target} is not on the allowlist.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_admin(update, context):
        await msg.reply_text("⛔ Admin-only command.")
        return
    allowlist = get_allowlist(context)
    static_ids = sorted(allowlist.static_ids())
    dynamic_ids = sorted(allowlist.dynamic_ids())
    if not static_ids and not dynamic_ids:
        await msg.reply_text(
            "⚠️ Allowlist is empty — bot is OPEN to anyone. "
            "Add yourself with /allow before sharing."
        )
        return
    lines = ["*Allowlist:*"]
    if static_ids:
        lines.append("\n_Static (from .env, cannot revoke at runtime):_")
        for uid in static_ids:
            lines.append(f"• `{uid}`")
    if dynamic_ids:
        lines.append("\n_Dynamic (added via /allow, can /revoke):_")
        for uid in dynamic_ids:
            lines.append(f"• `{uid}`")
    await msg.reply_markdown("\n".join(lines))
