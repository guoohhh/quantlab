<#
.SYNOPSIS
Starts the local QuantLab product UI in an isolated hackathon configuration.

.DESCRIPTION
Runs Streamlit on 127.0.0.1 together with a local background job worker, so
the AI assistant and expert roundtable (both background jobs) actually execute
instead of staying stuck at "queued". The launcher uses an isolated database,
skips the project .env file, disables external LLM providers and automatic
trusted-data refresh, and never starts the scheduler, API, formal experiments,
or any broker integration.

.PARAMETER Port
Preferred local port. If it is occupied, the launcher selects the next free
port within PortSearchRange and prints the final URL.

.PARAMETER PortSearchRange
Number of consecutive ports to inspect, starting with Port.

.PARAMETER NoBrowser
Starts Streamlit without opening the default browser.

.PARAMETER CheckOnly
Runs path, dependency, dataset, import, and port checks without starting a
server or creating a database.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-hackathon-demo.ps1

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-hackathon-demo.ps1 -CheckOnly
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8501,

    [ValidateRange(1, 100)]
    [int]$PortSearchRange = 20,

    [switch]$NoBrowser,
    [switch]$NoWorker,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Dashboard = Join-Path $ProjectRoot "dashboard\app.py"
$DefaultConfig = Join-Path $ProjectRoot "config\default.toml"
$FrozenDataset = Join-Path $ProjectRoot "data\demo\historical-research-v1.json"
$DemoRoot = Join-Path $ProjectRoot "data\demo"
$IsolatedDatabase = Join-Path $DemoRoot "hackathon-ui.db"
$IsolatedData = Join-Path $DemoRoot "hackathon-ui-data"

function Assert-RequiredFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description was not found: $Path"
    }
}

function Test-LocalPortAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [int]$CandidatePort
    )

    $Listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $CandidatePort
    )
    try {
        $Listener.Start()
        return $true
    }
    catch [System.Net.Sockets.SocketException] {
        return $false
    }
    finally {
        $Listener.Stop()
    }
}

function Resolve-LocalPort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$PreferredPort,

        [Parameter(Mandatory = $true)]
        [int]$SearchRange
    )

    $LastPort = [Math]::Min(65535, $PreferredPort + $SearchRange - 1)
    foreach ($CandidatePort in $PreferredPort..$LastPort) {
        if (Test-LocalPortAvailable -CandidatePort $CandidatePort) {
            return $CandidatePort
        }
    }
    throw "No free local port was found from $PreferredPort through $LastPort."
}

Assert-RequiredFile -Path $Python -Description "QuantLab virtual-environment Python"
Assert-RequiredFile -Path $Dashboard -Description "Streamlit dashboard entry"
Assert-RequiredFile -Path $DefaultConfig -Description "QuantLab default configuration"
Assert-RequiredFile -Path $FrozenDataset -Description "Frozen historical demo dataset"

# The process-level environment wins over local dotenv values. A unique missing
# env-file path prevents Settings.load() from importing real provider secrets.
$env:QUANTLAB_ENV_FILE = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("quantlab-hackathon-no-env-{0}.env" -f [guid]::NewGuid().ToString("N"))
$env:QUANTLAB_DATABASE_PATH = $IsolatedDatabase
$env:QUANTLAB_DATA_DIR = $IsolatedData
$env:QUANTLAB_LLM_PROVIDER = "mock"
$env:QUANTLAB_LLM_MODEL = ""
$env:QUANTLAB_LLM_BASE_URL = ""
$env:QUANTLAB_OPENAI_ENABLED = "false"
$env:QUANTLAB_DEEPSEEK_ENABLED = "false"
$env:QUANTLAB_TRUSTED_DATA_AUTO_REFRESH = "false"
$env:QUANTLAB_ENABLE_TEST_QUOTES = "0"
$env:OPENAI_API_KEY = ""
$env:OPENAI_API_KEYS = ""
$env:DEEPSEEK_API_KEY = ""
$env:DEEPSEEK_API_KEYS = ""
$env:PYTHONUNBUFFERED = "1"

