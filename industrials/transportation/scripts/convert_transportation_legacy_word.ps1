param(
    [Parameter(Mandatory = $true)]
    [string]$InputCsv,
    [Parameter(Mandatory = $true)]
    [string]$OutputCsv
)

$ErrorActionPreference = "Stop"
$converter = "C:\Program Files\Microsoft Office\root\Office16\Wordconv.exe"
if (-not [System.IO.File]::Exists($converter)) {
    throw "Office Wordconv.exe is not installed: $converter"
}
$requests = @(Import-Csv -LiteralPath $InputCsv)
if ($requests.Count -eq 0) {
    throw "Legacy Word conversion request CSV is empty"
}

$results = [System.Collections.Generic.List[object]]::new()
foreach ($request in $requests) {
    $source = [System.IO.Path]::GetFullPath($request.local_path)
    $target = [System.IO.Path]::GetFullPath($request.converted_path)
    $status = ""
    $errorText = ""
    if (-not [System.IO.File]::Exists($source)) {
        $status = "FAILED_SOURCE_MISSING"
        $errorText = "Source file does not exist"
    }
    elseif (
        [System.IO.File]::Exists($target) -and
        (Get-Item -LiteralPath $target).Length -gt 0
    ) {
        $status = "CACHE_HIT_VALID"
    }
    else {
        $targetDirectory = [System.IO.Path]::GetDirectoryName($target)
        [System.IO.Directory]::CreateDirectory($targetDirectory) | Out-Null
        $temporary = Join-Path $targetDirectory (
            "." + [System.IO.Path]::GetFileName($target) + "." +
            [System.Diagnostics.Process]::GetCurrentProcess().Id + ".tmp.docx"
        )
        try {
            $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
            $startInfo.FileName = $converter
            $startInfo.Arguments = (
                '-oice -nme "{0}" "{1}"' -f $source, $temporary
            )
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $process = [System.Diagnostics.Process]::Start($startInfo)
            if (-not $process.WaitForExit(60000)) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                throw "Wordconv exceeded the 60-second document timeout"
            }
            if (
                -not [System.IO.File]::Exists($temporary) -or
                (Get-Item -LiteralPath $temporary).Length -le 0
            ) {
                throw "Wordconv did not produce a nonempty DOCX"
            }
            Move-Item -LiteralPath $temporary -Destination $target -Force
            $status = "CONVERTED"
        }
        catch {
            $status = "FAILED_CONVERSION"
            $errorText = $_.Exception.Message
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
    $results.Add([pscustomobject]@{
        content_sha256 = $request.content_sha256
        local_path = $source
        converted_path = $target
        status = $status
        error = $errorText
    })
}

$outputDirectory = [System.IO.Path]::GetDirectoryName(
    [System.IO.Path]::GetFullPath($OutputCsv)
)
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
$results | Export-Csv -LiteralPath $OutputCsv -NoTypeInformation -Encoding UTF8
if (@($results | Where-Object { $_.status -like "FAILED_*" }).Count -gt 0) {
    exit 2
}
exit 0
