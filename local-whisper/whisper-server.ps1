# Launcher that Task Scheduler invokes on logon.
# Activates the venv, logs to disk, and runs the FastAPI server.
#
# DO NOT set $ErrorActionPreference = "Stop" here. Python writes its log
# lines to stderr, which PowerShell would interpret as error records and
# terminate the child process on the first one.

$root = "C:\local-whisper"
Set-Location -Path $root

# Add the venv-bundled cuDNN + cuBLAS DLLs to PATH so ctranslate2 finds them.
# (Installed via the `nvidia-cudnn-cu12` and `nvidia-cublas-cu12` pip packages.)
$nvidiaBins = @(
    "$root\.venv\Lib\site-packages\nvidia\cudnn\bin",
    "$root\.venv\Lib\site-packages\nvidia\cublas\bin",
    "$root\.venv\Lib\site-packages\nvidia\cuda_nvrtc\bin"
) | Where-Object { Test-Path $_ }
if ($nvidiaBins) { $env:Path = ($nvidiaBins -join ';') + ';' + $env:Path }

# Legacy: support a manually-installed cuDNN at the standard NVIDIA path too.
$cudnnManual = "C:\Program Files\NVIDIA\CUDNN\v9.5\bin"
if (Test-Path $cudnnManual) { $env:Path = "$cudnnManual;$env:Path" }

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log   = Join-Path $logDir "whisper-$stamp.log"

# cmd.exe handles the stdout+stderr merge without PowerShell's error-record
# coercion. Tee the result into the log file so the server's output lands
# both on the console and on disk.
& cmd.exe /c "`"$root\.venv\Scripts\python.exe`" `"$root\whisper_server.py`" 2>&1" |
    Tee-Object -FilePath $log
