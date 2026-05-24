"""Voice message handler."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..services.parser import ParseError, parse_audio, parse_text
from ..services.transcriber import TranscriptionError, transcribe
from .common import get_settings, is_authorised, log_entries

logger = logging.getLogger(__name__)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_authorised(update, context):
        return

    message = update.effective_message
    voice = message.voice or message.audio
    if voice is None:
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    # Immediate acknowledgement so the user knows something is happening even
    # when Gemini multimodal takes 10-30 sec. We edit this same message in
    # place through every stage so the chat stays tidy.
    status = await message.reply_text("🎧 Listening…")

    try:
        tg_file = await voice.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:
        logger.exception("Failed to download voice file")
        await status.edit_text(f"❌ Could not download your voice note: {exc}")
        return

    mime_lower = (getattr(voice, "mime_type", "") or "").lower()
    if "mp3" in mime_lower:
        filename, gemini_mime = "voice.mp3", "audio/mp3"
    elif "wav" in mime_lower:
        filename, gemini_mime = "voice.wav", "audio/wav"
    elif "m4a" in mime_lower or "mp4" in mime_lower:
        filename, gemini_mime = "voice.m4a", "audio/mp4"
    else:
        filename, gemini_mime = "voice.ogg", "audio/ogg"

    if settings.use_audio_native_gemini:
        await status.edit_text("🎧 Listening… 🤖 thinking…")
        try:
            entries = await parse_audio(
                audio_bytes, gemini_mime, api_key=settings.gemini_api_key
            )
            await log_entries(update, context, entries, status_message=status)
            return
        except ParseError as exc:
            logger.warning("Audio-native Gemini failed (%s), falling back to Whisper", exc)
            await status.edit_text("⚠️ Audio path failed, falling back to transcription…")
            # fall through

    # Whisper → text → Gemini fallback (or default if audio-native disabled).
    await status.edit_text("🎧 Transcribing…")
    try:
        transcript = await transcribe(
            audio_bytes,
            api_key=settings.groq_api_key,
            filename=filename,
            local_url=settings.local_whisper_url,
            local_timeout=settings.local_whisper_timeout,
        )
    except TranscriptionError as exc:
        await status.edit_text(f"❌ Transcription failed: {exc}")
        return

    await status.edit_text(f"🗣️ \"{transcript}\"\n\n🤖 Thinking…")

    try:
        entries = await parse_text(transcript, api_key=settings.gemini_api_key)
    except ParseError as exc:
        await status.edit_text(f"❌ Parser error: {exc}")
        return

    await log_entries(update, context, entries, status_message=status)
