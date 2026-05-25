"""End-to-end test battery for the bot's logic layers.

Runs inside the voice-expense-bot container:
    scp local→casaos:/tmp, docker cp into voice-expense-bot:/tmp,
    docker exec voice-expense-bot bash -c 'cd /app && PYTHONPATH=/app python /tmp/_battery_test.py'

Exercises:
  1. parse_text against 18 inputs (every loan flow direction, mixed
     messages, edge cases, Banglish).
  2. /loan summary + /loan <name> deep-dive against synthetic states
     built by writing __TEST__ -prefixed entries to ExpenseOwl.
  3. /undo, /today, /month behaviour with loan entries in scope.

Every test entry it creates is prefixed __TEST__ and deleted at the
end (cleanup runs in a finally block even if an assertion fails).

What this CANNOT test:
  - Real voice notes (only the user can speak into a phone)
  - Real receipt photos
  - Telegram-side rendering quirks (monospace alignment on Android)
"""
from __future__ import annotations

import asyncio
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from bot.config import load_settings, TIMEZONE
from bot.services.allowlist import Allowlist
from bot.services.expenseowl import ExpenseOwl
from bot.services import parser as parser_service
from bot.services.parser import parse_text
from bot.handlers.commands import (
    cmd_loan, cmd_today, cmd_month, cmd_undo, cmd_report,
)


SAIFUL_ID = 2036065642
SAIFUL_NAME = "Saiful"
TEST_PREFIX = "__TEST__"


# ---------- Test plumbing ----------

class Msg:
    def __init__(self):
        self.text = None
    async def reply_text(self, t):       self.text = t
    async def reply_markdown(self, t):   self.text = t
    async def reply_html(self, t):       self.text = t


def make_app(settings, owl, allowlist):
    return SimpleNamespace(bot_data={
        "settings": settings, "owl": owl, "allowlist": allowlist,
        "last_expense_id": {},
    })


def make_update(user_id=SAIFUL_ID, first_name=SAIFUL_NAME):
    msg = Msg()
    user = SimpleNamespace(id=user_id, first_name=first_name, username=None, is_bot=False)
    upd = SimpleNamespace(
        effective_user=user, effective_message=msg,
        effective_chat=SimpleNamespace(id=user_id),
    )
    return upd, msg


def make_ctx(application, args=None):
    return SimpleNamespace(application=application, args=list(args or []))


# ---------- Test results ----------

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    line = f"[{flag}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line)


def must(name: str, cond: bool, detail: str = "") -> None:
    report(name, cond, detail)


# ---------- Parser tests (pure function, no DB) ----------

