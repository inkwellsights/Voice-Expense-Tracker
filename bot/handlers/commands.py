"""Slash-command handlers."""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..config import CATEGORIES, TIMEZONE
from ..services.expenseowl import ExpenseOwlError, parse_expense_date
from .common import (
    expense_has_tag,
    format_amount,
    get_allowlist,
    get_loan_aliases,
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
            "/loan — loan summary (you owe / owed to you / net)",
            "/loan `<name>` — full history for one counterparty",
            "/loan merge `<from>` `<to>` — collapse mistranscribed names "
            "(e.g. `/loan merge ikbal iqbal`)",
            "/loan aliases — list active merges",
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

_REPORT_BUCKETS = (
    "regular_income", "regular_expense",
    "loan_taken", "loan_repaid", "loan_given", "loan_received",
)


def _classify_entry(exp: dict[str, Any]) -> tuple[str, float]:
    """Classify entry into one of _REPORT_BUCKETS by tag, fallback to sign."""
    tags_lower = {str(t).strip().lower() for t in (exp.get("tags") or [])}
    amt = float(exp.get("amount") or 0)
    if "loan-taken" in tags_lower:
        return "loan_taken", abs(amt)
    if "loan-repaid" in tags_lower:
        return "loan_repaid", abs(amt)
    if "loan-given" in tags_lower:
        return "loan_given", abs(amt)
    if "loan-received" in tags_lower:
        return "loan_received", abs(amt)
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
# Whitespace-aligned columns, no vertical bars — box-drawing characters
# render with visible gaps in many Android monospace fonts (verified on
# user's phone 2026-05-24). Horizontal-line dividers (─, U+2500) render
# solid across fonts, so we still get a "Excel-ish" header underline +
# totals divider without depending on box-drawing alignment.
_COL_DATE, _COL_ITEM, _COL_OUT, _COL_IN = 5, 13, 7, 7
_REPORT_W = _COL_DATE + 1 + _COL_ITEM + 1 + _COL_OUT + 1 + _COL_IN  # = 35


def _table_line(date: str, item: str, out: str, in_: str) -> str:
    return (
        f"{date:<{_COL_DATE}} "
        f"{item:<{_COL_ITEM}} "
        f"{out:>{_COL_OUT}} "
        f"{in_:>{_COL_IN}}"
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
# /loan — bidirectional per-loan tracker.
#
# Each entry's tags classify it into one of four flow events:
#   • 'loan-taken'    → I borrowed (cash in, I owe them more)
#   • 'loan-repaid'   → I paid back (cash out, I owe them less)
#   • 'loan-given'    → I lent (cash out, they owe me more)
#   • 'loan-received' → they paid me back (cash in, they owe me less)
#
# Per-loan net = (taken - repaid) - (given - received). Positive = you
# owe; negative = owed to you; zero = settled.
#
# Layout follows Bloomberg/dashboard principles: headline summary at
# top-left, one compact row per loan (sorted by abs(outstanding) desc),
# settled loans hidden after SETTLED_LOAN_VISIBLE_DAYS (Splitwise: 30d).
# `/loan <name>` opens a full-history deep-dive for one counterparty.
# ---------------------------------------------------------------------------

_LOAN_NAME_TAG_PREFIX = "loan--"
_LOAN_NAME_LEGACY_PREFIX = "loan "  # entries written before 2026-05-25 used
                                    # 'loan:<slug>' which ExpenseOwl mangled
                                    # to 'loan <slug>'. Read both for back-compat.


def _loan_name_from_tags(exp: dict[str, Any]) -> str:
    for raw in (exp.get("tags") or []):
        s = str(raw)
        if s.startswith(_LOAN_NAME_TAG_PREFIX):
            return s[len(_LOAN_NAME_TAG_PREFIX):]
        if s.startswith(_LOAN_NAME_LEGACY_PREFIX) and not s.startswith("loan-"):
            return s[len(_LOAN_NAME_LEGACY_PREFIX):].strip()
    return ""  # belongs to the unnamed bucket


def _classify_loan_entry(exp: dict[str, Any]) -> tuple[str, str, float] | None:
    """Return (flow, loan_name, magnitude) or None for non-loan entries.

    flow is one of: "taken", "repaid", "given", "received".
    """
    tags_lower = {str(t).strip().lower() for t in (exp.get("tags") or [])}
    amt = float(exp.get("amount") or 0)
    if "loan-taken" in tags_lower:
        return "taken", _loan_name_from_tags(exp), abs(amt)
    if "loan-repaid" in tags_lower:
        return "repaid", _loan_name_from_tags(exp), abs(amt)
    if "loan-given" in tags_lower:
        return "given", _loan_name_from_tags(exp), abs(amt)
    if "loan-received" in tags_lower:
        return "received", _loan_name_from_tags(exp), abs(amt)
    return None


def _loan_display_name(slug: str) -> str:
    if not slug:
        return "Unnamed"
    # bike-loan → Bike Loan
    return slug.replace("-", " ").title()


def _format_money(amount: float, currency: str) -> str:
    """৳-prefixed integer with commas; '—' for zero/negative-as-empty."""
    a = abs(float(amount))
    if not a:
        return "—"
    return f"{currency}{int(round(a)):,}"


def _relative_time(when: datetime | None, now: datetime) -> str:
    """Human-readable age. "today", "1d", "2w", "3mo", "1y"."""
    if when is None:
        return "?"
    delta = now - when
    days = delta.days
    if days < 0:
        return "future"
    if days == 0:
        return "today"
    if days == 1:
        return "1d"
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


# /loan layout: whitespace-aligned. No box-drawing borders — they
# fragment in some Android monospace fonts (see /report comment above).
_LOAN_W = _REPORT_W + 1  # 36 — matches /report's visual rhythm


def _loan_pair(label: str, value: str, indent: int = 2) -> str:
    """Label left, value right-aligned to the full loan-section width."""
    available = _LOAN_W - indent
    gap = available - len(label) - len(value)
    if gap < 1:
        gap = 1
    return " " * indent + label + " " * gap + value


# Compact one-line-per-loan row: name | amount-right | last-seen-right.
_LOAN_LAST_COL = 6
_LOAN_AMT_COL = 12
_LOAN_NAME_COL = _LOAN_W - _LOAN_AMT_COL - _LOAN_LAST_COL - 2  # = 16


def _loan_row(name: str, amount_str: str, last_str: str) -> str:
    name_t = _truncate(name, _LOAN_NAME_COL)
    return (
        f"{name_t:<{_LOAN_NAME_COL}} "
        f"{amount_str:>{_LOAN_AMT_COL}} "
        f"{last_str:>{_LOAN_LAST_COL}}"
    )


def _build_loan_buckets(
    own: list[dict[str, Any]],
    *,
    aliases=None,
) -> dict[str, dict[str, Any]]:
    """Bucket entries by canonical loan name.

    When `aliases` is provided, the bucket key is `aliases.canonical(name)`
    so legacy entries written before an alias was set still collapse into
    the canonical bucket on read.
    """
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"taken": 0.0, "repaid": 0.0, "given": 0.0, "received": 0.0, "events": []}
    )
    for e in own:
        cls = _classify_loan_entry(e)
        if not cls:
            continue
        flow, name, magnitude = cls
        if aliases is not None and name:
            name = aliases.canonical(name)
        b = buckets[name]
        b[flow] += magnitude
        b["events"].append((e, flow, magnitude))
    return buckets


