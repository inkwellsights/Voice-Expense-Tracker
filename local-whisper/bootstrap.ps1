# One-shot installer for the local Whisper fallback server.
#
# What this does (idempotent):
#  1. Checks Python 3.10+ and nvidia-smi are present
#  2. Creates C:\local-whisper\, a venv, and installs deps
#  3. Pulls whisper_server.py + whisper-server.ps1 from this repo
#  4. Pre-downloads the model (~3 GB) so the first request isn't slow
#  5. Opens TCP/8000 in the firewall (Private + Public profiles)
#  6. Registers a Task Scheduler task "Whisper Server" that runs on logon
#  7. Starts it now
#  8. Prints the LAN URL + the Tailscale URL so you can drop into .env
#
# Run from any non-admin PowerShell:
#   irm https://raw.githubusercontent.com/inkwellsights/Voice-Expense-Tracker/main/local-whisper/bootstrap.ps1 -OutFile $env:TEMP\wb.ps1; & powershell -ExecutionPolicy Bypass -File $env:TEMP\wb.ps1
#
# The script will self-elevate (UAC prompt) if not already admin.

$ErrorActionPreference = "Stop"
$REPO_RAW = "https://raw.githubusercontent.com/inkwellsights/Voice-Expense-Tracker/main/local-whisper"
$ROOT     = "C:\local-whisper"
$TASKNAME = "Whisper Server"
$PORT     = 8000

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "    OK $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    !! $msg" -ForegroundColor Yellow }
function Die($msg)        { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# --- Step 0: self-elevate if not admin ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Step "Re-launching elevated (UAC prompt)..."
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) {
        # Running via iex/irm with no on-disk path — save self first.
        $scriptPath = "$env:TEMP\wb.ps1"
        $MyInvocation.MyCommand.Definition | Out-File -FilePath $scriptPath -Encoding UTF8
    }
    Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -NoExit -File `"$scriptPath`""
    exit 0
}

# --- Step 1: Python check ---
Write-Step "Checking Python 3.10+"
$pyPath = $null
foreach ($cmd in @("python", "py -3")) {
    try {
        $ver = & cmd.exe /c "$cmd --version 2>&1"
        if ($ver -match "Python (\d+)\.(\d+)") {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -eq 3 -and $min -ge 10) {
                $pyPath = $cmd
                Write-OK "found $ver via '$cmd'"
                break
            }
        }
    } catch {}
}
if (-not $pyPath) {
    Die "Python 3.10+ not found. Install from https://www.python.org/downloads/windows/ (tick 'Add to PATH'), then re-run."
}

# --- Step 2: nvidia-smi check ---
Write-Step "Checking nvidia-smi"
try {
    $smi = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
    Write-OK "$smi"
} catch {
    Die "nvidia-smi not found. Install the NVIDIA driver from https://www.nvidia.com/Download/index.aspx then re-run."
}

# --- Step 3: folder + files ---
Write-Step "Setting up $ROOT"
if (-not (Test-Path $ROOT)) { New-Item -ItemType Directory -Path $ROOT | Out-Null }
Set-Location $ROOT

foreach ($f in @("whisper_server.py", "whisper-server.ps1", "requirements.txt")) {
    Write-Step "Fetching $f"
    Invoke-WebRequest -UseBasicParsing -Uri "$REPO_RAW/$f" -OutFile (Join-Path $ROOT $f)
    Write-OK $f
}