async def test_parser(settings):
    print("\n=== PARSER ===\n")
    key = settings.gemini_api_key
    parser_service.configure_context(settings.context_synonyms, settings.context_default)

    cases: list[tuple[str, str, Callable[[list[dict]], tuple[bool, str]]]] = [
        # Sanity baseline
        ("text: coffee 120",
         "coffee 120",
         lambda r: (len(r) == 1 and r[0]["amount"] == 120 and r[0]["type"] == "expense",
                    f"got {r}")),

        ("text: two items split",
         "coffee 120, uber 280",
         lambda r: (len(r) == 2 and {int(e["amount"]) for e in r} == {120, 280},
                    f"got {r}")),

        ("text: income (salary)",
         "salary 50000",
         lambda r: (len(r) == 1 and r[0]["type"] == "income" and r[0]["amount"] == 50000,
                    f"got {r}")),

        # ----- Loan: I borrowed (loan_taken) -----
        ("loan_taken: borrowed from rahim",
         "borrowed 5000 from rahim",
         lambda r: (any(
             e["flow"] == "loan_taken" and e["type"] == "income"
             and e["loan_name"] == "rahim" and e["amount"] == 5000 for e in r
         ), f"got {r}")),

        ("loan_taken: bare 'borrowed 500' no name",
         "borrowed 500",
         lambda r: (any(e["flow"] == "loan_taken" and e["amount"] == 500 for e in r),
                    f"got {r}")),

        ("loan_taken: Banglish 'abbu theke dosh hajar nilam'",
         "abbu theke dosh hajar nilam",
         lambda r: (any(
             e["flow"] == "loan_taken" and e["amount"] == 10000
             and e["loan_name"] == "abbu" for e in r
         ), f"got {r}")),

        # ----- Loan: I paid back (loan_repaid) -----
        ("loan_repaid: paid back rahim",
         "paid back rahim 2000",
         lambda r: (any(
             e["flow"] == "loan_repaid" and e["type"] == "expense"
             and e["loan_name"] == "rahim" and e["amount"] == 2000 for e in r
         ), f"got {r}")),

        ("loan_repaid: Banglish 'rahim ke loan shod korlam 1500'",
         "rahim ke loan shod korlam 1500",
         lambda r: (any(
             e["flow"] == "loan_repaid" and e["loan_name"] == "rahim"
             and e["amount"] == 1500 for e in r
         ), f"got {r}")),

        # ----- Loan: I lent (loan_given) -----
        ("loan_given: lent to bashar",
         "lent 1000 to bashar",
         lambda r: (any(
             e["flow"] == "loan_given" and e["type"] == "expense"
             and e["loan_name"] == "bashar" and e["amount"] == 1000 for e in r
         ), f"got {r}")),

        ("loan_given: Banglish 'bashar ke 2000 loan dilam'",
         "bashar ke 2000 loan dilam",
         lambda r: (any(
             e["flow"] == "loan_given" and e["loan_name"] == "bashar"
             and e["amount"] == 2000 for e in r
         ), f"got {r}")),

        # ----- Loan: got my money back (loan_received_back) -----
        ("loan_received_back: bashar paid me back 1000",
         "bashar paid me back 1000",
         lambda r: (any(
             e["flow"] == "loan_received_back" and e["type"] == "income"
             and e["loan_name"] == "bashar" and e["amount"] == 1000 for e in r
         ), f"got {r}")),

        ("loan_received_back: Banglish 'rahim theke loan ferot pailam 3000'",
         "rahim theke loan ferot pailam 3000",
         lambda r: (any(
             e["flow"] == "loan_received_back" and e["loan_name"] == "rahim"
             and e["amount"] == 3000 for e in r
         ), f"got {r}")),

        # ----- Mixed messages -----
        ("mixed: 'uber 150, coffee 80, paid back rafiq 500'",
         "uber 150, coffee 80, paid back rafiq 500",
         lambda r: (
             len(r) >= 3
             and any(e["name"].lower().startswith("uber") for e in r)
             and any("coffee" in e["name"].lower() for e in r)
             and any(e.get("flow") == "loan_repaid" for e in r),
             f"got {r}",
         )),

        ("mixed: loan_taken + grocery 'borrowed 2000 from karim and bought groceries 1800'",
         "borrowed 2000 from karim and bought groceries 1800",
         lambda r: (
             any(e["flow"] == "loan_taken" and e["amount"] == 2000 for e in r)
             and any(e["flow"] == "regular" and e["amount"] == 1800 for e in r),
             f"got {r}",
         )),

        # ----- Context tag -----
        ("context: 'masnoonhub borrowed 3000 from supplier'",
         "masnoonhub borrowed 3000 from supplier",
         lambda r: (any(
             e["flow"] == "loan_taken" and e["context"] == "MHUBEXP"
             and e["loan_name"] == "supplier" for e in r
         ), f"got {r}")),

        # ----- Edges -----
        ("edge: 'refund from amazon 450' is income NOT a loan",
         "refund from amazon 450",
         lambda r: (any(
             e["type"] == "income" and e["flow"] == "regular"
             and e["amount"] == 450 for e in r
         ), f"got {r}")),

        ("edge: empty string returns []",
         "",
         lambda r: (r == [], f"got {r}")),

        ("edge: gibberish returns []",
         "asdfqwer zxcv",
         lambda r: (r == [], f"got {r}")),
    ]

    # Gemini free tier is 15 RPM. Sleep between calls so a long battery
    # doesn't trip the limit. ~5s puts us safely under 12 RPM with some
    # headroom for the retries the parser does on its own.
    for i, (name, msg, check) in enumerate(cases):
        if i > 0:
            await asyncio.sleep(5)
        try:
            r = await parse_text(msg, api_key=key)
        except Exception as exc:
            report(name, False, f"raised {type(exc).__name__}: {exc}")
            continue
        try:
            ok, detail = check(r)
        except Exception as exc:
            ok, detail = False, f"check raised: {exc}"
        report(name, ok, detail)