def _bucket_net(b: dict[str, Any]) -> float:
    """Positive = I owe this counterparty; negative = they owe me."""
    return (b["taken"] - b["repaid"]) - (b["given"] - b["received"])


def _bucket_last_event(b: dict[str, Any]) -> datetime | None:
    when_list = [
        parse_expense_date(e.get("date")) for (e, _, _) in b["events"]
    ]
    when_list = [w for w in when_list if w is not None]
    if not when_list:
        return None
    return max(when_list)


async def cmd_loan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/loan dispatcher:
      /loan                       summary view
      /loan all                   household scope
      /loan <name> [all]          deep-dive on one counterparty
      /loan merge <from> <to>     merge two loan buckets + persist alias
      /loan unmerge <name>        remove an existing alias
      /loan aliases               list current alias map
    """
    settings = get_settings(context)
    if not is_authorised(update, context):
        return

    args_lower = [a.strip().lower() for a in (getattr(context, "args", None) or [])]

    # Subcommand dispatch — handle these before fetching expenses so the
    # admin paths stay snappy.
    if args_lower and args_lower[0] == "merge":
        await _cmd_loan_merge(update, context, args_lower[1:])
        return
    if args_lower and args_lower[0] == "unmerge":
        await _cmd_loan_unmerge(update, context, args_lower[1:])
        return
    if args_lower and args_lower[0] == "aliases":
        await _cmd_loan_aliases(update, context)
        return

    try:
        all_expenses = await _fetch_expenses(context)
    except ExpenseOwlError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return

    show_all = "all" in args_lower
    name_args = [a for a in args_lower if a != "all"]

    tag = user_tag(update, settings)
    own = (
        all_expenses if show_all
        else [e for e in all_expenses if expense_has_tag(e, tag)]
    )
    aliases = get_loan_aliases(context)
    buckets = _build_loan_buckets(own, aliases=aliases)

    if not buckets:
        prefix = "for everyone" if show_all else f"for {tag}"
        await update.effective_message.reply_text(
            f"No loans tracked {prefix} yet. "
            f"Say something like \"borrowed 5000 from rahim\" or "
            f"\"lent 2000 to bashar\" to start tracking."
        )
        return

    if name_args:
        # Deep-dive on a single counterparty.
        await _render_loan_detail(
            update, settings, buckets, query=name_args[0], show_all=show_all,
        )
        return

    await _render_loan_summary(update, settings, buckets, show_all=show_all, tag=tag)


async def _cmd_loan_merge(
    update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]
) -> None:
    """`/loan merge <from> <to>` — rewrite tags + persist alias.

    Idempotent. Safe if <from> bucket is empty (just stores the alias for
    future writes). Refuses to create loops (a→b when b already → a).
    """
    msg = update.effective_message
    if len(args) < 2:
        await msg.reply_markdown(
            "Usage: `/loan merge <from> <to>`\n\n"
            "Example: `/loan merge ikbal iqbal`\n"
            "All existing `ikbal` entries get re-tagged as `iqbal`, and any "
            "future entry the bot logs as `ikbal` will auto-collapse to `iqbal`.\n\n"
            "See current aliases: `/loan aliases`"
        )
        return
    src_input, dst_input = args[0], args[1]
    aliases = get_loan_aliases(context)
    owl = get_owl(context)

    # Step 1: persist the alias (validates loops too).
    src_s, dst_s, status = await aliases.add(src_input, dst_input)
    if status == "rejected-self":
        await msg.reply_text(f"❌ Can't merge a name into itself: {src_s}")
        return
    if status == "rejected-loop":
        await msg.reply_text(
            f"❌ That would create an alias loop ({dst_s} already chains back to {src_s})."
        )
        return
    if status == "rejected-empty":
        await msg.reply_text("❌ Names must be non-empty after normalization.")
        return

    # Step 2: rewrite existing loan--<src_s> tags to loan--<dst_s>.
    # Touches every entry that has the source tag, even if it belongs to
    # a different user — aliases are global because the dashboard view is
    # global. Future per-user aliases would split this map by user_id.
    try:
        all_exp = await owl.list_all()
    except ExpenseOwlError as exc:
        await msg.reply_text(
            f"⚠️ Alias saved ({src_s} → {dst_s}), but couldn't fetch existing "
            f"entries to rewrite: {exc}"
        )
        return

    src_tag = f"loan--{src_s}"
    dst_tag = f"loan--{dst_s}"
    affected = [
        e for e in all_exp
        if any(str(t) == src_tag for t in (e.get("tags") or []))
    ]
    rewritten = 0
    failed = 0
    for e in affected:
        tags = list(e.get("tags") or [])
        new_tags = [dst_tag if str(t) == src_tag else str(t) for t in tags]
        eid = str(e.get("id") or "")
        if not eid:
            failed += 1
            continue
        try:
            await owl.edit(
                eid,
                name=str(e.get("name") or ""),
                amount=float(e.get("amount") or 0),
                category=str(e.get("category") or "Other"),
                date=str(e.get("date") or ""),
                tags=new_tags,
            )
            rewritten += 1
        except ExpenseOwlError as exc:
            logger.warning("Failed to rewrite %s: %s", eid, exc)
            failed += 1

    parts = [
        f"✅ Alias saved: `{src_s}` → `{dst_s}`",
        f"Rewrote {rewritten} existing entr{'y' if rewritten == 1 else 'ies'}.",
    ]
    if failed:
        parts.append(f"⚠️ {failed} entries failed to rewrite (see logs).")
    if status == "updated":
        parts.append("_(this replaced a previous alias)_")
    parts.append("\nUndo with `/loan unmerge " + src_s + "`")
    await msg.reply_markdown("\n".join(parts))


async def _cmd_loan_unmerge(
    update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]
) -> None:
    msg = update.effective_message
    if not args:
        await msg.reply_markdown(
            "Usage: `/loan unmerge <name>`\n"
            "Removes the alias so future entries with that name keep their own bucket. "
            "Does NOT rewrite already-rewritten historical entries — you'd need to do "
            "that manually if you want the old separation back."
        )
        return
    aliases = get_loan_aliases(context)
    src_s = args[0]
    result = await aliases.remove(src_s)
    if result == "not-found":
        await msg.reply_text(f"No alias for '{src_s}'.")
    else:
        from ..services.loan_aliases import normalise_loan_slug
        await msg.reply_text(f"↩️ Removed alias for '{normalise_loan_slug(src_s)}'.")


async def _cmd_loan_aliases(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    aliases = get_loan_aliases(context)
    m = aliases.all_aliases()
    if not m:
        await msg.reply_markdown(
            "No loan aliases set.\n\n"
            "Example: `/loan merge ikbal iqbal` collapses both into one bucket."
        )
        return
    lines = ["*Current loan aliases:*"]
    for src in sorted(m.keys()):
        lines.append(f"• `{src}` → `{m[src]}`")
    lines.append("\nRemove one with `/loan unmerge <name>`")
    await msg.reply_markdown("\n".join(lines))


async def _render_loan_summary(
    update: Update,
    settings,
    buckets: dict[str, dict[str, Any]],
    *,
    show_all: bool,
    tag: str,
) -> None:
    cur = settings.currency_symbol
    now = datetime.now(TIMEZONE)
    cutoff_days = settings.settled_loan_visible_days

    # Net per bucket; classify into owed-by-you, owed-to-you, or settled.
    items: list[tuple[str, float, datetime | None]] = []
    you_owe_total = 0.0
    owed_to_you_total = 0.0
    settled_recent: list[tuple[str, datetime | None]] = []
    settled_hidden = 0

    for name, b in buckets.items():
        net = _bucket_net(b)
        last = _bucket_last_event(b)
        if abs(net) < 0.5:  # treat sub-rupee residue as settled
            if last is None or cutoff_days <= 0 or (now - last).days <= cutoff_days:
                settled_recent.append((name, last))
            else:
                settled_hidden += 1
            continue
        items.append((name, net, last))
        if net > 0:
            you_owe_total += net
        else:
            owed_to_you_total += -net

    # Sort by absolute outstanding, biggest first.
    items.sort(key=lambda x: (-abs(x[1]), x[0]))
    owe_items = [(n, v, l) for (n, v, l) in items if v > 0]
    owed_items = [(n, v, l) for (n, v, l) in items if v < 0]

    scope_name = "everyone" if show_all else tag
    stamp = now.strftime("%d/%m %H:%M")
    net_total = you_owe_total - owed_to_you_total

    lines: list[str] = [f"🏦 LOANS · {scope_name} · as of {stamp}", ""]
    lines.append(_loan_pair("You owe", _format_money(you_owe_total, cur), indent=0))
    lines.append(_loan_pair("Owed to you", _format_money(owed_to_you_total, cur), indent=0))
    # Net from your perspective: positive net_total = you owe more than
    # you're owed → render negative ("you're down ৳X"). Negative = the
    # opposite ("you're up ৳X").
    if net_total > 0:
        net_value = "-" + _format_money(net_total, cur)
    elif net_total < 0:
        net_value = "+" + _format_money(net_total, cur)
    else:
        net_value = "—"
    lines.append(_loan_pair("Net", net_value, indent=0))
    lines.append("")
    counts = f"Active {len(items)}"
    if settled_recent:
        counts += f" · Settled (last {cutoff_days}d) {len(settled_recent)}"
    if settled_hidden:
        counts += f" · {settled_hidden} older settled hidden"
    lines.append(counts)

    def section(title: str, rows: list[tuple[str, float, datetime | None]]) -> None:
        if not rows:
            return
        lines.append("")
        lines.append("─" * _LOAN_W)
        lines.append(f"{title:<{_LOAN_NAME_COL}} {'Amount':>{_LOAN_AMT_COL}} {'Last':>{_LOAN_LAST_COL}}")
        lines.append("─" * _LOAN_W)
        for name, net, last in rows:
            lines.append(_loan_row(
                _loan_display_name(name),
                _format_money(net, cur),
                _relative_time(last, now),
            ))

    section("YOU OWE", owe_items)
    section("OWED TO YOU", owed_items)

    if settled_recent:
        lines.append("")
        lines.append("─" * _LOAN_W)
        names = ", ".join(_loan_display_name(n) for n, _ in settled_recent)
        lines.append(f"Settled (last {cutoff_days}d): {names}")

    if items:
        lines.append("")
        lines.append("/loan <name>  for full history")

    body = "\n".join(lines).rstrip()
    await update.effective_message.reply_html(f"<pre>{html.escape(body)}</pre>")


async def _render_loan_detail(
    update: Update,
    settings,
    buckets: dict[str, dict[str, Any]],
    *,
    query: str,
    show_all: bool,
) -> None:
    """Single-loan view: full history, all four totals, dates."""
    cur = settings.currency_symbol
    now = datetime.now(TIMEZONE)

    # Resolve query → bucket key. Match against slug AND display name
    # case-insensitively. Prefix match falls through to exact.
    q = query.strip().lower()
    matches: list[str] = []
    for slug in buckets.keys():
        display_lower = _loan_display_name(slug).lower()
        if slug.lower() == q or display_lower == q:
            matches = [slug]
            break
        if slug.lower().startswith(q) or display_lower.startswith(q):
            matches.append(slug)
    if not matches:
        avail = ", ".join(sorted(_loan_display_name(k) for k in buckets.keys()))
        await update.effective_message.reply_text(
            f"No loan matches '{query}'. Known: {avail}"
        )
        return
    if len(matches) > 1:
        await update.effective_message.reply_text(
            f"'{query}' matches multiple: "
            f"{', '.join(_loan_display_name(m) for m in matches)}. "
            f"Be more specific."
        )
        return

    name = matches[0]
    b = buckets[name]
    net = _bucket_net(b)
    last = _bucket_last_event(b)
    events_sorted = sorted(
        b["events"],
        key=lambda x: parse_expense_date(x[0].get("date")) or datetime.min.replace(tzinfo=TIMEZONE),
    )
    first_event = (
        parse_expense_date(events_sorted[0][0].get("date")) if events_sorted else None
    )

    lines: list[str] = [
        f"🏦 {_loan_display_name(name).upper()} · {now.strftime('%d/%m %H:%M')}",
        "",
    ]
    if abs(net) < 0.5:
        status_value = "settled ✓"
    elif net > 0:
        status_value = f"you owe {_format_money(net, cur)}"
    else:
        status_value = f"{_loan_display_name(name)} owes you {_format_money(net, cur)}"
    lines.append(_loan_pair("Status", status_value, indent=0))
    lines.append(_loan_pair("First event", _relative_time(first_event, now), indent=0))
    lines.append(_loan_pair("Last event", _relative_time(last, now), indent=0))
    lines.append(_loan_pair("Events", str(len(b["events"])), indent=0))
    lines.append("")
    # Show only non-zero rows so the panel stays clean for one-sided loans.
    if b["taken"]:
        lines.append(_loan_pair("Taken (I borrowed)", _format_money(b["taken"], cur), indent=0))
    if b["repaid"]:
        lines.append(_loan_pair("Repaid (I paid back)", _format_money(b["repaid"], cur), indent=0))
    if b["given"]:
        lines.append(_loan_pair("Given (I lent)", _format_money(b["given"], cur), indent=0))
    if b["received"]:
        lines.append(_loan_pair("Received (paid back)", _format_money(b["received"], cur), indent=0))

    lines.append("")
    lines.append("─" * _LOAN_W)
    lines.append("History")
    lines.append("─" * _LOAN_W)
    # Compact event rows: date | desc | signed-amount
    avail = _LOAN_W - 5 - 2 - 1 - 9  # date(5)+gap(2)+space(1)+amount(9)
    sign_map = {"taken": "+", "received": "+", "repaid": "-", "given": "-"}
    for e, flow, magnitude in events_sorted:
        d = _entry_local_date(e)
        date_str = d.strftime("%d/%m") if d else "?"
        desc = _truncate(str(e.get("name") or "?"), avail)
        amt = f"{sign_map.get(flow, '?')}{int(round(magnitude)):,}"
        lines.append(f"{date_str}  {desc:<{avail}} {amt:>9}")

    lines.append("")
    lines.append("/loan  for summary")

    body = "\n".join(lines).rstrip()
    await update.effective_message.reply_html(f"<pre>{html.escape(body)}</pre>")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report dispatcher:
      /report              monthly cash-flow summary (default)
      /report all          household scope (everyone's entries)
      /report daily        day-by-day ledger (the old layout)
      /report daily all    household daily ledger
    """
    settings = get_settings(context)
    if not is_authorised(update, context):
        return
    try:
        all_expenses = await _fetch_expenses(context)
    except ExpenseOwlError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return

    args_lower = [a.strip().lower() for a in (getattr(context, "args", None) or [])]
    show_all = "all" in args_lower
    daily_view = "daily" in args_lower

    tag = user_tag(update, settings)
    own = (
        all_expenses if show_all
        else [e for e in all_expenses if expense_has_tag(e, tag)]
    )
    if not own:
        scope = "yet" if show_all else f"for {tag} yet"
        await update.effective_message.reply_text(f"Nothing logged {scope}.")
        return

    if daily_view:
        await _render_report_daily(update, settings, own, show_all=show_all, tag=tag)
    else:
        aliases = get_loan_aliases(context)
        await _render_report_summary(
            update, settings, own, show_all=show_all, tag=tag, aliases=aliases,
        )


