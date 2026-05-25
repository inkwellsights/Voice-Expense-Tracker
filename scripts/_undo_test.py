"""Round-trip test for /undo:
   1. Create a sentinel expense tagged 'Saiful'
   2. Verify it exists in ExpenseOwl
   3. Call cmd_undo as Saiful
   4. Verify it's gone
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot.config import load_settings
from bot.services.allowlist import Allowlist
from bot.services.expenseowl import ExpenseOwl
from bot.services.loan_aliases import LoanAliases
from bot.services import parser as parser_service
from bot.handlers.commands import cmd_undo


SAIFUL_ID = 2036065642
SENTINEL_NAME = "UNDO_SMOKE_TEST_DELETE_ME"


class CapturedMessage:
    def __init__(self):
        self.text = None
    async def reply_text(self, text):       self.text = text
    async def reply_markdown(self, text):   self.text = text
    async def reply_html(self, text):       self.text = text


async def run():
    settings = load_settings()
    parser_service.configure_context(settings.context_synonyms, settings.context_default)
    owl = ExpenseOwl(settings.expenseowl_url)
    allowlist = Allowlist(
        static_ids=set(settings.allowed_user_ids),
        json_path=Path("/app/data/allowed_users.json"),
    )

    print("STEP 1: create sentinel expense")
    created = await owl.create(
        name=SENTINEL_NAME,
        amount=1.0,
        category="Other",
        tags=["Saiful", "personal"],
        kind="expense",
    )
    print(f"  created: {created}")

    print()
    print("STEP 2: verify it's in the store")
    all_exp = await owl.list_all()
    sentinels = [e for e in all_exp if e.get("name") == SENTINEL_NAME]
    print(f"  found {len(sentinels)} sentinel(s) in store")
    if not sentinels:
        print("!! sentinel not visible after create — aborting")
        return
    print(f"  newest sentinel id: {sentinels[0].get('id')}")
    pre_count = len(all_exp)
    print(f"  total expenses pre-undo: {pre_count}")

    print()
    print("STEP 3: call /undo as Saiful")
    loan_aliases = LoanAliases(json_path=Path("/app/data/loan_aliases.json"))
    application = SimpleNamespace(bot_data={
        "settings": settings,
        "owl": owl,
        "allowlist": allowlist,
        "loan_aliases": loan_aliases,
        "last_expense_id": {},
    })
    msg = CapturedMessage()
    user = SimpleNamespace(id=SAIFUL_ID, first_name="Saiful", username=None, is_bot=False)
    upd = SimpleNamespace(effective_user=user, effective_message=msg, effective_chat=SimpleNamespace(id=SAIFUL_ID))
    ctx = SimpleNamespace(application=application, args=[])
    await cmd_undo(upd, ctx)
    print(f"  bot replied: {msg.text!r}")

    print()
    print("STEP 4: verify it's gone")
    all_exp = await owl.list_all()
    sentinels_after = [e for e in all_exp if e.get("name") == SENTINEL_NAME]
    post_count = len(all_exp)
    print(f"  sentinels remaining: {len(sentinels_after)}")
    print(f"  total expenses post-undo: {post_count}")
    print(f"  delta: {pre_count - post_count}")

    print()
    if len(sentinels_after) == 0 and post_count == pre_count - 1:
        print("PASS — /undo correctly removed the sentinel.")
    else:
        print("FAIL — undo did not remove the sentinel cleanly.")
        if sentinels_after:
            print("  -- cleaning up leftover sentinels --")
            for e in sentinels_after:
                try:
                    await owl.delete(str(e.get("id")))
                except Exception as exc:
                    print(f"  delete {e.get('id')} failed: {exc}")


if __name__ == "__main__":
    asyncio.run(run())