# ---------- /loan rendering tests (synthetic state) ----------

async def seed_entries(owl: ExpenseOwl, fixtures: list[dict]) -> list[str]:
    """Write fixture entries and return the ids so we can clean up."""
    created_ids: list[str] = []
    # We can't choose the id, but we name everything __TEST__... so we can
    # find them back by name even if the create response has empty id.
    for fx in fixtures:
        await owl.create(
            name=TEST_PREFIX + fx["name"],
            amount=fx["amount"],
            category=fx.get("category", "Other"),
            tags=fx["tags"],
            kind=fx.get("kind", "expense"),
            date=fx.get("date"),
        )
    # Fetch back and grab ids of every __TEST__-named entry.
    all_exp = await owl.list_all()
    for e in all_exp:
        if str(e.get("name", "")).startswith(TEST_PREFIX):
            eid = str(e.get("id") or "")
            if eid and eid not in created_ids:
                created_ids.append(eid)
    return created_ids


async def cleanup_entries(owl: ExpenseOwl) -> int:
    """Delete every __TEST__-named entry. Returns count deleted."""
    deleted = 0
    all_exp = await owl.list_all()
    for e in all_exp:
        if str(e.get("name", "")).startswith(TEST_PREFIX):
            eid = str(e.get("id") or "")
            if eid:
                try:
                    await owl.delete(eid)
                    deleted += 1
                except Exception as exc:
                    print(f"  WARN: cleanup failed for {eid}: {exc}")
    return deleted


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


async def render_loan(app, args: list[str] | None = None) -> str:
    upd, msg = make_update()
    ctx = make_ctx(app, args=args or [])
    await cmd_loan(upd, ctx)
    return msg.text or ""