# --- Step 4: venv + deps ---
if (-not (Test-Path "$ROOT\.venv\Scripts\python.exe")) {
    Write-Step "Creating venv"
    & cmd.exe /c "$pyPath -m venv `"$ROOT\.venv`""
    Write-OK ".venv ready"
} else {
    Write-OK ".venv already exists"
}

Write-Step "Installing dependencies (this can take 2-3 minutes)"
& "$ROOT\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$ROOT\.venv\Scripts\python.exe" -m pip install -r "$ROOT\requirements.txt" --quiet
Write-OK "deps installed"

# --- Step 5: pre-download model ---
Write-Step "Pre-downloading model (~3 GB on first run, cached after)"
$dlScript = @'
import os, sys, time
from faster_whisper import WhisperModel
t = time.time()
print("Downloading + loading Systran/faster-whisper-large-v3 ...", flush=True)
m = WhisperModel("Systran/faster-whisper-large-v3", device="cuda", compute_type="float16")
print(f"Loaded in {time.time()-t:.1f}s", flush=True)
'@
$dlScript | Out-File -FilePath "$ROOT\_warmup.py" -Encoding UTF8
& "$ROOT\.venv\Scripts\python.exe" "$ROOT\_warmup.py"
Remove-Item "$ROOT\_warmup.py" -Force
Write-OK "model cached"

# --- Step 6: firewall rule ---
Write-Step "Opening TCP/$PORT in firewall"
$rule = Get-NetFirewallRule -DisplayName "Whisper Server $PORT" -ErrorAction SilentlyContinue
if ($rule) {
    Write-OK "rule already exists"
} else {
    New-NetFirewallRule -DisplayName "Whisper Server $PORT" -Direction Inbound -Protocol TCP -LocalPort $PORT -Action Allow -Profile Private,Public | Out-Null
    Write-OK "rule created (Private+Public)"
}

# --- Step 7: Task Scheduler ---
Write-Step "Registering scheduled task '$TASKNAME'"
$existing = Get-ScheduledTask -TaskName $TASKNAME -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TASKNAME -Confirm:$false
    Write-OK "removed old task"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ROOT\whisper-server.ps1`"" `
    -WorkingDirectory $ROOT

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT30S"  # let GPU driver settle

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TASKNAME `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Local Whisper fallback for Voice Expense Tracker" | Out-Null
Write-OK "task registered (At log on, +30s delay)"

# --- Step 8: start it now ---
Write-Step "Starting service"
Start-ScheduledTask -TaskName $TASKNAME
Start-Sleep -Seconds 5
Write-OK "task started (model load takes ~10s after this)"

# --- Step 9: health probe loop ---
Write-Step "Waiting for /health to respond (up to 60s)"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://localhost:$PORT/health" -TimeoutSec 2
        if ($r.ok) { $ok = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}
if (-not $ok) {
    Write-Warn "health probe didn't succeed in 60s — check C:\local-whisper\logs\ for errors"
} else {
    Write-OK "server up at http://localhost:$PORT"
}

# --- Step 10: report URLs ---
Write-Host ""
Write-Step "DONE. Use one of these in the bot's .env LOCAL_WHISPER_URL:"
Write-Host ""

$tsHost = ""
try {
    $ts = & tailscale status --json 2>$null | ConvertFrom-Json
    if ($ts -and $ts.Self -and $ts.Self.HostName) {
        $tsHost = $ts.Self.HostName
        Write-Host "  Tailscale (recommended, works across networks):"
        Write-Host "    LOCAL_WHISPER_URL=http://$tsHost`:$PORT/v1/audio/transcriptions" -ForegroundColor Green
    }
} catch {}

$lan = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.PrefixOrigin -ne 'WellKnown' -and $_.IPAddress -notmatch '^169\.' } |
        Select-Object -First 1 -ExpandProperty IPAddress)
if ($lan) {
    Write-Host "  LAN (same network only):"
    Write-Host "    LOCAL_WHISPER_URL=http://$lan`:$PORT/v1/audio/transcriptions" -ForegroundColor Green
}

Write-Host ""
Write-Host "  Inspect the gate (open in browser):  http://localhost:$PORT/gpu"
Write-Host "  Logs:                                 C:\local-whisper\logs\"
Write-Host "  Manage the task:                      taskschd.msc -> 'Whisper Server'"
Write-Host ""
if ($tsHost) {
    Write-Host "==> Paste this back to Claude so the bot can be wired up:" -ForegroundColor Cyan
    Write-Host "    Tailscale hostname: $tsHost" -ForegroundColor Green
}
