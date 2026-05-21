# Running Whisper Locally on an NVIDIA GPU

When you want to stop relying on Groq's hosted Whisper (currently used as the fallback only — Gemini's audio-native path is primary), stand up a self-hosted Whisper server on a machine with an NVIDIA GPU and point the bot at it.

The local server exposes the same OpenAI-compatible API as Groq does. The only bot-side change is the URL.

This recipe is written for a Windows host with an RTX 3090 (24 GB VRAM) because Docker on Windows can be flaky around WSL/GPU passthrough — we run native Python instead. On Linux, replace the Task Scheduler steps with a `systemd` service and skip the cuDNN PATH dance.

## 1. Prerequisites check

Open PowerShell as your normal user and verify each item:

### Python 3.11+

```powershell
python --version
```

Expect `Python 3.11.x` or `3.12.x`. Avoid 3.13 — `ctranslate2` wheels lag behind on Windows.

If missing, install **3.11.9** from https://www.python.org/downloads/release/python-3119/ — tick **Add python.exe to PATH** during install.

### NVIDIA driver + CUDA runtime

```powershell
nvidia-smi
```

Expected output (top of table):

```
NVIDIA-SMI 551.xx   Driver Version: 551.xx   CUDA Version: 12.4
| NVIDIA GeForce RTX 3090     ...  24576MiB
```

Driver must be **535+** (CUDA 12.x). If older, install the latest NVIDIA Studio Driver from https://www.nvidia.com/Download/index.aspx. You do NOT need the full CUDA Toolkit installer — `ctranslate2` ships its own CUDA runtime.

### cuDNN 9.x for CUDA 12 (required by `ctranslate2` ≥ 4.5)

```powershell
where cudnn_ops64_9.dll
```

If it prints a path, you're done. If "INFO: Could not find files":

1. Go to https://developer.nvidia.com/cudnn-downloads → choose **Windows / x86_64 / Tarball / 12.x**
2. Sign in (free NVIDIA dev account)
3. Download the zip (e.g. `cudnn-windows-x86_64-9.5.1.17_cuda12-archive.zip`)
4. Extract to `C:\Program Files\NVIDIA\CUDNN\v9.5\`
5. Add to PATH:

```powershell
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\Program Files\NVIDIA\CUDNN\v9.5\bin", "User")
```

6. Close and reopen PowerShell, re-run `where cudnn_ops64_9.dll` to confirm.

## 2. Model recommendation

**Use `Systran/faster-whisper-large-v3`.**

- It's the CTranslate2-converted version of OpenAI's `large-v3` — the exact same weights Groq serves. Zero quality regression vs. your current cloud setup.
- On a 3090 with `compute_type="float16"` it uses ~5 GB VRAM, transcribes a 30-second clip in ~1.5 sec.
- For Banglish (code-switched EN+BN), `large-v3` is the strongest off-the-shelf option. Language detection runs per-segment.

### Why not a Bangla-finetuned model?

Candidates like `bangla-speech-processing/BanglaASR`, `arijitx/wav2vec2-xls-r-300m-bengali`, `hishab/titulm-asr-bn` are pure-Bangla finetunes. They beat `large-v3` on clean monolingual Bangla by 2-5% WER — but they **degrade hard on Banglish** because English tokens get force-mapped to Bangla phonemes. You'd trade English code-switching for marginal Bangla gains. None of them ship as faster-whisper / CTranslate2 format either, so you'd lose the speed advantage.

Verdict: stick with `Systran/faster-whisper-large-v3`. Revisit only if you measure real WER pain on pure-Bangla clips.

## 3. Server code

Create a working directory, e.g. `C:\whisper-server\`.

**`requirements.txt`:**

```
faster-whisper==1.0.3
fastapi==0.115.5
uvicorn[standard]==0.32.1
python-multipart==0.0.20
```

Install:

```powershell
cd C:\whisper-server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**`whisper_server.py`:**

