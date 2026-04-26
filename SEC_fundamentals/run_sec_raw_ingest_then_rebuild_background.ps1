param(
    [string]$PythonExe = "C:\Users\josel\Miniconda3\python.exe",
    [string]$SecConfig = "fundamental_data/config_sec_fundamentals.yaml",
    [string]$StartDate = "2019-03-15",
    [string]$EndDate = "",
    [string]$LogDir = "fundamental_data/logs",
    [string]$StateFile = "fundamental_data/logs/sec_history_state.json",
    [int]$SecSnapshotBatchSize = 60,
    [int]$SecSnapshotMaxWorkers = 2
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

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

$endIso = if ([string]::IsNullOrWhiteSpace($EndDate)) {
    (Get-Date).ToString("yyyy-MM-dd")
} else {
    [datetime]::ParseExact($EndDate, "yyyy-MM-dd", $null).ToString("yyyy-MM-dd")
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$masterLog = Join-Path $logRoot ("sec_raw_then_rebuild_{0}.log" -f $timestamp)
$rawLog = Join-Path $logRoot ("sec_raw_backfill_{0}.log" -f $timestamp)
$rebuildLog = Join-Path $logRoot ("sec_rebuild_{0}.log" -f $timestamp)

Write-MasterLog -PathValue $masterLog -Message ("Starting SEC raw backfill + rebuild. start={0} end={1} snapshot_workers={2}" -f $StartDate, $endIso, $SecSnapshotMaxWorkers)

$rawCmd = @(
    $PythonExe,
    "fundamental_data/ingest_sec_fundamentals_tier1.py",
    "--config", $SecConfig,
    "--mode", "backfill",
    "--start-date", $StartDate,
    "--end-date", $endIso
)
Invoke-LoggedCommand -Name "sec_raw_backfill" -CommandParts $rawCmd -MasterLog $masterLog -CommandLog $rawLog

$freshState = [ordered]@{
    sec_period_last_asof = ""
    sec_snapshot_last_asof = ""
    completed_utc = $null
}
$freshState | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding utf8
Write-MasterLog -PathValue $masterLog -Message ("Reset SEC derived-history state: {0}" -f $statePath)

$rebuildCmd = @(
    "powershell.exe",
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "fundamental_data/run_sec_history_background.ps1",
    "-PythonExe", $PythonExe,
    "-SecConfig", $SecConfig,
    "-StartDate", $StartDate,
    "-EndDate", $endIso,
    "-SecSnapshotBatchSize", $SecSnapshotBatchSize.ToString(),
    "-SecSnapshotMaxWorkers", $SecSnapshotMaxWorkers.ToString(),
    "-LogDir", $LogDir,
    "-StateFile", $StateFile
)
Invoke-LoggedCommand -Name "sec_history_rebuild" -CommandParts $rebuildCmd -MasterLog $masterLog -CommandLog $rebuildLog

Write-MasterLog -PathValue $masterLog -Message "SEC raw backfill + derived rebuild wrapper completed."
