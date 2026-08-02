param(
    [ValidateSet("Preview", "Install", "Remove")]
    [string]$Action = "Preview",
    [string]$TaskName = "StagingPortfolioProviderIngestion",
    [string]$PythonExe = "C:\Users\josel\Miniconda3\python.exe"
)

$ErrorActionPreference = "Stop"
$ScriptPath = (Resolve-Path (Join-Path $PSScriptRoot "run_due.py")).Path
$ConfigPath = (Resolve-Path (Join-Path $PSScriptRoot "..\config.yaml")).Path
$TaskCommand = ('"{0}" "{1}" --config "{2}"' -f $PythonExe, $ScriptPath, $ConfigPath)

if ($Action -eq "Preview") {
    Write-Output "Task: $TaskName"
    Write-Output "Cadence: every 10 minutes; run_due.py applies the America/New_York windows"
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
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $TaskAction `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Current-time-only FMP/Alpha estimate capture dispatcher" `
    -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
exit 0
