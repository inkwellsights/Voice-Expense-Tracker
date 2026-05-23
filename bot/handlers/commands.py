"""Slash-command handlers."""
from __future__ import annotations

import csv
import html
import io
import logging
from collections import defaultdict
from datetime import datetime
from itertools import groupby
from typing import Any

from telegram import InputFile, Update
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
            "/report — month + lifetime breakdown with loans + net worth",
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


# ---------------------------------------------------------------------------
# /report — month + lifetime financial breakdown for the caller, with
# loans modelled as a separate funding pool.
#
# Entry classification (uses tags written by common.log_entries):
#   • "loan-taken"  tag  → loan_taken bucket (type=income)
#   • "loan-repaid" tag  → loan_repaid bucket (type=expense)
#   • otherwise sign of amount decides regular_income vs regular_expense
#
# Derived numbers:
#   outstanding_loan = lifetime loan_taken − lifetime loan_repaid
#   actual_balance   = lifetime (regular_income + loan_taken)
#                      − lifetime (regular_expense + loan_repaid)
#   net_worth        = actual_balance − outstanding_loan
#                      (≡ lifetime regular_income − regular_expense)
#   spent_from_loan  = min(lifetime regular_expense, lifetime loan_taken)
#                      — sub-budget interpretation: regular spend is
#                        considered loan-funded until the loan is exhausted.
# ---------------------------------------------------------------------------

_REPORT_BUCKETS = ("regular_income", "regular_expense", "loan_taken", "loan_repaid")


def _classify_entry(exp: dict[str, Any]) -> tuple[str, float]:
    tags_lower = {str(t).strip().lower() for t in (exp.get("tags") or [])}
    amt = float(exp.get("amount") or 0)
    if "loan-taken" in tags_lower:
        return "loan_taken", abs(amt)
    if "loan-repaid" in tags_lower:
        return "loan_repaid", abs(amt)
    if amt > 0:
        return "regular_income", amt
    if amt < 0:
        return "regular_expense", abs(amt)
    return "skip", 0.0


def _sum_buckets(entries: list[dict[str, Any]]) -> dict[str, float]:
    out = {b: 0.0 for b in _REPORT_BUCKETS}
    for exp in entries:
        bucket, magnitude = _classify_entry(exp)
        if bucket in out:
            out[bucket] += magnitude
    return out


def _is_loan_entry(exp: dict[str, Any]) -> bool:
    tags_lower = {str(t).strip().lower() for t in (exp.get("tags") or [])}
    return "loan-taken" in tags_lower or "loan-repaid" in tags_lower


def _entry_local_date(exp: dict[str, Any]):
    when = parse_expense_date(exp.get("date"))
    if not when:
        return None
    return when.astimezone(TIMEZONE).date()


def _build_ledger_rows(
    month_entries: list[dict[str, Any]],
    all_entries: list[dict[str, Any]],
    month_start_date,
) -> list[dict[str, Any]]:
    """Daily-total rows for regular activity + one row per loan event.

    Running balance threads through every row, starting from the lifetime
    balance just before `month_start_date` so the first row reflects where
    you actually stood entering the month.
    """
    # Opening balance: cumulative net of every entry dated strictly before
    # the start of the reported month. Loan-taken counts as +amount,
    # loan-repaid as -amount, regular by sign — same as actual_balance math.
    opening = 0.0
    for e in all_entries:
        d = _entry_local_date(e)
        if d and d < month_start_date:
            opening += float(e.get("amount") or 0)

    sorted_month = sorted(
        month_entries,
        key=lambda e: parse_expense_date(e.get("date")) or datetime.min.replace(tzinfo=TIMEZONE),
    )

    rows: list[dict[str, Any]] = []
    running = opening

    # Opening-balance pseudo-row so the CSV is self-describing.
    rows.append({
        "date": month_start_date.strftime("%Y-%m-%d"),
        "description": "(opening balance)",
        "category": "",
        "tags": "",
        "in": "",
        "out": "",
        "balance": running,
    })

    for date_obj, group_iter in groupby(sorted_month, key=_entry_local_date):
        if date_obj is None:
            continue
        group = list(group_iter)
        regular = [e for e in group if not _is_loan_entry(e)]
        loans = [e for e in group if _is_loan_entry(e)]

        # Daily total for regular activity (skip if none — keeps CSV terse).
        if regular:
            reg_in = sum(float(e.get("amount") or 0) for e in regular if float(e.get("amount") or 0) > 0)
            reg_out = sum(-float(e.get("amount") or 0) for e in regular if float(e.get("amount") or 0) < 0)
            running += reg_in - reg_out
            # Category column: list distinct categories that contributed,
            # so the daily row still gives a hint of where the spend went.
            cats = sorted({str(e.get("category") or "Other") for e in regular})
            rows.append({
                "date": date_obj.strftime("%Y-%m-%d"),
                "description": f"Daily total ({len(regular)} entr{'y' if len(regular) == 1 else 'ies'})",
                "category": ", ".join(cats),
                "tags": "",
                "in": reg_in if reg_in else "",
                "out": reg_out if reg_out else "",
                "balance": running,
            })

        for e in loans:
            amt = float(e.get("amount") or 0)
            in_ = abs(amt) if amt > 0 else 0.0
            out_ = abs(amt) if amt < 0 else 0.0
            running += in_ - out_
            tags = e.get("tags") or []
            rows.append({
                "date": date_obj.strftime("%Y-%m-%d"),
                "description": str(e.get("name") or "?"),
                "category": str(e.get("category") or "Other"),
                "tags": ", ".join(str(t) for t in tags),
                "in": in_ if in_ else "",
                "out": out_ if out_ else "",
                "balance": running,
            })

    return rows


