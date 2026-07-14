param(
    [string]$At = "18:30",
    [switch]$RunResearch
)

$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "run-daily-cycle.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Daily runner was not found: $runner"
}

$runnerArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
if ($RunResearch) {
    $runnerArguments += " -RunResearch"
}
$actionParameters = @{
    Execute = "powershell.exe"
    Argument = $runnerArguments
    WorkingDirectory = (Split-Path -Parent $PSScriptRoot)
}
$action = New-ScheduledTaskAction @actionParameters
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settingsParameters = @{
    StartWhenAvailable = $true
    ExecutionTimeLimit = (New-TimeSpan -Hours 2)
}
$settings = New-ScheduledTaskSettingsSet @settingsParameters

$registerParameters = @{
    TaskName = "QuantLab-DailyCycle"
    Action = $action
    Trigger = $trigger
    Settings = $settings
    Description = "QuantLab paper trading, learning settlement and daily investor brief"
    Force = $true
}
Register-ScheduledTask @registerParameters

Write-Output "Installed QuantLab-DailyCycle at $At. API keys remain in the local .env file."
