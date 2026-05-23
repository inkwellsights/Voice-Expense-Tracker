"""Slash-command handlers."""
from __future__ import annotations

import html
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
            "/report — daily ledger for the running month",
            "/loan — per-loan tracker: taken, repaid, outstanding",
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


def _entry_local_date(exp: dict[str, Any]):
    when = parse_expense_date(exp.get("date"))
    if not when:
        return None
    return when.astimezone(TIMEZONE).date()


# Box-drawing primitives (U+2500 range — single-cell width in every
# Telegram monospace font we tested). All rendered inside <pre> blocks
# so the alignment holds on both mobile and desktop.
_BX = {
    "h": "─", "v": "│",
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
    "tm": "┬", "bm": "┴", "lm": "├", "rm": "┤", "x": "┼",
}


def _truncate(s: str, width: int) -> str:
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)] + "…"


def _cell_amount(amount: float) -> str:
    """Compact integer-with-commas, or — for zero. No currency symbol."""
    if not amount:
        return "—"
    a = abs(float(amount))
    if a.is_integer():
        return f"{int(a):,}"
    return f"{a:,.2f}"


# ---- /report daily-ledger table layout ----
# Column widths chosen so the boxed table is 36 chars wide total — fits
# every phone we've tested in portrait monospace. Bump _COL_ITEM if your
# day descriptions regularly truncate.
_COL_DATE, _COL_ITEM, _COL_OUT, _COL_IN = 5, 12, 7, 7
_BOX_W = 1 + _COL_DATE + 1 + _COL_ITEM + 1 + _COL_OUT + 1 + _COL_IN + 1  # = 36


def _box_border(left: str, mid: str, right: str) -> str:
    """One border line (top / middle-divider / bottom) for the /report table."""
    return (
        left
        + _BX["h"] * _COL_DATE + mid
        + _BX["h"] * _COL_ITEM + mid
        + _BX["h"] * _COL_OUT + mid
        + _BX["h"] * _COL_IN + right
    )


def _box_row(date: str, item: str, out: str, in_: str) -> str:
    v = _BX["v"]
    return (
        v + date.ljust(_COL_DATE)
        + v + item.ljust(_COL_ITEM)
        + v + out.rjust(_COL_OUT)
        + v + in_.rjust(_COL_IN)
        + v
    )