```python
"""Local Whisper server, OpenAI/Groq-compatible.

POST /v1/audio/transcriptions  multipart: file, model, language?, response_format?, prompt?
GET  /health
"""
import logging
import os
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

ALLOWED_FORMATS = {"json", "verbose_json"}

app = FastAPI(title="Local Whisper", version="1.0")
log.info("Loading model %s on %s:%d (%s)...", MODEL_NAME, DEVICE, DEVICE_INDEX, COMPUTE_TYPE)
_t0 = time.time()
model = WhisperModel(
    MODEL_NAME,
    device=DEVICE,
    device_index=DEVICE_INDEX,
    compute_type=COMPUTE_TYPE,
)
log.info("Model loaded in %.1fs", time.time() - _t0)


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL_NAME, "device": f"{DEVICE}:{DEVICE_INDEX}"}


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(...),  # accepted but ignored; we serve one model
    language: Optional[str] = Form(None),
    response_format: str = Form("json"),
    prompt: Optional[str] = Form(None),
    temperature: Optional[float] = Form(0.0),
):
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
                    "id": s.id,
                    "seek": s.seek,
                    "start": round(s.start, 3),
                    "end": round(s.end, 3),
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
        elapsed = time.time() - t0
        log.info(
            "transcribed %.1fs audio in %.2fs (lang=%s, segments=%d)",
            info.duration, elapsed, info.language, len(segments),
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
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
```

### First run (model downloads ~3 GB to `%USERPROFILE%\.cache\huggingface\`)

```powershell
.\.venv\Scripts\Activate.ps1
python whisper_server.py
```

Wait for `Model loaded in XX.Xs` then `Uvicorn running on http://0.0.0.0:8000`.

### Open the firewall port (one-time, PowerShell as Admin)

```powershell
New-NetFirewallRule -DisplayName "Whisper Server 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

## 4. Autostart with Task Scheduler

`C:\whisper-server\whisper-server.ps1`:

```powershell
$ErrorActionPreference = "Stop"
Set-Location -Path "C:\whisper-server"

$logDir = "C:\whisper-server\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log   = Join-Path $logDir "whisper-$stamp.log"

& "C:\whisper-server\.venv\Scripts\python.exe" "C:\whisper-server\whisper_server.py" *>&1 |
    Tee-Object -FilePath $log
```

Test once manually:

```powershell
powershell -ExecutionPolicy Bypass -File C:\whisper-server\whisper-server.ps1
```

Press Ctrl+C after you see `Uvicorn running...`.

### Register with Task Scheduler

1. Press `Win`, type **Task Scheduler**, open it.
2. Right pane → **Create Task...** (NOT "Create Basic Task").
3. **General** tab:
   - Name: `Whisper Server`
   - Select **Run only when user is logged on**
   - Tick **Run with highest privileges**
4. **Triggers** tab → **New...**
   - Begin the task: **At log on**
   - Delay task for: **30 seconds** (lets the GPU driver settle)
5. **Actions** tab → **New...**
   - Action: **Start a program**
   - Program/script: `powershell.exe`
   - Add arguments: `-ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\whisper-server\whisper-server.ps1"`
   - Start in: `C:\whisper-server`
6. **Conditions** tab: untick "Start the task only if the computer is on AC power"
7. **Settings** tab: tick "Allow task to be run on demand", untick "Stop the task if it runs longer than"

Verify: right-click the task → **Run**. Then from any LAN machine:

```bash
curl http://<windows-desktop-ip>:8000/health
```

Should return `{"ok":true,"model":"Systran/faster-whisper-large-v3","device":"cuda:0"}`.

## 5. Bot-side change (with cloud fallback)

The bot currently uses Groq Whisper only when audio-native Gemini fails. To also try LOCAL Whisper first when Gemini falls back, edit `bot/services/transcriber.py`:

Add to `bot/config.py`:

```python
# In Settings dataclass:
local_whisper_url: str = ""
local_whisper_timeout: float = 8.0

# In load_settings():
local_whisper_url=os.getenv("LOCAL_WHISPER_URL", "").strip(),
local_whisper_timeout=float(os.getenv("LOCAL_WHISPER_TIMEOUT_SECONDS", "8")),
```

Add to `.env`:

```
LOCAL_WHISPER_URL=http://<windows-ip>:8000/v1/audio/transcriptions
LOCAL_WHISPER_TIMEOUT_SECONDS=8
```

Wrap the existing `transcribe()` call so it tries local first, falls through to Groq on any timeout / connect refused / 5xx:

