"""Smoke-test every read-only slash command against the live store.

Mocks Telegram's Update + Context just enough to satisfy the handlers,
captures reply_* output, and prints what the user would actually see.
Read-only: skips /undo /allow /revoke.

Run inside the bot container:
    docker exec -i voice-expense-bot python /tmp/_bot_smoke.py
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot.config import load_settings
from bot.services.allowlist import Allowlist
from bot.services.expenseowl import ExpenseOwl
from bot.services.loan_aliases import LoanAliases
from bot.services import parser as parser_service
from bot.handlers.commands import (
    cmd_start, cmd_help, cmd_categories,
    cmd_today, cmd_month, cmd_report, cmd_loan, cmd_users,
)


SAIFUL_ID = 2036065642
SAIFUL_NAME = "Saiful"


class CapturedMessage:
    def __init__(self):
        self.text = None
        self.mode = None

    async def reply_text(self, text):           self._set(text, "plain")
    async def reply_markdown(self, text):       self._set(text, "markdown")
    async def reply_html(self, text):           self._set(text, "html")
    async def reply_markdown_v2(self, text):    self._set(text, "markdown_v2")
    async def edit_text(self, text):            self._set(text, "edit")
    def _set(self, text, mode):
        self.text = text
        self.mode = mode


def make_update(user_id=SAIFUL_ID, first_name=SAIFUL_NAME):
    msg = CapturedMessage()
    user = SimpleNamespace(
        id=user_id, first_name=first_name, username=None, is_bot=False,
    )
    upd = SimpleNamespace(
        effective_user=user,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=user_id),
    )
    return upd, msg


def make_context(application, args=None):
    return SimpleNamespace(application=application, args=list(args or []))


async def run():
    settings = load_settings()
    parser_service.configure_context(settings.context_synonyms, settings.context_default)

    owl = ExpenseOwl(settings.expenseowl_url)
    allowlist = Allowlist(
        static_ids=set(settings.allowed_user_ids),
        json_path=Path("/app/data/allowed_users.json"),
    )

    loan_aliases = LoanAliases(json_path=Path("/app/data/loan_aliases.json"))
    application = SimpleNamespace(bot_data={
        "settings": settings,
        "owl": owl,
        "allowlist": allowlist,
        "loan_aliases": loan_aliases,
        "last_expense_id": {},
    })

    cases = [
        ("/start",          cmd_start,      []),
        ("/help",           cmd_help,       []),
        ("/categories",     cmd_categories, []),
        ("/today",          cmd_today,      []),
        ("/today all",      cmd_today,      ["all"]),
        ("/month",          cmd_month,      []),
        ("/month all",      cmd_month,      ["all"]),
        ("/report",         cmd_report,     []),
        ("/report all",     cmd_report,     ["all"]),
        ("/loan",           cmd_loan,       []),
        ("/loan all",       cmd_loan,       ["all"]),
        ("/users (admin)",  cmd_users,      []),
    ]

    for label, handler, args in cases:
        upd, msg = make_update()
        ctx = make_context(application, args)
        print("=" * 72)
        print(f">>> {label}    (as {SAIFUL_NAME}, mode={{{handler.__name__}}})")
        print("=" * 72)
        try:
            await handler(upd, ctx)
        except Exception as e:
            print(f"!! handler raised: {type(e).__name__}: {e}")
            continue
        if msg.text is None:
            print("(no reply sent — caller not authorised? command exited silently?)")
        else:
            print(f"[parse_mode={msg.mode}]")
            print(msg.text)
        print()


if __name__ == "__main__":
    asyncio.run(run())