def _ledger_to_csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Serialize ledger rows as UTF-8 CSV with a BOM (Excel reads ৳ correctly)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Description", "Category", "Tags", "In", "Out", "Running balance"])
    for r in rows:
        writer.writerow([
            r["date"],
            r["description"],
            r["category"],
            r["tags"],
            r["in"],
            r["out"],
            r["balance"],
        ])
    # ﻿ BOM helps Excel auto-detect UTF-8 and render Bangla / ৳ properly.
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _build_summary_caption(
    month: dict[str, float],
    life: dict[str, float],
    currency: str,
    *,
    title: str,
) -> str:
    """Short monospace summary used as the CSV attachment's caption."""

    def fmt(amount: float, signed: bool = False) -> str:
        s = format_amount(abs(amount), currency)
        if signed:
            return ("+" if amount >= 0 else "−") + s
        return s

    outstanding = life["loan_taken"] - life["loan_repaid"]
    actual_balance = (
        life["regular_income"] + life["loan_taken"]
        - life["regular_expense"] - life["loan_repaid"]
    )
    net_worth = actual_balance - outstanding
    month_net = (
        month["regular_income"] + month["loan_taken"]
        - month["regular_expense"] - month["loan_repaid"]
    )

    label_w, value_w = 19, 12

    def row(label: str, value: str) -> str:
        return f"  {label.ljust(label_w)}{value.rjust(value_w)}"

    def div() -> str:
        return " " * (label_w + 2) + "─" * value_w

    lines = [
        title,
        "",
        "THIS MONTH",
        row("Regular in", fmt(month["regular_income"])),
        row("Regular out", fmt(month["regular_expense"])),
        row("Loans taken", fmt(month["loan_taken"])),
        row("Loans repaid", fmt(month["loan_repaid"])),
        div(),
        row("Net", fmt(month_net, signed=True)),
        "",
        "LIFETIME LOANS",
        row("Outstanding", fmt(outstanding)),
        "",
        row("Net worth", fmt(net_worth, signed=True)),
    ]
    return "\n".join(lines)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_authorised(update, context):
        return
    try:
        all_expenses = await _fetch_expenses(context)
    except ExpenseOwlError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return

    show_all = _wants_all_view(context)
    tag = user_tag(update, settings)
    own = (
        all_expenses if show_all
        else [e for e in all_expenses if expense_has_tag(e, tag)]
    )

    if not own:
        scope = "yet" if show_all else f"for {tag} yet"
        await update.effective_message.reply_text(f"Nothing logged {scope}.")
        return

    month_entries = _filter_month(own)
    month = _sum_buckets(month_entries)
    life = _sum_buckets(own)

    now = datetime.now(TIMEZONE)
    scope_name = "everyone" if show_all else tag
    title = f"Report — {now.strftime('%B %Y')} · {scope_name}"

    # Build CSV ledger; running balance opens with the lifetime balance
    # as of the day BEFORE this month started (so each row reflects the
    # ongoing account state, not just month-to-date deltas).
    month_start = now.replace(day=1).date()
    rows = _build_ledger_rows(month_entries, own, month_start)
    csv_bytes = _ledger_to_csv_bytes(rows)

    caption_body = _build_summary_caption(
        month, life, settings.currency_symbol, title=title
    )
    caption = f"<pre>{html.escape(caption_body)}</pre>"

    file_scope = "everyone" if show_all else tag.lower().replace(" ", "-")
    filename = f"report-{now.strftime('%Y-%m')}-{file_scope}.csv"

    await update.effective_message.reply_document(
        document=InputFile(io.BytesIO(csv_bytes), filename=filename),
        caption=caption,
        parse_mode="HTML",
    )


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
