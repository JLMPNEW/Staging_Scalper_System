param(
    [ValidateSet("Preview", "Install", "Remove")]
    [string]$Action = "Preview",
    [string]$TaskName = "StagingMacroEvidenceMonthly",
    [string]$PythonExe = "C:\Users\josel\miniconda3\envs\scalper-staging\python.exe",
    [string]$LocalTime = "11:00"
)

$ErrorActionPreference = "Stop"
$RunnerPath = (Resolve-Path (Join-Path $PSScriptRoot "run_macro_evidence_refresh.py")).Path
$ParsedTime = [TimeSpan]::ParseExact($LocalTime, "hh\:mm", $null)
$StartAt = [DateTime]::Today.Add($ParsedTime)
$TaskArguments = ('"{0}"' -f $RunnerPath)

if ($Action -eq "Preview") {
    Write-Output "Task: $TaskName"
    Write-Output "Cadence: first Saturday of each month at $LocalTime local time"
    Write-Output "Mode: rerun industry ablation + shadow backtest so macro evidence accumulates"
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
    -Argument $TaskArguments `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
# First Saturday of the month, after the weekly survivorship rebuild (09:00).
$Trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek Saturday -At $StartAt
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $TaskAction `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Every-4-weeks macro evidence refresh (industry ablation + shadow backtest) after the Saturday survivorship rebuild" `
    -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName every 4 weeks (Saturday $LocalTime)"
exit 0
