[CmdletBinding()]
param(
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$Version = "3.2.0",
    [switch]$SkipOnedir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$distDir = Join-Path $projectRoot "dist"

function Assert-PeFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [long]$MinimumBytes = 256KB
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing PE file: $Path" }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -lt $MinimumBytes) { throw "PE file is unexpectedly small: $Path ($($item.Length) bytes)" }
    $stream = [System.IO.File]::OpenRead($item.FullName)
    try {
        if ($stream.ReadByte() -ne 0x4D -or $stream.ReadByte() -ne 0x5A) { throw "Invalid PE header: $Path" }
    }
    finally { $stream.Dispose() }
}

$portable = Join-Path $distDir "BraXYTDow.exe"
Assert-PeFile -Path $portable -MinimumBytes 50MB
$portableVersion = (Get-Item -LiteralPath $portable).VersionInfo.ProductVersion
if (-not $portableVersion.StartsWith($Version)) { throw "Portable EXE version mismatch: '$portableVersion' (expected $Version)." }

$archiveListing = & $Python -m PyInstaller.utils.cliutils.archive_viewer -l -b $portable 2>&1
if ($LASTEXITCODE -ne 0) { throw "Could not inspect the portable PyInstaller archive." }
$normalizedListing = ($archiveListing -join "`n").Replace("\", "/")
foreach ($entry in @("bin/yt-dlp.exe", "bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/deno.exe")) {
    if (-not $normalizedListing.Contains($entry)) { throw "Portable EXE does not contain required tool: $entry" }
}

$smokeProcess = Start-Process -FilePath $portable -ArgumentList "--smoke-test" -PassThru -WindowStyle Hidden
$deadline = [DateTime]::UtcNow.AddSeconds(60)
do {
    $smokeDescendants = @(
        Get-CimInstance Win32_Process -Filter "Name='BraXYTDow.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and [System.IO.Path]::GetFullPath($_.ExecutablePath) -eq $portable }
    )
    if ($smokeDescendants.Count -eq 0) { break }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)
if ($smokeDescendants.Count -gt 0) {
    Get-Process -Id $smokeDescendants.ProcessId -ErrorAction SilentlyContinue | Stop-Process -Force
    throw "Portable EXE smoke test timed out after 60 seconds."
}
$smokeProcess.Refresh()
if ($smokeProcess.HasExited -and $smokeProcess.ExitCode -ne 0) {
    throw "Portable EXE smoke test failed with exit code $($smokeProcess.ExitCode)."
}

if (-not $SkipOnedir) {
    $onedir = Join-Path $distDir "BraXYTDow"
    $onedirExe = Join-Path $onedir "BraXYTDow.exe"
    Assert-PeFile -Path $onedirExe
    $onedirVersion = (Get-Item -LiteralPath $onedirExe).VersionInfo.ProductVersion
    if (-not $onedirVersion.StartsWith($Version)) { throw "Onedir EXE version mismatch: '$onedirVersion' (expected $Version)." }

    foreach ($tool in @("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "deno.exe")) {
        $source = Join-Path $projectRoot "bin\$tool"
        $packagedCandidates = @(
            (Join-Path $onedir "_internal\bin\$tool"),
            (Join-Path $onedir "bin\$tool")
        )
        $packaged = $packagedCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $packaged) { throw "Onedir package does not contain required tool: $tool" }
        $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
        $packagedHash = (Get-FileHash -LiteralPath $packaged -Algorithm SHA256).Hash
        if ($sourceHash -ne $packagedHash) { throw "Packaged tool hash mismatch: $tool" }
    }
}

Write-Host "Release verification and smoke test passed. Portable version: $portableVersion"