async def test_loan_rendering(settings, owl, allowlist):
    print("\n=== /loan RENDERING ===\n")
    app = make_app(settings, owl, allowlist)

    # Wipe any test residue first.
    await cleanup_entries(owl)
    # Skip "no loans for user" — the bot is allowlist-gated and we can't
    # easily construct an authorized user with zero entries while live
    # data exists for Saiful. Empty-state path is exercised in production
    # before the first loan is logged.

    # ---- Scenario 2: synthetic state -- one each of every flow ----
    today = now_local()
    fixtures = [
        # New format loan-name tag, taken from Alice 5000
        {"name": "from alice 5000", "amount": 5000, "kind": "income",
         "tags": ["Saiful", "personal", "loan-taken", "loan--alice"],
         "date": today},
        # Repaid 1000 to alice
        {"name": "to alice 1000", "amount": -1000, "kind": "expense",
         "tags": ["Saiful", "personal", "loan-repaid", "loan--alice"],
         "date": today},
        # Lent 2000 to bob
        {"name": "lent to bob 2000", "amount": -2000, "kind": "expense",
         "tags": ["Saiful", "personal", "loan-given", "loan--bob"],
         "date": today},
        # Bob paid back 500
        {"name": "bob back 500", "amount": 500, "kind": "income",
         "tags": ["Saiful", "personal", "loan-received", "loan--bob"],
         "date": today},
        # Fully settled: carol borrowed 1000, paid back 1000 — TODAY (within 30d)
        {"name": "from carol 1000", "amount": 1000, "kind": "income",
         "tags": ["Saiful", "personal", "loan-taken", "loan--carol"],
         "date": today},
        {"name": "to carol 1000", "amount": -1000, "kind": "expense",
         "tags": ["Saiful", "personal", "loan-repaid", "loan--carol"],
         "date": today},
        # Fully settled: dave borrowed 1000, paid back 1000 — 45 DAYS AGO (>30d, should hide)
        {"name": "from dave old 1000", "amount": 1000, "kind": "income",
         "tags": ["Saiful", "personal", "loan-taken", "loan--dave"],
         "date": today - timedelta(days=45)},
        {"name": "to dave old 1000", "amount": -1000, "kind": "expense",
         "tags": ["Saiful", "personal", "loan-repaid", "loan--dave"],
         "date": today - timedelta(days=45)},
    ]
    await seed_entries(owl, fixtures)

    out = await render_loan(app)
    # Strip the <pre>/</pre> wrapper for easier assertions.
    body = out.replace("<pre>", "").replace("</pre>", "").replace("&lt;", "<").replace("&gt;", ">")
    print("---- /loan output (Saiful, with synthetic fixtures) ----")
    print(body)
    print("---- end ----")

    must("loan: Alice in YOU OWE", "Alice" in body and "YOU OWE" in body)
    must("loan: Bob in OWED TO YOU", "Bob" in body and "OWED TO YOU" in body)
    must("loan: Carol in settled (last 30d)", "Carol" in body and "Settled" in body)
    must("loan: Dave (old settled) is HIDDEN", "Dave" not in body,
         "Dave should not appear (>30d settled)")
    must("loan: '4,000' is alice net (5k-1k)", "৳4,000" in body,
         f"missing in: {body!r}")
    must("loan: '1,500' is bob net (2k-500)", "৳1,500" in body,
         f"missing in: {body!r}")
    must("loan: footer hint visible",
         "/loan <name>" in body, f"missing in: {body!r}")
    must("loan: 'older settled hidden' counter shows when applicable",
         "older settled hidden" in body,
         f"missing in: {body!r}")

    # ---- Scenario 3: deep-dive on alice ----
    out = await render_loan(app, args=["alice"])
    body = out.replace("<pre>", "").replace("</pre>", "").replace("&lt;", "<").replace("&gt;", ">")
    print("---- /loan alice ----")
    print(body)
    print("---- end ----")
    must("deep-dive: alice header", "ALICE" in body)
    must("deep-dive: alice status 'you owe'", "you owe" in body.lower())
    must("deep-dive: alice net 4,000", "৳4,000" in body)
    # Event rows use bare numbers with sign, NOT the ৳ symbol (saved for totals)
    must("deep-dive: history shows both events with correct signs",
         "+5,000" in body and "-1,000" in body,
         f"missing event signs: {body!r}")
    must("deep-dive: hint to go back to summary", "/loan  for summary" in body)

    # ---- Scenario 4: deep-dive on bob (the OWED-TO-YOU case) ----
    out = await render_loan(app, args=["bob"])
    body = out.replace("<pre>", "").replace("</pre>", "").replace("&lt;", "<").replace("&gt;", ">")
    print("---- /loan bob ----")
    print(body)
    print("---- end ----")
    must("deep-dive: bob status 'Bob owes you'", "Bob owes you" in body)

    # ---- Scenario 5: deep-dive on carol (settled) ----
    out = await render_loan(app, args=["carol"])
    body = out.replace("<pre>", "").replace("</pre>", "").replace("&lt;", "<").replace("&gt;", ">")
    must("deep-dive: carol status 'settled ✓'", "settled ✓" in body, body[:200])

    # ---- Scenario 6: deep-dive on unknown ----
    out = await render_loan(app, args=["nonexistentperson"])
    must("deep-dive: unknown name → helpful error",
         "No loan matches" in (out or ""),
         f"got {out!r}")

    # ---- Scenario 7: ambiguous prefix ----
    # Both 'bob' and 'banana' would match 'b' if banana existed; add it.
    await owl.create(
        name=TEST_PREFIX + "banana loan",
        amount=100, kind="income", category="Other",
        tags=["Saiful", "personal", "loan-taken", "loan--banana"],
        date=today,
    )
    out = await render_loan(app, args=["b"])
    body = out if not out.startswith("<pre>") else (
        out.replace("<pre>", "").replace("</pre>", "")
        .replace("&lt;", "<").replace("&gt;", ">")
    )
    must("deep-dive: ambiguous prefix 'b' → asks to be specific",
         "matches multiple" in body,
         f"got {body!r}")


