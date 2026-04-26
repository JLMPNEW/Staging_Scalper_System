param(
    [string]$PythonExe = "C:\Users\josel\Miniconda3\python.exe",
    [string]$SecConfig = "fundamental_data/config_sec_fundamentals.yaml",
    [string]$StartDate = "2019-03-15",
    [string]$EndDate = "",
    [int]$SecSnapshotBatchSize = 60,
    [int]$SecSnapshotMaxWorkers = 2,
    [string]$LogDir = "fundamental_data/logs",
    [string]$StateFile = "fundamental_data/logs/sec_history_state.json"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Get-PreviousOrSameBusinessDate {
    param([datetime]$DateValue)
    $out = $DateValue.Date
    while ($out.DayOfWeek -in @("Saturday", "Sunday")) {
        $out = $out.AddDays(-1)
    }
    return $out
}

function Get-NextBusinessDate {
    param([datetime]$DateValue)
    $out = $DateValue.Date.AddDays(1)
    while ($out.DayOfWeek -in @("Saturday", "Sunday")) {
        $out = $out.AddDays(1)
    }
    return $out
}

function Get-BusinessDates {
    param(
        [datetime]$StartValue,
        [datetime]$EndValue
    )
    $dates = New-Object System.Collections.Generic.List[datetime]
    $cur = $StartValue.Date
    while ($cur -le $EndValue.Date) {
        if ($cur.DayOfWeek -notin @("Saturday", "Sunday")) {
            [void]$dates.Add($cur)
        }
        $cur = $cur.AddDays(1)
    }
    return $dates
}

function Resolve-RepoPath {
    param([string]$RawPath)
    if ([string]::IsNullOrWhiteSpace($RawPath)) {
        return $null
    }
    if ([System.IO.Path]::IsPathRooted($RawPath)) {
        return [System.IO.Path]::GetFullPath($RawPath)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $RawPath))
}

function Load-State {
    param([string]$PathValue)
    if (-not (Test-Path -LiteralPath $PathValue)) {
        return [ordered]@{
            sec_period_last_asof = ""
            sec_snapshot_last_asof = ""
            completed_utc = $null
        }
    }
    $raw = Get-Content -LiteralPath $PathValue -Raw | ConvertFrom-Json
    return [ordered]@{
        sec_period_last_asof = [string]$raw.sec_period_last_asof
        sec_snapshot_last_asof = [string]$raw.sec_snapshot_last_asof
        completed_utc = if ($null -eq $raw.completed_utc -or [string]::IsNullOrWhiteSpace([string]$raw.completed_utc)) { $null } else { [string]$raw.completed_utc }
    }
}

function Save-State {
    param(
        [hashtable]$State,
        [string]$PathValue
    )
    $State | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $PathValue -Encoding utf8
}

function Write-MasterLog {
    param(
        [string]$PathValue,
        [string]$Message
    )
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $PathValue -Value $line
    Write-Output $line
}