# ---- helpers used by the summary view ------------------------------------

def _filter_prev_month(expenses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entries dated in the LOCAL previous calendar month."""
    now = datetime.now(TIMEZONE)
    prev_y, prev_m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    out: list[dict[str, Any]] = []
    for exp in expenses:
        when = parse_expense_date(exp.get("date"))
        if not when:
            continue
        local = when.astimezone(TIMEZONE)
        if local.year == prev_y and local.month == prev_m:
            out.append(exp)
    return out


def _days_in_month(now: datetime) -> int:
    """Number of days in `now`'s local month."""
    if now.month == 12:
        nxt = now.replace(year=now.year + 1, month=1, day=1)
    else:
        nxt = now.replace(month=now.month + 1, day=1)
    last = nxt - timedelta(days=1)
    return last.day


def _active_days(entries: list[dict[str, Any]]) -> int:
    return len({_entry_local_date(e) for e in entries if _entry_local_date(e)})


def _top_mover(
    entries: list[dict[str, Any]],
    *,
    side: str,
) -> tuple[str, float, Any] | None:
    """Biggest single-entry mover on `side` ('out' = expense/loan-out, 'in' = income/loan-in).
    Returns (name, magnitude, local_date) or None.
    """
    out_buckets = {"regular_expense", "loan_repaid", "loan_given"}
    in_buckets = {"regular_income", "loan_taken", "loan_received"}
    target = out_buckets if side == "out" else in_buckets
    best: tuple[float, dict[str, Any]] | None = None
    for e in entries:
        bucket, mag = _classify_entry(e)
        if bucket not in target or mag <= 0:
            continue
        if best is None or mag > best[0]:
            best = (mag, e)
    if best is None:
        return None
    mag, e = best
    return (str(e.get("name") or "?"), mag, _entry_local_date(e))


def _category_breakdown(
    entries: list[dict[str, Any]],
) -> list[tuple[str, float, float]]:
    """List of (category, amount, pct) for regular expenses only, desc by amount."""
    sums: dict[str, float] = defaultdict(float)
    for e in entries:
        bucket, mag = _classify_entry(e)
        if bucket != "regular_expense":
            continue
        sums[str(e.get("category") or "Other")] += mag
    total = sum(sums.values())
    if total <= 0:
        return []
    return sorted(
        ((cat, amt, amt / total * 100) for cat, amt in sums.items()),
        key=lambda x: -x[1],
    )


def _net_loan_position(
    own_all_time: list[dict[str, Any]],
    *,
    aliases,
) -> tuple[float, float]:
    """Return (you_owe_total, owed_to_you_total) summed across every loan
    bucket. Uses the alias map so collapsed names sum into one bucket.
    """
    buckets = _build_loan_buckets(own_all_time, aliases=aliases)
    you_owe = 0.0
    owed_to_you = 0.0
    for name, b in buckets.items():
        net = (b["taken"] - b["repaid"]) - (b["given"] - b["received"])
        if net > 0:
            you_owe += net
        elif net < 0:
            owed_to_you += -net
    return you_owe, owed_to_you


# ---- /report summary view (default) --------------------------------------

async def _render_report_summary(
    update: Update,
    settings,
    own_all_time: list[dict[str, Any]],
    *,
    show_all: bool,
    tag: str,
    aliases,
) -> None:
    cur = settings.currency_symbol
    now = datetime.now(TIMEZONE)
    scope_name = "everyone" if show_all else tag

    month_entries = _filter_month(own_all_time)
    prev_entries = _filter_prev_month(own_all_time)
    month = _sum_buckets(month_entries)
    prev = _sum_buckets(prev_entries)

    # Cash flow components.
    loans_in = month["loan_taken"] + month["loan_received"]
    loans_out = month["loan_repaid"] + month["loan_given"]
    reg_in = month["regular_income"]
    reg_out = month["regular_expense"]
    net_change = (reg_in + loans_in) - (reg_out + loans_out)
    net_worth_change = reg_in - reg_out  # excludes loan churn

    # Pace.
    days_elapsed = now.day
    days_total = _days_in_month(now)
    active = _active_days(month_entries)
    eom_forecast = (
        reg_out * days_total / days_elapsed if days_elapsed > 0 and reg_out > 0
        else 0.0
    )
    prev_reg_out = prev["regular_expense"]

    # Net loan position from ALL time, not just this month.
    you_owe, owed_to_you = _net_loan_position(own_all_time, aliases=aliases)

    # ---- layout ----
    lines: list[str] = [
        f"{now.strftime('%B %Y').upper()} · {scope_name} · as of "
        f"{now.strftime('%d/%m %H:%M')}",
        "",
    ]

    # CASH FLOW block
    lines.append("CASH FLOW")
    lines.append(_loan_pair("Regular income",  _money_signed(+reg_in,  cur)))
    lines.append(_loan_pair("Loans IN",        _money_signed(+loans_in, cur)))
    lines.append(_loan_pair("Regular expense", _money_signed(-reg_out, cur)))
    lines.append(_loan_pair("Loans OUT",       _money_signed(-loans_out, cur)))
    lines.append("  " + "─" * (_LOAN_W - 2))
    lines.append(_loan_pair("Net change",      _money_signed(net_change, cur)))
    lines.append("")
    lines.append(_loan_pair("Net worth change (excl loans)", _money_signed(net_worth_change, cur)))
    if you_owe:
        lines.append(_loan_pair("Outstanding (you owe)",  _format_money(you_owe, cur)))
    if owed_to_you:
        lines.append(_loan_pair("Outstanding (owed to you)", _format_money(owed_to_you, cur)))

    # PACE block
    lines.append("")
    lines.append("PACE")
    lines.append(_loan_pair("Active days", f"{active} of {days_elapsed} elapsed"))
    if eom_forecast:
        lines.append(_loan_pair("EOM expense forecast", f"~{_format_money(eom_forecast, cur)}"))
    if prev_reg_out:
        delta_pct = (reg_out - prev_reg_out) / prev_reg_out * 100
        arrow = "▲" if delta_pct >= 0 else "▼"
        lines.append(_loan_pair(
            "Last month expense",
            f"{_format_money(prev_reg_out, cur)} {arrow} {abs(delta_pct):.0f}%",
        ))
    elif prev_entries:
        lines.append(_loan_pair("Last month expense", "—"))

    # TOP MOVERS block
    top_out = _top_mover(month_entries, side="out")
    top_in = _top_mover(month_entries, side="in")
    if top_out or top_in:
        lines.append("")
        lines.append("TOP MOVERS")
        if top_out:
            name, mag, d = top_out
            dt = d.strftime("%d/%m") if d else "?"
            lines.append(_loan_pair("Biggest spend",
                                    f"{_truncate(name, 18)} {_format_money(mag, cur)} ({dt})"))
        if top_in:
            name, mag, d = top_in
            dt = d.strftime("%d/%m") if d else "?"
            lines.append(_loan_pair("Biggest income",
                                    f"{_truncate(name, 18)} {_format_money(mag, cur)} ({dt})"))

    # CATEGORIES block (expenses only)
    cats = _category_breakdown(month_entries)
    if cats:
        lines.append("")
        lines.append("CATEGORIES (regular expense)")
        for cat, amt, pct in cats:
            lines.append(_loan_pair(
                f"  {cat}",
                f"{_format_money(amt, cur)} ({pct:.0f}%)",
            ))

    lines.append("")
    lines.append("─" * _LOAN_W)
    lines.append("/report daily   day-by-day ledger")
    lines.append("/loan           per-counterparty detail")

    body = "\n".join(lines).rstrip()
    await update.effective_message.reply_html(f"<pre>{html.escape(body)}</pre>")


def _money_signed(amount: float, currency: str) -> str:
    """Like _format_money but always shows sign; '—' for zero."""
    if abs(amount) < 0.5:
        return "—"
    sign = "+" if amount > 0 else "-"
    return f"{sign}{_format_money(amount, currency)}"


# ---- /report daily view (the old layout, now opt-in) ---------------------

async def _render_report_daily(
    update: Update,
    settings,
    own_all_time: list[dict[str, Any]],
    *,
    show_all: bool,
    tag: str,
) -> None:
    """The previous default — day-by-day rolled-up ledger. Kept available
    via `/report daily` for months where day-level detail matters."""
    cur = settings.currency_symbol
    month_entries = _filter_month(own_all_time)

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

    body_lines: list[str] = [
        f"{now.strftime('%B %Y')} · {scope_name} · daily",
        "",
        _table_line("Date", "Item", "Out", "In"),
        "─" * _REPORT_W,
    ]
    if daily:
        for r in daily:
            body_lines.append(_table_line(
                r["date"], r["item"],
                _cell_amount(r["out"]), _cell_amount(r["in"]),
            ))
        body_lines.append("─" * _REPORT_W)
        body_lines.append(_table_line(
            "TOTAL", "", _cell_amount(total_out), _cell_amount(total_in),
        ))
    else:
        body_lines.append("  (no entries this month)")

    body_lines.append("")
    body_lines.append("/report   for the cash-flow summary")
    body = "\n".join(body_lines)
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
