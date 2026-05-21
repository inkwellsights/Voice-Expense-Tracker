"""Photo / receipt handler."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..services.parser import ParseError, parse_image
from .common import get_settings, is_authorised, log_entries

logger = logging.getLogger(__name__)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_authorised(update, settings):
        return

    message = update.effective_message
    photos = message.photo or []
    document = message.document

    file_bytes: bytes
    mime_type = "image/jpeg"

    try:
        if photos:
            largest = photos[-1]  # Telegram sorts smallest → largest
            tg_file = await largest.get_file()
            file_bytes = bytes(await tg_file.download_as_bytearray())
        elif document and (document.mime_type or "").startswith("image/"):
            tg_file = await document.get_file()
            file_bytes = bytes(await tg_file.download_as_bytearray())
            mime_type = document.mime_type or "image/jpeg"
        else:
            return
    except Exception as exc:
        logger.exception("Failed to download photo")
        await message.reply_text(f"❌ Could not download the image: {exc}")
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    try:
        entries = await parse_image(
            file_bytes, mime_type, api_key=settings.gemini_api_key
        )
    except ParseError as exc:
        await message.reply_text(f"❌ Could not read the receipt: {exc}")
        return

    await log_entries(update, context, entries)
