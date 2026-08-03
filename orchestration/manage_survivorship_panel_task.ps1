param(
    [ValidateSet("Preview", "Install", "Remove")]
    [string]$Action = "Preview",
    [string]$TaskName = "StagingSurvivorshipPanelWeekly",
    [string]$PythonExe = "C:\Users\josel\miniconda3\envs\scalper-staging\python.exe",
    [string]$LocalTime = "09:00"
)

$ErrorActionPreference = "Stop"
$RunnerPath = (Resolve-Path (Join-Path $PSScriptRoot "..\portfolio_layer\backtest\15b_build_survivorship_panel.py")).Path
$ParsedTime = [TimeSpan]::ParseExact($LocalTime, "hh\:mm", $null)
$StartAt = [DateTime]::Today.Add($ParsedTime)
# --build-date defaults to the run day inside 15b, so the panel right edge
# always advances to the latest completed trading day.
$TaskArguments = ('"{0}"' -f $RunnerPath)
$Days = @("Saturday")

if ($Action -eq "Preview") {
    Write-Output "Task: $TaskName"
    Write-Output "Cadence: Saturday at $LocalTime local machine time"
    Write-Output "Mode: rebuild the Stage 11 survivorship price panel with a current right edge"
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
    -WorkingDirectory (Split-Path -Parent (Split-Path -Parent $RunnerPath))
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
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $TaskAction `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Weekly survivorship-complete price panel rebuild so historical/non-universe tickers stay fresh for the macro layers" `
    -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName at $LocalTime local time (Saturday)"
exit 0
