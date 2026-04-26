param(
    [string]$PythonExe = "C:\Users\josel\Miniconda3\python.exe",
    [string]$Form4Config = "config_sec_form4.yaml",
    [string]$StartDate = "2019-03-15",
    [string]$EndDate = "",
    [int]$BatchSize = 75,
    [string]$LogDir = "fundamental_data/logs",
    [string]$StateFile = "fundamental_data/logs/form4_history_state.json"
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
            form4_update_done = $false
            form4_history_last_range_end = ""
            completed_utc = ""
        }
    }
    $raw = Get-Content -LiteralPath $PathValue -Raw | ConvertFrom-Json
    return [ordered]@{
        form4_update_done = [bool]$raw.form4_update_done
        form4_history_last_range_end = [string]$raw.form4_history_last_range_end
        completed_utc = [string]$raw.completed_utc
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
$endIso = $endDt.ToString("yyyy-MM-dd")
$startIso = ([datetime]::ParseExact($StartDate, "yyyy-MM-dd", $null)).ToString("yyyy-MM-dd")

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$masterLog = Join-Path $logRoot ("form4_history_run_{0}.log" -f $timestamp)
$state = Load-State -PathValue $statePath

Write-MasterLog -PathValue $masterLog -Message ("Starting Form4 historical rebuild. start={0} end={1}" -f $startIso, $endIso)

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

$batchNumber = 0
while ($true) {
    $batchNumber += 1
    $commandLog = Join-Path $logRoot ("form4_history_batch_{0:000}.log" -f $batchNumber)
    $cmd = @(
        $PythonExe,
        "helper_scripts/run_sec_form4_snapshot_history.py",
        "--config", $Form4Config,
        "--cadence", "daily",
        "--daily-start-date", $startIso,
        "--end-date", $endIso,
        "--skip-existing",
        "--max-dates", $BatchSize.ToString(),
        "--date-order", "oldest",
        "--no-refresh-legacy-buy-table"
    )
    $output = Invoke-LoggedCommand -Name ("form4_history_batch_{0:000}" -f $batchNumber) -CommandParts $cmd -MasterLog $masterLog -CommandLog $commandLog
    if (Output-ContainsText -Lines $output -Needle "All selected as_of dates already exist. Nothing to run.") {
        Write-MasterLog -PathValue $masterLog -Message "Form4 history is fully caught up for the requested window."
        break
    }
    if (Output-ContainsText -Lines $output -Needle "No as_of dates selected. Nothing to run.") {
        Write-MasterLog -PathValue $masterLog -Message "Form4 history had no eligible dates to build."
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
    $state["form4_history_last_range_end"] = $rangeEnd
    Save-State -State $state -PathValue $statePath
}

$state["completed_utc"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Save-State -State $state -PathValue $statePath
Write-MasterLog -PathValue $masterLog -Message ("Form4 historical rebuild completed. state={0}" -f $statePath)
