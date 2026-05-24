"""Whisper transcription with optional local-first cascade.

Order of operations per call:
  1. If `local_url` is set, POST the audio there with a short timeout.
     - 200 → return text (subject to the same confidence guard as cloud).
     - 503 (gpu busy gate), connect-refused, connect-timeout, read-timeout,
       or any other 5xx → log and fall through to Groq.
  2. POST to Groq's hosted whisper-large-v3 as the cloud fallback.

The local server is run by `local-whisper/` in this repo. Its GPU-busy gate
makes 503 a normal, expected response — meaning "use the cloud instead".
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


def _extract_text(data: dict, source: str) -> str:
    """Pull text + confidence-guard a whisper response (local or cloud)."""
    text = (data.get("text") or "").strip()
    if not text:
        raise TranscriptionError(f"{source} returned an empty transcript.")
    segments = data.get("segments") or []
    no_speech, avg_logprob = _aggregate_confidence(segments)
    logger.info(
        "Transcript (%s): %s  [no_speech=%.2f avg_logprob=%.2f segs=%d]",
        source, text, no_speech, avg_logprob, len(segments),
    )
    if no_speech > MAX_NO_SPEECH_PROB or avg_logprob < MIN_AVG_LOGPROB:
        raise LowConfidenceTranscript(
            f"Couldn't make out the audio (no_speech={no_speech:.0%}, "
            f"avg_logprob={avg_logprob:.2f}). Try a slightly longer clip "
            f"in a quieter spot."
        )
    return text


async def _try_local(
    audio_bytes: bytes, *, url: str, timeout: float, filename: str
) -> str | None:
    """Try the local Whisper server. Returns text on success, None on fallthrough.

    LowConfidenceTranscript bubbles up (it's a real transcript that failed the
    guard — no point asking the cloud the same question). Everything else
    (transport errors, 503, 5xx) returns None so the caller hits Groq.
    """
    files = {
        "file": (filename, audio_bytes, "audio/ogg"),
        "model": (None, MODEL),
        "response_format": (None, "verbose_json"),
        "temperature": (None, "0"),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, files=files)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        logger.warning("local whisper unreachable (%s); falling back to Groq", type(exc).__name__)
        return None
    except httpx.HTTPError as exc:
        logger.warning("local whisper transport error (%s); falling back to Groq", exc)
        return None

    if response.status_code == 503:
        # GPU-busy gate said no. Expected, not an error.
        logger.info("local whisper busy (503: %s); falling back to Groq", response.text[:120])
        return None
    if response.status_code >= 500:
        logger.warning("local whisper %s: %s; falling back to Groq", response.status_code, response.text[:120])
        return None
    if response.status_code != 200:
        # 4xx — something we sent is wrong. Fail fast rather than hide it.
        raise TranscriptionError(
            f"Local Whisper returned {response.status_code}: {response.text[:300]}"
        )
    return _extract_text(response.json(), source="local")


async def _try_groq(
    audio_bytes: bytes, *, api_key: str, filename: str
) -> str:
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
    return _extract_text(response.json(), source="groq")


async def transcribe(
    audio_bytes: bytes,
    *,
    api_key: str,
    filename: str = "voice.ogg",
    local_url: str = "",
    local_timeout: float = 8.0,
) -> str:
    """Local-first transcription. Falls back to Groq on any local failure."""
    if local_url:
        text = await _try_local(
            audio_bytes, url=local_url, timeout=local_timeout, filename=filename
        )
        if text is not None:
            return text
    return await _try_groq(audio_bytes, api_key=api_key, filename=filename)
