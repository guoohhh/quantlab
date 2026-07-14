param(
    [switch]$RunResearch
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$projectRoot = Split-Path -Parent $PSScriptRoot
$quantlab = Join-Path $projectRoot ".venv\Scripts\quantlab.exe"
$logDir = Join-Path $projectRoot "data\logs"
$logPath = Join-Path $logDir "daily-cycle.log"

if (-not (Test-Path -LiteralPath $quantlab)) {
    throw "QuantLab virtual environment was not found: $quantlab"
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Set-Location -LiteralPath $projectRoot

$arguments = @("daily-cycle")
if ($RunResearch) {
    $arguments += "--run-research"
}

"[$(Get-Date -Format o)] starting QuantLab daily cycle" | Out-File -FilePath $logPath -Append -Encoding utf8
& $quantlab @arguments 2>&1 | Out-File -FilePath $logPath -Append -Encoding utf8
if ($LASTEXITCODE -ne 0) {
    throw "QuantLab daily cycle failed with exit code $LASTEXITCODE; see $logPath"
}
"[$(Get-Date -Format o)] QuantLab daily cycle completed" | Out-File -FilePath $logPath -Append -Encoding utf8
