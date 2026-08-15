param(
    [ValidateSet("Preview", "Install", "Remove")]
    [string]$Action = "Preview",
    [string]$TaskName = "StagingPortfolioProviderIngestion",
    [string]$PythonExe = "C:\Users\josel\Miniconda3\python.exe",
    [int]$PollMinutes = 0,
    [int]$MaxScheduledAttempts = 0,
    [int]$ExecutionLimitMinutes = 0
)

$ErrorActionPreference = "Stop"
$ScriptPath = (Resolve-Path (Join-Path $PSScriptRoot "run_due.py")).Path
$ConfigPath = (Resolve-Path (Join-Path $PSScriptRoot "..\config.yaml")).Path

function Read-ProviderIntegerSetting {
    param([Parameter(Mandatory = $true)][string]$Key)
    $Match = Select-String `
        -LiteralPath $ConfigPath `
        -Pattern ("^\s{{2}}{0}:\s*([0-9]+)\s*$" -f [regex]::Escape($Key))
    if ($Match.Count -ne 1 -or $Match.Matches.Count -ne 1) {
        throw "Expected exactly one provider_ingestion.$Key integer in $ConfigPath"
    }
    return [int]$Match.Matches[0].Groups[1].Value
}

$ConfiguredPollMinutes = Read-ProviderIntegerSetting -Key "scheduler_poll_minutes"
$ConfiguredMaxScheduledAttempts = Read-ProviderIntegerSetting -Key "max_scheduled_attempts"
$ConfiguredExecutionLimitMinutes = (Read-ProviderIntegerSetting -Key "capture_timeout_minutes") + 20

if ($PollMinutes -le 0) {
    $PollMinutes = $ConfiguredPollMinutes
} elseif ($PollMinutes -ne $ConfiguredPollMinutes) {
    throw "PollMinutes must match provider_ingestion.scheduler_poll_minutes=$ConfiguredPollMinutes"
}
if ($MaxScheduledAttempts -le 0) {
    $MaxScheduledAttempts = $ConfiguredMaxScheduledAttempts
} elseif ($MaxScheduledAttempts -ne $ConfiguredMaxScheduledAttempts) {
    throw "MaxScheduledAttempts must match provider_ingestion.max_scheduled_attempts=$ConfiguredMaxScheduledAttempts"
}
if ($ExecutionLimitMinutes -le 0) {
    $ExecutionLimitMinutes = $ConfiguredExecutionLimitMinutes
} elseif ($ExecutionLimitMinutes -ne $ConfiguredExecutionLimitMinutes) {
    throw "ExecutionLimitMinutes must match capture_timeout_minutes plus the 20-minute sealing margin ($ConfiguredExecutionLimitMinutes)"
}
if ($PollMinutes -le 0) {
    throw "PollMinutes must be positive"
}
if ($MaxScheduledAttempts -lt 2) {
    throw "MaxScheduledAttempts must be at least two"
}
if ($ExecutionLimitMinutes -le 20) {
    throw "ExecutionLimitMinutes must leave a positive child-runtime allowance"
}
$RestartCount = $MaxScheduledAttempts - 1
$TaskCommand = ('"{0}" "{1}" --config "{2}"' -f $PythonExe, $ScriptPath, $ConfigPath)

if ($Action -eq "Preview") {
    Write-Output "Task: $TaskName"
    Write-Output "Cadence: every $PollMinutes minutes; run_due.py applies the America/New_York windows"
    Write-Output "Attempts: $MaxScheduledAttempts total; task restarts: $RestartCount; execution limit: $ExecutionLimitMinutes minutes"
    Write-Output "Command: $TaskCommand"
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
    -Argument ('"{0}" --config "{1}"' -f $ScriptPath, $ConfigPath)
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $PollMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount $RestartCount `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionLimitMinutes)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $TaskAction `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Current-time-only FMP/Alpha estimate capture dispatcher" `
    -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
exit 0
