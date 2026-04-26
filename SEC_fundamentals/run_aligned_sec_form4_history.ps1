param(
    [string]$PythonExe = "C:\Users\josel\Miniconda3\python.exe",
    [string]$SecConfig = "fundamental_data/config_sec_fundamentals.yaml",
    [string]$Form4Config = "config_sec_form4.yaml",
    [string]$StartDate = "2019-03-15",
    [string]$EndDate = "",
    [int]$Form4BatchSize = 75,
    [int]$SecSnapshotBatchSize = 60,
    [string]$LogDir = "fundamental_data/logs",
    [string]$StateFile = "fundamental_data/logs/aligned_history_state.json"
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
    $candidate = $RawPath
    if ([System.IO.Path]::IsPathRooted($candidate)) {
        return [System.IO.Path]::GetFullPath($candidate)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $candidate))
}

function Load-State {
    param([string]$PathValue)
    if (-not (Test-Path -LiteralPath $PathValue)) {
        return [ordered]@{
            form4_update_done = $false
            form4_history_last_range_end = ""
            sec_period_last_asof = ""
            sec_snapshot_last_asof = ""
            completed_utc = $null
        }
    }
    $raw = Get-Content -LiteralPath $PathValue -Raw | ConvertFrom-Json
    return [ordered]@{
        form4_update_done = [bool]$raw.form4_update_done
        form4_history_last_range_end = [string]$raw.form4_history_last_range_end
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
    $output = & $CommandParts[0] @argList 2>&1
    $exitCode = $LASTEXITCODE
    $rendered = @($output | ForEach-Object { $_.ToString() })
    if ($rendered.Count -gt 0) {
        Add-Content -LiteralPath $CommandLog -Value $rendered
    }
    Write-MasterLog -PathValue $MasterLog -Message ("END {0}: exit_code={1} log={2}" -f $Name, $exitCode, $CommandLog)
    if ($exitCode -ne 0) {
        throw ("Command '{0}' failed with exit code {1}. See {2}" -f $Name, $exitCode, $CommandLog)
    }
    return $rendered
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

function Run-Form4-HistoryLoop {
    param(
        [string]$PythonExeValue,
        [string]$Form4ConfigValue,
        [string]$StartDateIso,
        [string]$EndDateIso,
        [int]$BatchSize,
        [string]$MasterLog,
        [string]$LogRoot,
        [hashtable]$State,
        [string]$StatePathValue
    )
    $batchNumber = 0
    while ($true) {
        $batchNumber += 1
        $commandLog = Join-Path $LogRoot ("form4_history_batch_{0:000}.log" -f $batchNumber)
        $cmd = @(
            $PythonExeValue,
            "helper_scripts/run_sec_form4_snapshot_history.py",
            "--config", $Form4ConfigValue,
            "--cadence", "daily",
            "--daily-start-date", $StartDateIso,
            "--end-date", $EndDateIso,
            "--skip-existing",
            "--max-dates", $BatchSize.ToString(),
            "--date-order", "oldest",
            "--no-refresh-legacy-buy-table"
        )
        $output = Invoke-LoggedCommand -Name ("form4_history_batch_{0:000}" -f $batchNumber) -CommandParts $cmd -MasterLog $MasterLog -CommandLog $commandLog
        if (Output-ContainsText -Lines $output -Needle "All selected as_of dates already exist. Nothing to run.") {
            Write-MasterLog -PathValue $MasterLog -Message "Form4 history is fully caught up for the requested window."
            break
        }
        if (Output-ContainsText -Lines $output -Needle "No as_of dates selected. Nothing to run.") {
            Write-MasterLog -PathValue $MasterLog -Message "Form4 history had no eligible dates to build."
            break
        }
        $failedBuilds = Parse-FailedBuilds -Lines $output
        if ($failedBuilds -gt 0) {
            throw ("Form4 history batch failed. See {0}" -f $commandLog)
        }
        $completedBuilds = Parse-CompletedBuilds -Lines $output
        if ($completedBuilds -le 0) {
            throw ("Form4 history batch reported no completed builds but still had queued work. See {0}" -f $commandLog)
        }
        $rangeEnd = Parse-RangeEnd -Lines $output
        if ([string]::IsNullOrWhiteSpace($rangeEnd)) {
            throw ("Could not parse Form4 history batch range end. See {0}" -f $commandLog)
        }
        $State["form4_history_last_range_end"] = $rangeEnd
        Save-State -State $State -PathValue $StatePathValue
    }
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
$startDt = [datetime]::ParseExact($StartDate, "yyyy-MM-dd", $null)
$startDt = $startDt.Date
$endIso = $endDt.ToString("yyyy-MM-dd")
$startIso = $startDt.ToString("yyyy-MM-dd")

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$masterLog = Join-Path $logRoot ("aligned_history_{0}.log" -f $timestamp)
$state = Load-State -PathValue $statePath
$state["completed_utc"] = $null
Save-State -State $state -PathValue $statePath

Write-MasterLog -PathValue $masterLog -Message ("Starting aligned SEC/Form4 historical rebuild. start={0} end={1}" -f $startIso, $endIso)

if (-not [bool]$state["form4_update_done"]) {
    $form4UpdateLog = Join-Path $logRoot ("form4_update_{0}.log" -f $timestamp)
    $form4UpdateCmd = @(
        $PythonExe,
        "helper_scripts/update_sec_form4_daily.py",
        "--config", $Form4Config,
        "--mode", "daily",
        "--end-date", $endIso
    )
    Invoke-LoggedCommand -Name "form4_update" -CommandParts $form4UpdateCmd -MasterLog $masterLog -CommandLog $form4UpdateLog | Out-Null
    $state["form4_update_done"] = $true
    Save-State -State $state -PathValue $statePath
}

Run-Form4-HistoryLoop `
    -PythonExeValue $PythonExe `
    -Form4ConfigValue $Form4Config `
    -StartDateIso $startIso `
    -EndDateIso $endIso `
    -BatchSize $Form4BatchSize `
    -MasterLog $masterLog `
    -LogRoot $logRoot `
    -State $state `
    -StatePathValue $statePath

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
        "--max-workers", "1"
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

Run-Form4-HistoryLoop `
    -PythonExeValue $PythonExe `
    -Form4ConfigValue $Form4Config `
    -StartDateIso $startIso `
    -EndDateIso $endIso `
    -BatchSize $Form4BatchSize `
    -MasterLog $masterLog `
    -LogRoot $logRoot `
    -State $state `
    -StatePathValue $statePath

$alignmentLog = Join-Path $logRoot ("alignment_{0}.log" -f $timestamp)
$alignmentCmd = @(
    $PythonExe,
    "-c",
    @"
import sqlite3
from datetime import date, timedelta

sec_db = r"C:\Users\josel\Documents\PROD\DB\sec_fundamentals.sqlite"
form4_db = r"C:\Users\josel\Documents\PROD\DB\sec_insider.sqlite"
start = date.fromisoformat("$startIso")
end = date.fromisoformat("$endIso")

expected = []
cur = start
while cur <= end:
    if cur.weekday() < 5:
        expected.append(cur.isoformat())
    cur += timedelta(days=1)
expected_set = set(expected)

sec_conn = sqlite3.connect(sec_db)
form4_conn = sqlite3.connect(form4_db)
sec_rows = sec_conn.execute(
    "SELECT DISTINCT as_of_date FROM sec_fundamental_snapshot_filled_security_t1_resolved WHERE as_of_date >= ? AND as_of_date <= ?",
    (start.isoformat(), end.isoformat()),
).fetchall()
form4_rows = form4_conn.execute(
    "SELECT DISTINCT as_of_date FROM stock_signal_snapshot_tier1 WHERE as_of_date >= ? AND as_of_date <= ?",
    (start.isoformat(), end.isoformat()),
).fetchall()
sec_dates = {str(r[0]) for r in sec_rows if r and r[0]}
form4_dates = {str(r[0]) for r in form4_rows if r and r[0]}
missing_in_sec = sorted(form4_dates - sec_dates)
missing_in_form4 = sorted(sec_dates - form4_dates)
missing_business_sec = sorted(expected_set - sec_dates)
missing_business_form4 = sorted(expected_set - form4_dates)
print(f"sec_dates={len(sec_dates)}")
print(f"form4_dates={len(form4_dates)}")
print(f"missing_in_sec={len(missing_in_sec)}")
print(f"missing_in_form4={len(missing_in_form4)}")
print(f"missing_business_sec={len(missing_business_sec)}")
print(f"missing_business_form4={len(missing_business_form4)}")
print("missing_in_sec_sample=" + ",".join(missing_in_sec[:20]))
print("missing_in_form4_sample=" + ",".join(missing_in_form4[:20]))
print("missing_business_sec_sample=" + ",".join(missing_business_sec[:20]))
print("missing_business_form4_sample=" + ",".join(missing_business_form4[:20]))
sec_conn.close()
form4_conn.close()
"@
)
Invoke-LoggedCommand -Name "alignment_check" -CommandParts $alignmentCmd -MasterLog $masterLog -CommandLog $alignmentLog | Out-Null

$state["completed_utc"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Save-State -State $state -PathValue $statePath
Write-MasterLog -PathValue $masterLog -Message ("Aligned SEC/Form4 historical rebuild completed. state={0}" -f $statePath)