# ---------- end-to-end smoke ----------

async def test_end_to_end(settings, owl, allowlist):
    print("\n=== END-TO-END (parse → log → render → undo) ===\n")
    app = make_app(settings, owl, allowlist)

    # Clean before so the test is reproducible.
    await cleanup_entries(owl)

    # Parse a text → seed via log_entries → confirm it's there → undo → confirm gone.
    from bot.handlers.common import log_entries

    # Use a sentinel-style input so we can find it by name.
    # NOTE: parser may not include __TEST__ in name; we'll match by amount + flow.
    text_in = f"borrowed 12345 from {TEST_PREFIX}eve"
    entries = await parse_text(text_in, api_key=settings.gemini_api_key)
    must("e2e: parser extracted loan_taken from eve",
         any(e.get("flow") == "loan_taken" and abs(e["amount"] - 12345) < 1 for e in entries),
         f"got {entries}")
    # Tag the entry name with __TEST__ so cleanup catches it
    for e in entries:
        e["name"] = TEST_PREFIX + e["name"]

    upd, msg = make_update()
    ctx = make_ctx(app)
    await log_entries(upd, ctx, entries)
    must("e2e: bot replied with confirmation", "Logged" in (msg.text or ""),
         f"got {msg.text!r}")

    # /loan should now show this
    out = await render_loan(app)
    body = out.replace("<pre>", "").replace("</pre>", "").replace("&lt;", "<").replace("&gt;", ">")
    must("e2e: /loan shows the new entry's slug",
         "eve" in body.lower() or "12,345" in body,
         f"missing in: {body!r}")

    # /undo should remove it
    upd2, msg2 = make_update()
    ctx2 = make_ctx(app)
    await cmd_undo(upd2, ctx2)
    must("e2e: /undo replied",
         (msg2.text or "").startswith("↩️"),
         f"got {msg2.text!r}")

    # /loan should NOT show it any more
    out = await render_loan(app)
    body = out.replace("<pre>", "").replace("</pre>", "").replace("&lt;", "<").replace("&gt;", ">")
    must("e2e: /loan no longer shows the undone entry",
         "12,345" not in body,
         f"still in: {body!r}")


# ---------- Driver ----------

async def main():
    settings = load_settings()
    owl = ExpenseOwl(settings.expenseowl_url)
    allowlist = Allowlist(
        static_ids=set(settings.allowed_user_ids),
        json_path=Path("/app/data/allowed_users.json"),
    )

    pre_count_snapshot = len(await owl.list_all())
    print(f"Pre-test live entry count: {pre_count_snapshot}")

    try:
        await test_parser(settings)
        await test_loan_rendering(settings, owl, allowlist)
        await test_end_to_end(settings, owl, allowlist)
    except Exception:
        print("\n!!! Battery aborted by exception:")
        traceback.print_exc()
    finally:
        deleted = await cleanup_entries(owl)
        post_count = len(await owl.list_all())
        print(f"\nCleanup deleted {deleted} __TEST__ entries.")
        print(f"Post-test live entry count: {post_count} (delta vs pre: {post_count - pre_count_snapshot})")

    print("\n=== SUMMARY ===")
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"{passed} passed · {failed} failed")
    if failed:
        print("\nFailures:")
        for name, ok, detail in results:
            if not ok:
                print(f"  · {name}")
                if detail:
                    print(f"      {detail}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