def _daily_row(date_obj, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse one day's entries into a single Date | Item | Out | In row.

    Loans fold into the In / Out columns by cash direction (loan_taken
    counts as money in, loan_repaid as money out), per the 4-column
    spec — the user shouldn't have to scan past extra rows to read the
    daily impact.
    """
    day_in = sum(
        float(e.get("amount") or 0)
        for e in entries
        if float(e.get("amount") or 0) > 0
    )
    day_out = sum(
        -float(e.get("amount") or 0)
        for e in entries
        if float(e.get("amount") or 0) < 0
    )
    # Most-significant single item by absolute amount, then "+N" if more.
    top = max(entries, key=lambda e: abs(float(e.get("amount") or 0)))
    top_name = str(top.get("name") or "?")
    extras = len(entries) - 1
    label = f"{top_name} +{extras}" if extras else top_name
    return {
        "date": date_obj.strftime("%d/%m"),
        "item": _truncate(label, _COL_ITEM),
        "out": day_out,
        "in": day_in,
    }


# ---------------------------------------------------------------------------
# /loan — per-loan tracker.
#
# Reads each entry's tags to bucket it:
#   • tag 'loan-taken'  → flow="taken"
#   • tag 'loan-repaid' → flow="repaid"
#   • tag 'loan:<slug>' → bucket key (else "" = unnamed bucket)
#
# Per loan, shows totals (taken / repaid / outstanding) and the last
# few events. Loans are sorted by outstanding descending so anything
# with money still owed surfaces first; settled loans drop to the
# bottom. Caller-only by default; `/loan all` aggregates households.
# ---------------------------------------------------------------------------

_LOAN_NAME_TAG_PREFIX = "loan:"


def _loan_name_from_tags(exp: dict[str, Any]) -> str:
    for raw in (exp.get("tags") or []):
        s = str(raw)
        if s.startswith(_LOAN_NAME_TAG_PREFIX):
            return s[len(_LOAN_NAME_TAG_PREFIX):]
    return ""  # belongs to the unnamed bucket


def _classify_loan_entry(exp: dict[str, Any]) -> tuple[str, str, float] | None:
    """Return (flow, loan_name, magnitude) or None for non-loan entries."""
    tags_lower = {str(t).strip().lower() for t in (exp.get("tags") or [])}
    amt = float(exp.get("amount") or 0)
    if "loan-taken" in tags_lower:
        return "taken", _loan_name_from_tags(exp), abs(amt)
    if "loan-repaid" in tags_lower:
        return "repaid", _loan_name_from_tags(exp), abs(amt)
    return None


def _loan_display_name(slug: str) -> str:
    if not slug:
        return "(UNNAMED)"
    # bike-loan → Bike Loan
    return slug.replace("-", " ").upper()


def _format_loan_amount(amount: float, currency: str) -> str:
    if amount <= 0:
        return "—"
    return f"{currency}{int(round(amount)):,}"


# /loan boxed-section width — matched to /report so both commands feel
# like part of the same UI. Inside the box, content gets 1 char of
# horizontal padding on each side, so usable width = _BOX_W − 4.
_LOAN_BOX_W = _BOX_W           # 36 chars wall-to-wall
_LOAN_CONTENT_W = _LOAN_BOX_W - 4   # 32 chars of usable space inside


def _loan_box_h(left: str, right: str) -> str:
    return left + _BX["h"] * (_LOAN_BOX_W - 2) + right


def _loan_box_line(content: str) -> str:
    """One body line of a loan box. Content gets clipped if too long."""
    text = _truncate(content, _LOAN_CONTENT_W).ljust(_LOAN_CONTENT_W)
    return _BX["v"] + " " + text + " " + _BX["v"]


def _loan_pair_line(label: str, value: str) -> str:
    """Two-column-ish row inside a loan box: label left, value right."""
    space = _LOAN_CONTENT_W - len(label) - len(value)
    if space < 1:
        space = 1
    return _loan_box_line(label + " " * space + value)


async def cmd_loan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"taken": 0.0, "repaid": 0.0, "events": []}
    )
    for e in own:
        cls = _classify_loan_entry(e)
        if not cls:
            continue
        flow, name, magnitude = cls
        b = buckets[name]
        b[flow] += magnitude
        b["events"].append((e, flow, magnitude))

    cur = settings.currency_symbol
    scope_name = "everyone" if show_all else tag

    if not buckets:
        prefix = "for everyone" if show_all else f"for {tag}"
        await update.effective_message.reply_text(
            f"No loans tracked {prefix} yet. "
            f"Say something like \"borrowed 5000 from rahim\" to start tracking."
        )
        return

    def sort_key(name: str) -> tuple[float, str]:
        b = buckets[name]
        outstanding = b["taken"] - b["repaid"]
        # Active loans first (outstanding > 0), settled at bottom.
        # Within active, descending by outstanding so the biggest debt is on top.
        return (-outstanding, name)

    sections: list[str] = [f"🏦 Loans · {scope_name}", ""]
    total_outstanding = 0.0

    for name in sorted(buckets.keys(), key=sort_key):
        b = buckets[name]
        outstanding = b["taken"] - b["repaid"]
        total_outstanding += outstanding

        header_status = (
            "settled ✓" if outstanding <= 0
            else f"{cur}{int(round(outstanding)):,} left"
        )
        header_label = _loan_display_name(name)

        sections.append(_loan_box_h(_BX["tl"], _BX["tr"]))
        sections.append(_loan_pair_line(header_label, f"({header_status})"))
        sections.append(_loan_box_h(_BX["lm"], _BX["rm"]))
        sections.append(_loan_pair_line("Taken", f"{cur}{int(round(b['taken'])):,}"))
        sections.append(_loan_pair_line("Repaid", f"{cur}{int(round(b['repaid'])):,}"))
        sections.append(_loan_pair_line("Out", _format_loan_amount(outstanding, cur)))

        # Recent events, oldest first within the last-5 window so the
        # ledger reads naturally top-to-bottom in chronological order.
        events_by_date = sorted(
            b["events"],
            key=lambda x: parse_expense_date(x[0].get("date")) or datetime.min.replace(tzinfo=TIMEZONE),
            reverse=True,
        )[:5]
        if events_by_date:
            sections.append(_loan_box_h(_BX["lm"], _BX["rm"]))
            sections.append(_loan_box_line("Recent:"))
            # Description width chosen so the date(5) + 2-space + desc + 1-space + amount(7)
            # fits inside _LOAN_CONTENT_W (32). 5 + 2 + N + 1 + 7 = 32 → N = 17.
            desc_w = _LOAN_CONTENT_W - 5 - 2 - 1 - 7
            for e, flow, magnitude in reversed(events_by_date):
                d = _entry_local_date(e)
                date_str = d.strftime("%d/%m") if d else "?"
                sign = "+" if flow == "taken" else "−"
                desc = _truncate(str(e.get("name") or "?"), desc_w)
                amount_str = f"{sign}{int(round(magnitude)):,}"
                line = f"{date_str}  {desc:<{desc_w}} {amount_str:>7}"
                sections.append(_loan_box_line(line))
        sections.append(_loan_box_h(_BX["bl"], _BX["br"]))
        sections.append("")  # gap between loan blocks

    # Strip trailing blank line before footer
    while sections and sections[-1] == "":
        sections.pop()
    sections.append("")
    sections.append(_BX["h"] * _LOAN_BOX_W)
    sections.append(
        f"TOTAL OUTSTANDING: {cur}{int(round(total_outstanding)):,}"
    )

    body = "\n".join(sections).rstrip()
    await update.effective_message.reply_html(f"<pre>{html.escape(body)}</pre>")


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

    # Group month entries by local date so we can collapse each day
    # into a single row.
    by_date: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for e in month_entries:
        d = _entry_local_date(e)
        if d is not None:
            by_date[d].append(e)

    daily = [_daily_row(d, by_date[d]) for d in sorted(by_date.keys())]
    total_out = sum(r["out"] for r in daily)
    total_in = sum(r["in"] for r in daily)

    now = datetime.now(TIMEZONE)
    scope_name = "everyone" if show_all else tag
    cur = settings.currency_symbol

    body_lines: list[str] = [
        f"{now.strftime('%B %Y')} · {scope_name}",
        "",
        _box_border(_BX["tl"], _BX["tm"], _BX["tr"]),
        _box_row("Date", "Item", "Out", "In"),
        _box_border(_BX["lm"], _BX["x"], _BX["rm"]),
    ]
    if daily:
        for r in daily:
            body_lines.append(
                _box_row(
                    r["date"], r["item"],
                    _cell_amount(r["out"]), _cell_amount(r["in"]),
                )
            )
        body_lines.append(_box_border(_BX["lm"], _BX["x"], _BX["rm"]))
        body_lines.append(
            _box_row(
                "TOTAL", "",
                _cell_amount(total_out), _cell_amount(total_in),
            )
        )
        body_lines.append(_box_border(_BX["bl"], _BX["bm"], _BX["br"]))
    else:
        # Empty-month bottom: just close the table; no TOTAL row needed.
        body_lines.append(
            _box_row("", "(no entries yet)", "", "")
        )
        body_lines.append(_box_border(_BX["bl"], _BX["bm"], _BX["br"]))

    # Below the table: loans + outstanding + net so the user can read
    # the "what's happening with loans" story without re-summing rows.
    month_buckets = _sum_buckets(month_entries)
    life = _sum_buckets(own)
    outstanding = life["loan_taken"] - life["loan_repaid"]
    month_net = total_in - total_out

    body_lines.append("")
    body_lines.append(f"amounts in {cur}")
    sign = "+" if month_net >= 0 else "−"
    body_lines.append(f"Net this month: {sign}{cur}{abs(int(month_net)):,}")
    if month_buckets["loan_taken"] or month_buckets["loan_repaid"]:
        body_lines.append(
            f"Loans this month: in {cur}{int(month_buckets['loan_taken']):,} · "
            f"out {cur}{int(month_buckets['loan_repaid']):,}"
        )
    if outstanding:
        body_lines.append(f"Outstanding loan: {cur}{int(outstanding):,}")

    body = "\n".join(body_lines)
    # <pre> renders monospace on Telegram so the columns line up.
    # Escape because tag values could contain <, >, & in pathological cases.
    await update.effective_message.reply_html(f"<pre>{html.escape(body)}</pre>")


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