$PythonPathEntries = @(
    (Join-Path $ProjectRoot "src"),
    $ProjectRoot
)
if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $PythonPathEntries += $env:PYTHONPATH
}
$env:PYTHONPATH = $PythonPathEntries -join [System.IO.Path]::PathSeparator

Push-Location -LiteralPath $ProjectRoot
try {
    $StreamlitVersion = & $Python -m streamlit --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Streamlit dependency check failed: $StreamlitVersion"
    }

    $DatasetProbe = & $Python -c @"
from quantlab.config import Settings
from quantlab.workflows.product_demo import historical_demo_dataset

dataset = historical_demo_dataset(Settings.load())
assert dataset['research_only'] is True
assert dataset['training_eligible'] is False
assert dataset['forward_scorecard_eligible'] is False
print(dataset['dataset_fingerprint'])
"@ 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Frozen demo dataset validation failed: $DatasetProbe"
    }

    $SelectedPort = Resolve-LocalPort -PreferredPort $Port -SearchRange $PortSearchRange
    $Url = "http://127.0.0.1:$SelectedPort"
    if ($SelectedPort -ne $Port) {
        Write-Host "Preferred port $Port is occupied; selected $SelectedPort instead." -ForegroundColor Yellow
    }

    Write-Host "QuantLab hackathon preflight passed." -ForegroundColor Green
    Write-Host "Streamlit: $StreamlitVersion"
    Write-Host "Frozen dataset SHA-256: $DatasetProbe"
    Write-Host "Local URL: $Url"
    Write-Host "Isolated UI database: $IsolatedDatabase"
    if ($NoWorker) {
        Write-Host "Safety boundary: local Streamlit only (worker disabled; AI assistant and roundtable will stay queued)."
    }
    else {
        Write-Host "Safety boundary: local Streamlit + background job worker; no .env secrets, scheduler, API, formal experiment, or broker execution."
    }

    if ($CheckOnly) {
        Write-Host "CheckOnly completed; no server was started and no database was created."
        return
    }

    $Headless = if ($NoBrowser) { "true" } else { "false" }
    $Arguments = @(
        "-m", "streamlit", "run", $Dashboard,
        "--server.address", "127.0.0.1",
        "--server.port", $SelectedPort.ToString(),
        "--server.headless", $Headless,
        "--server.fileWatcherType", "none",
        "--server.runOnSave", "false",
        "--browser.gatherUsageStats", "false",
        "--client.toolbarMode", "viewer",
        "--client.showErrorDetails", "none"
    )

    # The AI assistant (chat_request) and expert roundtable (roundtable_request)
    # are submitted as background jobs. Without a worker consuming the queue they
    # stay at "queued" forever. Start a local worker loop alongside Streamlit and
    # stop it automatically when this launcher exits.
    $WorkerProcess = $null
    if (-not $NoWorker) {
        Write-Host "Starting the background job worker." -ForegroundColor Cyan
        $WorkerScript = @"
import time

from quantlab.config import Settings
from quantlab.runtime.worker import JobWorker

worker = JobWorker(Settings.load(), worker_id="hackathon-demo-worker")
while True:
    try:
        worker.run_until_empty(20)
    except Exception as exc:
        print(f"[worker] error: {exc}", flush=True)
    time.sleep(1.0)
"@
        $WorkerProcess = Start-Process -FilePath $Python -ArgumentList @("-c", $WorkerScript) -PassThru -NoNewWindow
    }

    try {
        Write-Host "Starting the product UI. Press Ctrl+C in this window to stop it." -ForegroundColor Cyan
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Streamlit exited with code $LASTEXITCODE."
        }
    }
    finally {
        if ($WorkerProcess -and -not $WorkerProcess.HasExited) {
            Stop-Process -Id $WorkerProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
finally {
    Pop-Location
}
