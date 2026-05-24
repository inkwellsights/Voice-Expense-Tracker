# Local Whisper fallback (RTX 3090)

A FastAPI server that runs `faster-whisper-large-v3` on a CUDA GPU and exposes the same OpenAI/Groq-compatible transcription endpoint. The bot tries this first; on connect-refused, timeout, or HTTP 503 it falls back to cloud Groq automatically.

## Why 503

A **GPU-busy gate** runs before every transcription. If GPU utilization is over 25% (someone's gaming) or another process is holding > 1 GB of VRAM beyond our baseline (background render, ML job), the server responds `503 gpu busy: ...` immediately. The bot interprets that as "use the cloud" — no manual intervention, no contention with whatever else is on the GPU.

Inspect what the gate sees in real time: `GET http://localhost:8000/gpu`.

## Install (one command, run on the 3090 desktop)

Open PowerShell (any user) and paste:

```powershell
irm https://raw.githubusercontent.com/inkwellsights/Voice-Expense-Tracker/main/local-whisper/bootstrap.ps1 -OutFile $env:TEMP\wb.ps1; & powershell -ExecutionPolicy Bypass -File $env:TEMP\wb.ps1
```

A UAC prompt appears (the installer needs admin to open the firewall and register the Task Scheduler entry). After ~5 minutes (mostly model download on first run), it prints the Tailscale + LAN URLs to put in the bot's `.env`.

## What the installer does

1. Verifies Python 3.10+ and `nvidia-smi` are present
2. Creates `C:\local-whisper\`, a venv, installs deps
3. Pre-downloads `Systran/faster-whisper-large-v3` (~3 GB, cached in `%USERPROFILE%\.cache\huggingface`)
4. Opens TCP/8000 in the Windows firewall (Private + Public)
5. Registers a Task Scheduler task "Whisper Server" that runs on log-on with +30s delay
6. Starts the task and probes `/health` until it responds

Re-running is safe — it overwrites the task and skips already-completed steps.

## Bot-side wiring

Add to the bot's `.env` (the URL is printed at the end of bootstrap):

```
LOCAL_WHISPER_URL=http://<tailscale-hostname>:8000/v1/audio/transcriptions
LOCAL_WHISPER_TIMEOUT_SECONDS=8
```

Restart the bot — `bot/services/transcriber.py` already cascades local-first → cloud on failure.

## Troubleshooting

- `Could not load library cudnn_ops64_9.dll` → install cuDNN 9.x for CUDA 12 (see `docs/LOCAL_WHISPER_SETUP.md` for full walkthrough), drop the DLLs into `C:\Program Files\NVIDIA\CUDNN\v9.5\bin`, restart the task.
- Health probe in bootstrap times out → check `C:\local-whisper\logs\*.log`.
- Bot keeps falling back to Groq → hit `http://<tailscale>:8000/gpu` from the casaos box; if `busy: true`, lower the thresholds via env (e.g. `GPU_UTIL_BUSY_PCT=40`).
- Want to disable the local server temporarily → `Stop-ScheduledTask -TaskName "Whisper Server"`. The bot will fall back to Groq on connect-refused.

## Uninstall

```powershell
Unregister-ScheduledTask -TaskName "Whisper Server" -Confirm:$false
Remove-NetFirewallRule -DisplayName "Whisper Server 8000"
Remove-Item -Recurse -Force C:\local-whisper
```
