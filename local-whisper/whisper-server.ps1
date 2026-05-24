# Launcher that Task Scheduler invokes on logon.
# Activates the venv, logs to disk, and runs the FastAPI server.
$ErrorActionPreference = "Stop"
$root = "C:\local-whisper"
Set-Location -Path $root

# Some cuDNN installs only land on the user PATH; surface it for this process.
$cudnn = "C:\Program Files\NVIDIA\CUDNN\v9.5\bin"
if (Test-Path $cudnn) { $env:Path = "$cudnn;$env:Path" }

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log   = Join-Path $logDir "whisper-$stamp.log"

& "$root\.venv\Scripts\python.exe" "$root\whisper_server.py" *>&1 |
    Tee-Object -FilePath $log