function Invoke-LoggedCommand {
    param(
        [string]$Name,
        [string[]]$CommandParts,
        [string]$MasterLog,
        [string]$CommandLog
    )
    Write-MasterLog -PathValue $MasterLog -Message ("START {0}: {1}" -f $Name, ($CommandParts -join " "))
    $argList = @()
    if ($CommandParts.Length -gt 1) {
        $argList = $CommandParts[1..($CommandParts.Length - 1)]
    }
    $stdoutLog = $CommandLog
    $stderrLog = "{0}.stderr" -f $CommandLog
    if (Test-Path -LiteralPath $stdoutLog) {
        Remove-Item -LiteralPath $stdoutLog -Force
    }
    if (Test-Path -LiteralPath $stderrLog) {
        Remove-Item -LiteralPath $stderrLog -Force
    }
    $proc = Start-Process `
        -FilePath $CommandParts[0] `
        -ArgumentList $argList `
        -WorkingDirectory $repoRoot `
        -NoNewWindow `
        -Wait `
        -PassThru `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog
    $exitCode = $proc.ExitCode
    if (Test-Path -LiteralPath $stderrLog) {
        Get-Content -LiteralPath $stderrLog | Add-Content -LiteralPath $stdoutLog
        Remove-Item -LiteralPath $stderrLog -Force
    }
    Write-MasterLog -PathValue $MasterLog -Message ("END {0}: exit_code={1} log={2}" -f $Name, $exitCode, $CommandLog)
    if ($exitCode -ne 0) {
        throw ("Command '{0}' failed with exit code {1}. See {2}" -f $Name, $exitCode, $CommandLog)
    }
    return @(
        if (Test-Path -LiteralPath $stdoutLog) {
            Get-Content -LiteralPath $stdoutLog
        }
    )
}

function Get-RegexValue {
    param(
        [string[]]$Lines,
        [string]$Pattern
    )
    foreach ($line in $Lines) {
        if ($line -match $Pattern) {
            return $Matches[1]
        }
    }
    return ""
}

function Parse-RangeEnd {
    param([string[]]$Lines)
    return Get-RegexValue -Lines $Lines -Pattern "Range:\s+\d{4}-\d{2}-\d{2}\s+->\s+(\d{4}-\d{2}-\d{2})"
}

function Parse-FailedBuilds {
    param([string[]]$Lines)
    $raw = Get-RegexValue -Lines $Lines -Pattern "Failed builds:\s+([0-9,]+)"
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return 0
    }
    return [int]($raw -replace ",", "")
}

function Parse-CompletedBuilds {
    param([string[]]$Lines)
    $raw = Get-RegexValue -Lines $Lines -Pattern "Completed builds:\s+([0-9,]+)"
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return 0
    }
    return [int]($raw -replace ",", "")
}

function Output-ContainsText {
    param(
        [string[]]$Lines,
        [string]$Needle
    )
    foreach ($line in $Lines) {
        if ($line -like "*$Needle*") {
            return $true
        }
    }
    return $false
}

$logRoot = Resolve-RepoPath -RawPath $LogDir
$statePath = Resolve-RepoPath -RawPath $StateFile
if (-not (Test-Path -LiteralPath $logRoot)) {
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
}
$stateDir = Split-Path -Parent $statePath
if (-not (Test-Path -LiteralPath $stateDir)) {
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
}

$endDt = if ([string]::IsNullOrWhiteSpace($EndDate)) { Get-Date } else { [datetime]::ParseExact($EndDate, "yyyy-MM-dd", $null) }
$endDt = Get-PreviousOrSameBusinessDate -DateValue $endDt
$startDt = [datetime]::ParseExact($StartDate, "yyyy-MM-dd", $null).Date
$endIso = $endDt.ToString("yyyy-MM-dd")
$startIso = $startDt.ToString("yyyy-MM-dd")

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$masterLog = Join-Path $logRoot ("sec_history_run_{0}.log" -f $timestamp)
$state = Load-State -PathValue $statePath
$state["completed_utc"] = $null
Save-State -State $state -PathValue $statePath

Write-MasterLog -PathValue $masterLog -Message ("Starting SEC historical rebuild. start={0} end={1} snapshot_workers={2}" -f $startIso, $endIso, $SecSnapshotMaxWorkers)

$periodStartDt = if ([string]::IsNullOrWhiteSpace([string]$state["sec_period_last_asof"])) {
    $startDt
} else {
    Get-NextBusinessDate -DateValue ([datetime]::ParseExact([string]$state["sec_period_last_asof"], "yyyy-MM-dd", $null))
}

if ($periodStartDt -le $endDt) {
    $periodDates = Get-BusinessDates -StartValue $periodStartDt -EndValue $endDt
    $periodTotal = $periodDates.Count
    $periodIndex = 0
    foreach ($dt in $periodDates) {
        $periodIndex += 1
        $asOfIso = $dt.ToString("yyyy-MM-dd")
        $periodLog = Join-Path $logRoot ("sec_period_{0}.log" -f $asOfIso.Replace("-", ""))
        Write-MasterLog -PathValue $masterLog -Message ("SEC period rebuild [{0}/{1}] as_of_date={2}" -f $periodIndex, $periodTotal, $asOfIso)
        $periodCmd = @(
            $PythonExe,
            "fundamental_data/build_sec_fundamental_features_tier1.py",
            "--config", $SecConfig,
            "--as-of-date", $asOfIso
        )
        Invoke-LoggedCommand -Name ("sec_period_{0}" -f $asOfIso) -CommandParts $periodCmd -MasterLog $masterLog -CommandLog $periodLog | Out-Null
        $state["sec_period_last_asof"] = $asOfIso
        Save-State -State $state -PathValue $statePath
    }
} else {
    Write-MasterLog -PathValue $masterLog -Message "SEC period rebuild already completed for the requested window."
}

$snapshotStartDt = if ([string]::IsNullOrWhiteSpace([string]$state["sec_snapshot_last_asof"])) {
    $startDt
} else {
    Get-NextBusinessDate -DateValue ([datetime]::ParseExact([string]$state["sec_snapshot_last_asof"], "yyyy-MM-dd", $null))
}

while ($snapshotStartDt -le $endDt) {
    $snapshotStartIso = $snapshotStartDt.ToString("yyyy-MM-dd")
    $snapshotLog = Join-Path $logRoot ("sec_snapshot_batch_{0}.log" -f $snapshotStartIso.Replace("-", ""))
    $snapshotCmd = @(
        $PythonExe,
        "fundamental_data/run_sec_fundamental_snapshot_history.py",
        "--config", $SecConfig,
        "--use-period-asof-dates",
        "--period-start-date", $snapshotStartIso,
        "--end-date", $endIso,
        "--max-dates", $SecSnapshotBatchSize.ToString(),
        "--date-order", "oldest",
        "--max-workers", $SecSnapshotMaxWorkers.ToString()
    )
    $snapshotOutput = Invoke-LoggedCommand -Name ("sec_snapshot_batch_{0}" -f $snapshotStartIso) -CommandParts $snapshotCmd -MasterLog $masterLog -CommandLog $snapshotLog
    if (
        (Output-ContainsText -Lines $snapshotOutput -Needle "No as_of dates selected. Nothing to run.") -or
        (Output-ContainsText -Lines $snapshotOutput -Needle "All selected as_of dates already exist. Nothing to run.")
    ) {
        Write-MasterLog -PathValue $masterLog -Message "SEC snapshot history is fully rebuilt for the requested window."
        break
    }
    $failedBuilds = Parse-FailedBuilds -Lines $snapshotOutput
    if ($failedBuilds -gt 0) {
        throw ("SEC snapshot history batch failed. See {0}" -f $snapshotLog)
    }
    $completedBuilds = Parse-CompletedBuilds -Lines $snapshotOutput
    if ($completedBuilds -le 0) {
        throw ("SEC snapshot history batch reported no completed builds but still had queued work. See {0}" -f $snapshotLog)
    }
    $rangeEnd = Parse-RangeEnd -Lines $snapshotOutput
    if ([string]::IsNullOrWhiteSpace($rangeEnd)) {
        throw ("Could not parse SEC snapshot batch range end. See {0}" -f $snapshotLog)
    }
    $state["sec_snapshot_last_asof"] = $rangeEnd
    Save-State -State $state -PathValue $statePath
    $snapshotStartDt = Get-NextBusinessDate -DateValue ([datetime]::ParseExact($rangeEnd, "yyyy-MM-dd", $null))
}

$state["completed_utc"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Save-State -State $state -PathValue $statePath
Write-MasterLog -PathValue $masterLog -Message ("SEC historical rebuild completed. state={0}" -f $statePath)
