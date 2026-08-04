param(
    [ValidateSet("Preview", "Install", "Remove")]
    [string]$Action = "Preview",
    [string]$TaskName = "StagingPortfolioNightlyOrchestrator",
    [string]$PythonExe = "C:\Users\josel\Miniconda3\python.exe",
    [string]$LocalTime = "23:00"
)

$ErrorActionPreference = "Stop"
$RunnerPath = (Resolve-Path (Join-Path $PSScriptRoot "run_nightly.py")).Path
$ConfigPath = (Resolve-Path (Join-Path $PSScriptRoot "..\portfolio_layer\config.yaml")).Path
$ParsedTime = [TimeSpan]::ParseExact($LocalTime, "hh\:mm", $null)
$StartAt = [DateTime]::Today.Add($ParsedTime)
$TaskArguments = ('"{0}" --config "{1}"' -f $RunnerPath, $ConfigPath)
$Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

if ($Action -eq "Preview") {
    Write-Output "Task: $TaskName"
    Write-Output "Cadence: Monday-Friday at $LocalTime local machine time"
    Write-Output "Mode: provider-store validation, late IB reconciliation, then master --catch-up"
    Write-Output "Command: `"$PythonExe`" $TaskArguments"
    exit 0
}

if ($Action -eq "Remove") {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    exit 0
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}

$TaskAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $TaskArguments
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek $Days `
    -At $StartAt
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 18) `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $TaskAction `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Fail-closed nightly sector and portfolio catch-up orchestrator" `
    -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName at $LocalTime local time (Monday-Friday)"
exit 0