```python
# In bot/services/transcriber.py, before the Groq call:

if settings.local_whisper_url:
    try:
        async with httpx.AsyncClient(timeout=settings.local_whisper_timeout) as client:
            response = await client.post(
                settings.local_whisper_url,
                files={"file": (filename, audio_bytes, "audio/ogg")},
                data={"model": MODEL, "response_format": "verbose_json", "temperature": "0"},
            )
            response.raise_for_status()
            data = response.json()
            # ... same confidence-guard logic ...
            return text
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPStatusError) as e:
        logger.warning("local whisper unreachable (%s), falling back to Groq", type(e).__name__)

# existing Groq call follows
```

## 6. Health-check tests from another machine

```bash
# Health probe
curl -s http://<windows-ip>:8000/health | jq

# Generate a 2-second test WAV (requires ffmpeg)
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=2" -ar 16000 -ac 1 /tmp/test.wav

# Transcribe
curl -s -X POST http://<windows-ip>:8000/v1/audio/transcriptions \
  -F "file=@/tmp/test.wav" \
  -F "model=whisper-large-v3" \
  -F "language=en" \
  -F "response_format=verbose_json" | jq

# Latency sanity-check
time curl -s -X POST http://<windows-ip>:8000/v1/audio/transcriptions \
  -F "file=@/tmp/test.wav" \
  -F "model=whisper-large-v3" \
  -F "response_format=json" > /dev/null
```

Expect total well under 1 second for a 2-second clip on a 3090.

If `/health` works but transcription hangs: firewall is dropping the multipart upload. Re-check the rule's profile (Private vs Public) matches your network classification.

## 7. Honest caveats

- **First model load = 60–90 sec.** The Task Scheduler trigger has a 30s delay; the HF cache download on first ever run can take 5+ minutes (3 GB). Run `python whisper_server.py` manually once before relying on autostart.

- **Desktop sleeps → bot timeouts.** Windows defaults sleep the box after 30 min idle. Disable: `Settings → System → Power → Screen and sleep → "When plugged in, put my device to sleep after" = Never`. Also `powercfg /change standby-timeout-ac 0` as admin.

- **Firewall blocks port 8000.** If `/health` from another machine times out but works from `localhost` on the desktop, the firewall rule is wrong. Re-run with profile `Any` if your network is classified Public.

- **VRAM pinned permanently.** The model holds ~5 GB VRAM 24/7 even when idle. If you game on the same box, stop the task in Task Scheduler before launching games.

- **cuDNN DLL not found at runtime.** Symptom: `Could not load library cudnn_ops64_9.dll`. The PATH update from step 1 only applies to new sessions. Task Scheduler tasks created before the PATH update won't see it — add `$env:Path += ";C:\Program Files\NVIDIA\CUDNN\v9.5\bin"` at the top of `whisper-server.ps1`.

- **Bot has no way to know the desktop is "warming up".** If the 30-sec startup delay isn't enough, the bot's first call will time out and fall back to Groq. Acceptable. Bump `LOCAL_WHISPER_TIMEOUT_SECONDS=15` if you see noisy fallback logs after reboots.

- **Tailscale IP changes after reinstall.** Pin the Windows desktop's Tailscale hostname instead of the IP: `LOCAL_WHISPER_URL=http://windesktop:8000/v1/audio/transcriptions` (with MagicDNS on).

- **Banglish quality vs. Groq.** Self-hosted `large-v3` matches Groq's exactly (same weights). If you notice quality drops, the likely culprit is `vad_filter=True` clipping short Bangla words at segment edges. Set `vad_filter=False` in the server code.

- **Concurrent requests serialize.** `faster-whisper` is not thread-safe for a single model instance. FastAPI queues them. Fine for one user; if family grows, set `WhisperModel(num_workers=2)` and accept the VRAM bump.

## Future: also run Gemma locally on the same 3090

Once Whisper is running, the same machine can host a local Gemma model (via Ollama) and the bot can cascade: `Gemini Cloud → Local Gemma → Cloud Whisper+Gemini fallback`. See the discussion in CLAUDE.md / chat history. Same RTX 3090, ~5GB Whisper + ~9GB Gemma 3 12B = 14 GB total, fits comfortably in 24GB.
