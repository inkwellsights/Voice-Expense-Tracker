"""Whisper transcription with cloud-first cascade.

Order of operations per call (per user pref):
  1. POST to Groq's hosted whisper-large-v3 first (cloud, fast, generous
     free tier, ~0.5s typical).
  2. If Groq fails (network, 4xx, 5xx, low-confidence transcript), fall
     back to local Whisper on the 3090 if configured.
  3. If both fail, raise.

Why cloud-first: Groq Whisper is consistently fast with no cold-start
penalty, and its free tier is generous. The local 3090 is the offline
failsafe — slower when warm, much slower on cold start, but kicks in
when the network is bad or Groq is throttled.
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

    Returns None for ANY failure mode so the caller falls through to Groq:
      - transport / connect / timeout errors
      - 503 (GPU-busy gate)
      - 5xx server errors
      - LowConfidenceTranscript — local large-v3 can be conservative on
        short Banglish clips where cloud large-v3 is fine, so a second
        opinion is worth one round trip. If cloud ALSO returns low
        confidence, that error then bubbles up to the user.
    4xx (other than 503) raises — that means we sent something wrong and
    the cloud will reject identically.
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
    try:
        return _extract_text(response.json(), source="local")
    except LowConfidenceTranscript as exc:
        # Worth asking cloud — different model weights / detector tuning.
        logger.info("local low-confidence (%s); falling back to Groq cloud", exc)
        return None


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
    """Cloud-first transcription. Falls back to local Whisper if Groq fails."""
    # Tier 1: Groq cloud Whisper.
    last_err: TranscriptionError | None = None
    try:
        return await _try_groq(audio_bytes, api_key=api_key, filename=filename)
    except TranscriptionError as exc:
        logger.warning("Groq Whisper failed (%s); falling back to local", exc)
        last_err = exc

    # Tier 2: local Whisper on the 3090.
    if local_url:
        text = await _try_local(
            audio_bytes, url=local_url, timeout=local_timeout, filename=filename
        )
        if text is not None:
            return text
        # _try_local already logged the reason it returned None.

    # Nothing worked. Re-raise the most informative error we saw, or a
    # generic one if even local wasn't configured.
    if last_err is not None:
        raise last_err
    raise TranscriptionError(
        "No Whisper provider available — set GROQ_API_KEY or LOCAL_WHISPER_URL."
    )
