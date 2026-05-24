"""Local Whisper server with GPU-busy gate.

OpenAI/Groq-compatible:
    POST /v1/audio/transcriptions  multipart: file, model, language?, response_format?, prompt?
    GET  /health
    GET  /gpu                       (debug: what the gate sees right now)

Behaviour:
- Loads Systran/faster-whisper-large-v3 onto cuda:0 (float16, ~5GB VRAM).
- After load, measures baseline VRAM usage. That is "us, idle".
- Per request, queries nvidia-smi. If GPU utilization > UTIL_THRESHOLD
  OR another process is using > OTHER_VRAM_MB_THRESHOLD beyond our
  baseline, returns HTTP 503 with a reason. The bot treats 503 the same
  as connect-refused and falls back to Groq automatically.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("whisper-server")

MODEL_NAME = os.environ.get("WHISPER_MODEL", "Systran/faster-whisper-large-v3")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
DEVICE_INDEX = int(os.environ.get("WHISPER_DEVICE_INDEX", "0"))
MAX_UPLOAD_MB = int(os.environ.get("WHISPER_MAX_UPLOAD_MB", "50"))
PORT = int(os.environ.get("WHISPER_PORT", "8000"))

# GPU busy thresholds. Tuned for "personal desktop where the user might be
# gaming". Adjust via env if it's too aggressive / too lax.
UTIL_THRESHOLD = int(os.environ.get("GPU_UTIL_BUSY_PCT", "25"))
OTHER_VRAM_MB_THRESHOLD = int(os.environ.get("GPU_OTHER_VRAM_BUSY_MB", "1024"))

ALLOWED_FORMATS = {"json", "verbose_json"}
NVIDIA_SMI = shutil.which("nvidia-smi") or "nvidia-smi"

app = FastAPI(title="Local Whisper", version="1.0")


def _smi(query: str) -> str:
    """Run nvidia-smi with --query-gpu and return stdout."""
    out = subprocess.run(
        [NVIDIA_SMI, f"--query-gpu={query}", "--format=csv,noheader,nounits",
         f"--id={DEVICE_INDEX}"],
        capture_output=True, text=True, timeout=5,
    )
    if out.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {out.stderr.strip()}")
    return out.stdout.strip()


def _query_vram_used_mb() -> int:
    return int(_smi("memory.used").split("\n")[0].strip())


def _query_gpu_util_pct() -> int:
    return int(_smi("utilization.gpu").split("\n")[0].strip())


log.info("Loading model %s on %s:%d (%s)...", MODEL_NAME, DEVICE, DEVICE_INDEX, COMPUTE_TYPE)
_t0 = time.time()
model = WhisperModel(
    MODEL_NAME,
    device=DEVICE,
    device_index=DEVICE_INDEX,
    compute_type=COMPUTE_TYPE,
)
log.info("Model loaded in %.1fs", time.time() - _t0)

# Wait a beat for VRAM to settle, then sample baseline.
time.sleep(2)
try:
    BASELINE_VRAM_MB = _query_vram_used_mb()
    log.info("Baseline VRAM (us, idle): %d MB", BASELINE_VRAM_MB)
except Exception as e:
    log.warning("Could not read baseline VRAM (%s); gate will skip VRAM check", e)
    BASELINE_VRAM_MB = -1


def _gpu_busy() -> tuple[bool, str]:
    """Return (busy?, reason) using nvidia-smi at request time."""
    try:
        util = _query_gpu_util_pct()
    except Exception as e:
        # If we can't talk to nvidia-smi, don't block — let the request go.
        log.warning("util check failed: %s — allowing", e)
        return False, ""
    if util > UTIL_THRESHOLD:
        return True, f"gpu util {util}% > {UTIL_THRESHOLD}%"

    if BASELINE_VRAM_MB >= 0:
        try:
            used = _query_vram_used_mb()
        except Exception as e:
            log.warning("vram check failed: %s — allowing", e)
            return False, ""
        other = max(0, used - BASELINE_VRAM_MB)
        if other > OTHER_VRAM_MB_THRESHOLD:
            return True, f"other procs using {other}MB VRAM > {OTHER_VRAM_MB_THRESHOLD}MB"
    return False, ""


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL_NAME, "device": f"{DEVICE}:{DEVICE_INDEX}"}


@app.get("/gpu")
def gpu():
    try:
        util = _query_gpu_util_pct()
        used = _query_vram_used_mb()
    except Exception as e:
        return {"error": str(e)}
    busy, why = _gpu_busy()
    return {
        "util_pct": util,
        "vram_used_mb": used,
        "baseline_mb": BASELINE_VRAM_MB,
        "other_procs_mb": max(0, used - BASELINE_VRAM_MB) if BASELINE_VRAM_MB >= 0 else None,
        "busy": busy,
        "why": why,
        "thresholds": {"util_pct": UTIL_THRESHOLD, "other_vram_mb": OTHER_VRAM_MB_THRESHOLD},
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model_name: str = Form(..., alias="model"),  # accepted, ignored — single model server
    language: Optional[str] = Form(None),
    response_format: str = Form("json"),
    prompt: Optional[str] = Form(None),
    temperature: Optional[float] = Form(0.0),
):
    busy, why = _gpu_busy()
    if busy:
        log.info("rejecting request: gpu busy (%s)", why)
        raise HTTPException(503, f"gpu busy: {why}")

    if response_format not in ALLOWED_FORMATS:
        raise HTTPException(400, f"response_format must be one of {ALLOWED_FORMATS}")
    if not file.filename:
        raise HTTPException(400, "file is required")

    suffix = os.path.splitext(file.filename)[1] or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        size = 0
        chunk = await file.read(1024 * 1024)
        while chunk:
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(400, f"file exceeds {MAX_UPLOAD_MB} MB")
            tmp.write(chunk)
            chunk = await file.read(1024 * 1024)
        tmp.flush()
        tmp.close()

        kwargs = dict(
            beam_size=5,
            vad_filter=True,
            temperature=temperature or 0.0,
        )
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["initial_prompt"] = prompt

        t0 = time.time()
        segments_iter, info = model.transcribe(tmp.name, **kwargs)
        segments = []
        full_text_parts = []
        for s in segments_iter:
            segments.append(
                {
                    "id": s.id, "seek": s.seek,
                    "start": round(s.start, 3), "end": round(s.end, 3),
                    "text": s.text,
                    "tokens": list(s.tokens) if s.tokens else [],
                    "temperature": s.temperature,
                    "avg_logprob": s.avg_logprob,
                    "compression_ratio": s.compression_ratio,
                    "no_speech_prob": s.no_speech_prob,
                }
            )
            full_text_parts.append(s.text)

        text = "".join(full_text_parts).strip()
        log.info(
            "transcribed %.1fs audio in %.2fs (lang=%s, segments=%d)",
            info.duration, time.time() - t0, info.language, len(segments),
        )

        if response_format == "json":
            return {"text": text}
        return {
            "task": "transcribe",
            "language": info.language,
            "duration": round(info.duration, 3),
            "text": text,
            "segments": segments,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("transcription failed")
        raise HTTPException(500, f"transcription failed: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
