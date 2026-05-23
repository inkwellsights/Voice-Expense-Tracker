"""Telegram bot entry point."""
from __future__ import annotations

import logging
import sys

from telegram import Update

# Windows consoles default to cp1252, which can't print Bangla / emoji and
# raises UnicodeEncodeError mid-log. Force UTF-8 on both streams so any log line
# (transcripts, expense names, error messages) renders cleanly.
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from pathlib import Path

from .config import load_settings
from .handlers.commands import (
    cmd_allow,
    cmd_categories,
    cmd_help,
    cmd_loan,
    cmd_month,
    cmd_report,
    cmd_revoke,
    cmd_start,
    cmd_today,
    cmd_undo,
    cmd_users,
)
from .handlers.photo import handle_photo
from .handlers.text import handle_text
from .handlers.voice import handle_voice
from .services.allowlist import Allowlist
from .services.expenseowl import ExpenseOwl
from .services import parser as parser_service

# Where the dynamic-allowlist JSON lives. Inside Docker we mount /app/data
# from the host so the file survives `docker compose up -d --build bot`.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# httpx is chatty at INFO — quiet it down to keep logs readable.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def build_application() -> Application:
    settings = load_settings()

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .build()
    )

    # Wire context-tag config into the module-level parser cache so
    # every parse_* call gets the right system prompt addendum + synonym
    # normalization. Done once at startup; safe to call again on reload.
    parser_service.configure_context(
        settings.context_synonyms, settings.context_default
    )

    application.bot_data["settings"] = settings
    application.bot_data["owl"] = ExpenseOwl(settings.expenseowl_url)
    application.bot_data["allowlist"] = Allowlist(
        static_ids=set(settings.allowed_user_ids),
        json_path=DATA_DIR / "allowed_users.json",
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("report", cmd_report))
    application.add_handler(CommandHandler("loan", cmd_loan))
    application.add_handler(CommandHandler("categories", cmd_categories))
    application.add_handler(CommandHandler("undo", cmd_undo))
    # Admin: manage allowlist at runtime
    application.add_handler(CommandHandler("allow", cmd_allow))
    application.add_handler(CommandHandler("revoke", cmd_revoke))
    application.add_handler(CommandHandler("users", cmd_users))

    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(
        MessageHandler(filters.Document.IMAGE, handle_photo)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    return application


def main() -> None:
    app = build_application()
    logger.info("Starting Voice Expense Tracker bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
