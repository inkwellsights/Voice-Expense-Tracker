"""Text message handler."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..services.parser import ParseError, parse_text
from .common import get_settings, is_authorised, log_entries

logger = logging.getLogger(__name__)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_authorised(update, context):
        return

    message = update.effective_message
    if not message or not message.text:
        return
    if message.text.startswith("/"):
        # Commands are dispatched by their own handlers.
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    try:
        entries = await parse_text(message.text, api_key=settings.gemini_api_key)
    except ParseError as exc:
        await message.reply_text(f"❌ Parser error: {exc}")
        return

    await log_entries(update, context, entries)
