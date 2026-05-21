"""Groq Whisper transcription client.

Uses whisper-large-v3-turbo, which handles Bangla, English, and code-switched
Banglish natively. Free tier limit ~28,800 seconds of audio per day.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3"

# Earlier we tried a long English+Banglish priming prompt. It poisoned Whisper:
# a clean English clip like "burger three hundred taka" came out as Korean
# Unicode garbage. Without priming, plain Whisper handles Banglish well enough
# and Gemini cleans up the rest. Keep priming empty unless tightly scoped.
PRIMING_PROMPT = ""


class TranscriptionError(RuntimeError):
    pass


# Whisper confidence thresholds. If aggregate quality is worse than this we
# treat the transcript as a hallucination and ask the user to retry instead
# of forwarding garbage to Gemini.
#   no_speech_prob: 0..1, higher = more likely the segment is silence
#   avg_logprob   : negative, closer to 0 = more confident token predictions
MAX_NO_SPEECH_PROB = 0.60
MIN_AVG_LOGPROB = -1.0


class LowConfidenceTranscript(TranscriptionError):
    """Whisper returned text but its own confidence signals say it's noise."""


def _aggregate_confidence(segments: list[dict]) -> tuple[float, float]:
    """Return (max no_speech_prob, min avg_logprob) across segments."""
    if not segments:
        return 0.0, 0.0
    no_speech = max(float(s.get("no_speech_prob") or 0.0) for s in segments)
    avg_logprob = min(float(s.get("avg_logprob") or 0.0) for s in segments)
    return no_speech, avg_logprob


async def transcribe(audio_bytes: bytes, *, api_key: str, filename: str = "voice.ogg") -> str:
    """Transcribe a voice file via Groq's OpenAI-compatible endpoint."""
    files = {
        "file": (filename, audio_bytes, "audio/ogg"),
        "model": (None, MODEL),
        # Let whisper-large-v3 auto-detect. We tried forcing language=bn to
        # stop short-Banglish clips from being decoded as Gujarati; that fixed
        # the Banglish case but caused English clips to be transliterated into
        # garbled Bengali. The full v3 model's auto-detect is good enough that
        # we'd rather take the occasional Gujarati-script transcript (Gemini
        # parses it anyway) than break English entirely.
        "response_format": (None, "verbose_json"),
        "temperature": (None, "0"),
    }
    if PRIMING_PROMPT:
        files["prompt"] = (None, PRIMING_PROMPT)
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(GROQ_TRANSCRIPTION_URL, headers=headers, files=files)
    except httpx.HTTPError as exc:
        logger.exception("Groq transport error")
        raise TranscriptionError(f"Network error talking to Groq: {exc}") from exc

    if response.status_code != 200:
        logger.error("Groq error %s: %s", response.status_code, response.text)
        raise TranscriptionError(
            f"Groq returned {response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    text = (data.get("text") or "").strip()
    if not text:
        raise TranscriptionError("Groq returned an empty transcript.")

    segments = data.get("segments") or []
    no_speech, avg_logprob = _aggregate_confidence(segments)
    logger.info(
        "Transcript: %s  [no_speech=%.2f avg_logprob=%.2f segs=%d]",
        text, no_speech, avg_logprob, len(segments),
    )
    if no_speech > MAX_NO_SPEECH_PROB or avg_logprob < MIN_AVG_LOGPROB:
        raise LowConfidenceTranscript(
            f"Couldn't make out the audio (no_speech={no_speech:.0%}, "
            f"avg_logprob={avg_logprob:.2f}). Try a slightly longer clip "
            f"in a quieter spot."
        )
    return text
