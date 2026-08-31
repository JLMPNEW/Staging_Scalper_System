param(
    [ValidateSet("Preview", "Install", "Verify", "Remove")]
    [string]$Action = "Preview",
    [string]$TaskName = "StagingPortfolioNightlyOrchestrator",
    [string]$PythonExe = "C:\Users\josel\Miniconda3\python.exe",
    [string]$LocalTime = "23:00"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunnerPath = (Resolve-Path (Join-Path $PSScriptRoot "run_nightly.py")).Path
$ConfigPath = (Resolve-Path (Join-Path $PSScriptRoot "..\portfolio_layer\config.yaml")).Path
$ParsedTime = [TimeSpan]::ParseExact($LocalTime, "hh\:mm", $null)
$StartAt = [DateTime]::Today.Add($ParsedTime)
$TaskArguments = ('"{0}" --config "{1}"' -f $RunnerPath, $ConfigPath)
$Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$ExpectedUser = [Environment]::UserName

function Assert-NightlyTask {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($Task.State -eq "Disabled") {
        throw "Scheduled task is disabled: $TaskName"
    }
    if (($Task.Principal.UserId -split '\\')[-1] -ne $ExpectedUser) {
        throw "Scheduled task user mismatch: $($Task.Principal.UserId)"
    }
    if ([string]$Task.Principal.LogonType -ne "Interactive") {
        throw "Scheduled task must use the interactive user session for TWS and user-profile data: $($Task.Principal.LogonType)"
    }
    $ActualAction = @($Task.Actions)[0]
    if ($null -eq $ActualAction) {
        throw "Scheduled task has no action: $TaskName"
    }
    if ([IO.Path]::GetFullPath($ActualAction.Execute) -ne [IO.Path]::GetFullPath($PythonExe)) {
        throw "Scheduled task Python mismatch: $($ActualAction.Execute)"
    }
    if ($ActualAction.Arguments -ne $TaskArguments) {
        throw "Scheduled task arguments mismatch: $($ActualAction.Arguments)"
    }
    if (
        [string]::IsNullOrWhiteSpace($ActualAction.WorkingDirectory) -or
        [IO.Path]::GetFullPath($ActualAction.WorkingDirectory) -ne [IO.Path]::GetFullPath($RepoRoot)
    ) {
        throw "Scheduled task working directory mismatch: $($ActualAction.WorkingDirectory)"
    }
    $ActualTrigger = @($Task.Triggers)[0]
    if ($null -eq $ActualTrigger -or [int]$ActualTrigger.DaysOfWeek -ne 62) {
        throw "Scheduled task weekday trigger mismatch"
    }
    if (([DateTime]$ActualTrigger.StartBoundary).ToString("HH:mm") -ne $LocalTime) {
        throw "Scheduled task time mismatch: $($ActualTrigger.StartBoundary)"
    }
    $Settings = $Task.Settings
    if ([string]$Settings.MultipleInstances -ne "IgnoreNew") {
        throw "Scheduled task overlap policy mismatch: $($Settings.MultipleInstances)"
    }
    if (-not $Settings.StartWhenAvailable) {
        throw "Scheduled task must start when available after a missed trigger"
    }
    if ([int]$Settings.RestartCount -ne 2 -or [string]$Settings.RestartInterval -ne "PT30M") {
        throw "Scheduled task restart policy mismatch: count=$($Settings.RestartCount) interval=$($Settings.RestartInterval)"
    }
    if ([string]$Settings.ExecutionTimeLimit -ne "PT18H") {
        throw "Scheduled task execution limit mismatch: $($Settings.ExecutionTimeLimit)"
    }
    if (-not $Settings.WakeToRun) {
        throw "Scheduled task WakeToRun must be enabled"
    }
    if (-not $Settings.RunOnlyIfNetworkAvailable) {
        throw "Scheduled task network requirement must be enabled"
    }
    if ($Settings.DisallowStartIfOnBatteries -or $Settings.StopIfGoingOnBatteries) {
        throw (
            "Scheduled task battery policy mismatch: disallow_start=$($Settings.DisallowStartIfOnBatteries) " +
            "stop_if_battery=$($Settings.StopIfGoingOnBatteries)"
        )
    }
    Write-Output (
        "Verified scheduled task: $TaskName " +
        "(enabled, Monday-Friday at $LocalTime local time, IgnoreNew, restart=2x30m, limit=18h)"
    )
}

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

if ($Action -eq "Verify") {
    Assert-NightlyTask
    exit 0
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}

$TaskAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $TaskArguments `
    -WorkingDirectory $RepoRoot
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
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal `
    -UserId $ExpectedUser `
    -LogonType Interactive `
    -RunLevel Limited
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $TaskAction `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Fail-closed nightly sector and portfolio catch-up orchestrator" `
    -Force | Out-Null
Enable-ScheduledTask -TaskName $TaskName | Out-Null
Assert-NightlyTask
Write-Output "Installed scheduled task: $TaskName at $LocalTime local time (Monday-Friday)"
exit 0
