[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Offline,
    [switch]$SkipDeno,
    [ValidateSet("stable", "nightly")]
    [string]$YtDlpChannel = "nightly",
    [string]$YtDlpUrl = "",
    [string]$YtDlpChecksumUrl = "",
    [string]$FfmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    [string]$FfmpegChecksumUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256",
    [string]$DenoUrl = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
    [string]$DenoChecksumUrl = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip.sha256sum",
    [string]$YtDlpSha256 = "",
    [string]$FfmpegArchiveSha256 = "",
    [string]$DenoArchiveSha256 = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$binDir = Join-Path $projectRoot "bin"
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
$tempDir = [System.IO.Path]::GetFullPath((Join-Path $tempRoot ("BraXYTDow-tools-" + [guid]::NewGuid().ToString("N"))))
New-Item -ItemType Directory -Force -Path $binDir, $tempDir | Out-Null

if (-not $YtDlpUrl) {
    $releaseRepository = if ($YtDlpChannel -eq "nightly") { "yt-dlp/yt-dlp-nightly-builds" } else { "yt-dlp/yt-dlp" }
    $YtDlpUrl = "https://github.com/$releaseRepository/releases/latest/download/yt-dlp.exe"
    if (-not $YtDlpChecksumUrl) {
        $YtDlpChecksumUrl = "https://github.com/$releaseRepository/releases/latest/download/SHA2-256SUMS"
    }
}
elseif (-not $YtDlpChecksumUrl -and -not $YtDlpSha256) {
    throw "A custom -YtDlpUrl requires -YtDlpChecksumUrl or -YtDlpSha256."
}

function Test-PeBinary {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][long]$MinimumBytes
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -lt $MinimumBytes) { return $false }
    $stream = [System.IO.File]::OpenRead($item.FullName)
    try {
        return $stream.ReadByte() -eq 0x4D -and $stream.ReadByte() -eq 0x5A
    }
    finally { $stream.Dispose() }
}

function Get-PublishedHash {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$AssetName
    )
    $checksumFile = Join-Path $tempDir ("checksum-" + [guid]::NewGuid().ToString("N") + ".txt")
    Invoke-WebRequest -UseBasicParsing -Headers @{ "User-Agent" = "BraXYTDow-Build/2.3" } -Uri $Uri -OutFile $checksumFile
    $content = [System.IO.File]::ReadAllText($checksumFile)
    $escapedName = [regex]::Escape($AssetName)
    $named = [regex]::Match($content, "(?im)^\s*([0-9a-f]{64})\s+\*?$escapedName\s*$")
    if ($named.Success) { return $named.Groups[1].Value.ToUpperInvariant() }
    $single = [regex]::Match($content, "(?i)\b([0-9a-f]{64})\b")
    if ($single.Success) { return $single.Groups[1].Value.ToUpperInvariant() }
    throw "Could not parse a SHA-256 checksum for '$AssetName' from '$Uri'."
}

function Assert-ExpectedHash {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Expected
    )
    if ($Expected -notmatch "^[0-9a-fA-F]{64}$") { throw "Invalid expected SHA-256 value for '$Path'." }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ne $Expected.Trim().ToUpperInvariant()) {
        throw "SHA-256 mismatch for $([System.IO.Path]::GetFileName($Path)). Expected: $Expected; actual: $actual"
    }
}

function Install-StagedBinary {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )
    $staged = Join-Path $binDir ("." + [System.IO.Path]::GetFileName($Destination) + "." + [guid]::NewGuid().ToString("N") + ".new")
    try {
        Copy-Item -LiteralPath $Source -Destination $staged
        Move-Item -LiteralPath $staged -Destination $Destination -Force
    }
    finally {
        if (Test-Path -LiteralPath $staged) { Remove-Item -LiteralPath $staged -Force }
    }
}

function Get-ToolVersion {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    try {
        $lines = @(& $Path @Arguments 2>$null)
        $exitCode = $LASTEXITCODE
        $output = $lines | Select-Object -First 1
        if ($exitCode -eq 0 -and $output) { return "$output".Trim() }
    }
    catch { }
    return "unknown"
}

function Extract-SingleZipEntry {
    param(
        [Parameter(Mandatory)][string]$Archive,
        [Parameter(Mandatory)][string]$EntryName,
        [Parameter(Mandatory)][string]$Destination
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $matches = @($zip.Entries | Where-Object {
            $_.FullName.Replace("\", "/").TrimStart("/") -eq $EntryName
        })
        if ($matches.Count -ne 1) { throw "Expected exactly one '$EntryName' entry in the Deno archive." }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($matches[0], $Destination, $true)
    }
    finally { $zip.Dispose() }
}

try {
    $ytDlp = Join-Path $binDir "yt-dlp.exe"
    $ffmpeg = Join-Path $binDir "ffmpeg.exe"
    $ffprobe = Join-Path $binDir "ffprobe.exe"
    $deno = Join-Path $binDir "deno.exe"
    $needYtDlp = $Force -or -not (Test-PeBinary -Path $ytDlp -MinimumBytes 5MB)
    $needFfmpeg = $Force -or -not (Test-PeBinary -Path $ffmpeg -MinimumBytes 10MB) -or -not (Test-PeBinary -Path $ffprobe -MinimumBytes 10MB)
    $needDeno = -not $SkipDeno -and ($Force -or -not (Test-PeBinary -Path $deno -MinimumBytes 10MB))
    $didDownloadYtDlp = $needYtDlp
    $didDownloadFfmpeg = $needFfmpeg
    $didDownloadDeno = $needDeno

    if ($Offline -and ($needYtDlp -or $needFfmpeg -or $needDeno)) {
        throw "Offline mode: one or more required binaries are missing or invalid in '$binDir'. Use -SkipDeno if Deno is intentionally omitted."
    }

    if ($needYtDlp) {
        Write-Host "Downloading yt-dlp ($YtDlpChannel)..."
        $download = Join-Path $tempDir "yt-dlp.exe"
        Invoke-WebRequest -UseBasicParsing -Headers @{ "User-Agent" = "BraXYTDow-Build/2.3" } -Uri $YtDlpUrl -OutFile $download
        $expected = if ($YtDlpSha256) { $YtDlpSha256 } else { Get-PublishedHash -Uri $YtDlpChecksumUrl -AssetName "yt-dlp.exe" }
        Assert-ExpectedHash -Path $download -Expected $expected
        if (-not (Test-PeBinary -Path $download -MinimumBytes 5MB)) { throw "The yt-dlp download is not a valid PE executable." }
        Install-StagedBinary -Source $download -Destination $ytDlp
    }
    else { Write-Host "Reusing validated yt-dlp. Use -Force to refresh it." }

    if ($needFfmpeg) {
        Write-Host "Downloading FFmpeg GPL package (Gyan Essentials)..."
        $archive = Join-Path $tempDir "ffmpeg-release-essentials.zip"
        $expanded = Join-Path $tempDir "ffmpeg-expanded"
        Invoke-WebRequest -UseBasicParsing -Headers @{ "User-Agent" = "BraXYTDow-Build/2.3" } -Uri $FfmpegUrl -OutFile $archive
        $expected = if ($FfmpegArchiveSha256) { $FfmpegArchiveSha256 } else { Get-PublishedHash -Uri $FfmpegChecksumUrl -AssetName "ffmpeg-release-essentials.zip" }
        Assert-ExpectedHash -Path $archive -Expected $expected
        Expand-Archive -LiteralPath $archive -DestinationPath $expanded
        $downloadedFfmpeg = Get-ChildItem -LiteralPath $expanded -Recurse -File -Filter "ffmpeg.exe" | Select-Object -First 1
        $downloadedFfprobe = Get-ChildItem -LiteralPath $expanded -Recurse -File -Filter "ffprobe.exe" | Select-Object -First 1
        if (-not $downloadedFfmpeg -or -not $downloadedFfprobe) { throw "FFmpeg or FFprobe was not found in the verified package." }
        if (-not (Test-PeBinary -Path $downloadedFfmpeg.FullName -MinimumBytes 10MB) -or -not (Test-PeBinary -Path $downloadedFfprobe.FullName -MinimumBytes 10MB)) {
            throw "The FFmpeg package contains invalid or incomplete executables."
        }
        Install-StagedBinary -Source $downloadedFfmpeg.FullName -Destination $ffmpeg
        Install-StagedBinary -Source $downloadedFfprobe.FullName -Destination $ffprobe
    }
    else { Write-Host "Reusing validated FFmpeg/FFprobe. Use -Force to refresh them." }

    if ($needDeno) {
        Write-Host "Downloading Deno JavaScript runtime..."
        $archive = Join-Path $tempDir "deno-x86_64-pc-windows-msvc.zip"
        $downloadedDeno = Join-Path $tempDir "deno.exe"
        Invoke-WebRequest -UseBasicParsing -Headers @{ "User-Agent" = "BraXYTDow-Build/2.3" } -Uri $DenoUrl -OutFile $archive
        $expected = if ($DenoArchiveSha256) { $DenoArchiveSha256 } else { Get-PublishedHash -Uri $DenoChecksumUrl -AssetName "deno-x86_64-pc-windows-msvc.zip" }
        Assert-ExpectedHash -Path $archive -Expected $expected
        Extract-SingleZipEntry -Archive $archive -EntryName "deno.exe" -Destination $downloadedDeno
        if (-not (Test-PeBinary -Path $downloadedDeno -MinimumBytes 10MB)) { throw "The Deno archive contains an invalid executable." }
        Install-StagedBinary -Source $downloadedDeno -Destination $deno
    }
    elseif (-not $SkipDeno) { Write-Host "Reusing validated Deno. Use -Force to refresh it." }

    foreach ($required in @(
        @{ Path = $ytDlp; Minimum = 5MB },
        @{ Path = $ffmpeg; Minimum = 10MB },
        @{ Path = $ffprobe; Minimum = 10MB }
    )) {
        if (-not (Test-PeBinary -Path $required.Path -MinimumBytes $required.Minimum)) {
            throw "Required binary is missing or invalid: $($required.Path)"
        }
    }

    $tools = [ordered]@{
        "yt-dlp" = [ordered]@{
            version = Get-ToolVersion -Path $ytDlp -Arguments @("--version")
            channel = if ($didDownloadYtDlp) { $YtDlpChannel } else { "unknown (preexisting build input)" }
            source = if ($didDownloadYtDlp) { $YtDlpUrl } else { "preexisting-local" }
            official_download = $YtDlpUrl
            license = "GPL-3.0-or-later (standalone executable; see upstream notices)"
            sha256 = (Get-FileHash -LiteralPath $ytDlp -Algorithm SHA256).Hash.ToLowerInvariant()
            size = (Get-Item -LiteralPath $ytDlp).Length
        }
        ffmpeg = [ordered]@{
            version = Get-ToolVersion -Path $ffmpeg -Arguments @("-version")
            source = if ($didDownloadFfmpeg) { $FfmpegUrl } else { "preexisting-local" }
            official_download = $FfmpegUrl
            license = "GPL-3.0"
            sha256 = (Get-FileHash -LiteralPath $ffmpeg -Algorithm SHA256).Hash.ToLowerInvariant()
            size = (Get-Item -LiteralPath $ffmpeg).Length
        }
        ffprobe = [ordered]@{
            version = Get-ToolVersion -Path $ffprobe -Arguments @("-version")
            source = if ($didDownloadFfmpeg) { $FfmpegUrl } else { "preexisting-local" }
            official_download = $FfmpegUrl
            license = "GPL-3.0"
            sha256 = (Get-FileHash -LiteralPath $ffprobe -Algorithm SHA256).Hash.ToLowerInvariant()
            size = (Get-Item -LiteralPath $ffprobe).Length
        }
    }
    if (-not $SkipDeno -and (Test-PeBinary -Path $deno -MinimumBytes 10MB)) {
        $tools["deno"] = [ordered]@{
            version = Get-ToolVersion -Path $deno -Arguments @("--version")
            source = if ($didDownloadDeno) { $DenoUrl } else { "preexisting-local" }
            official_download = $DenoUrl
            license = "MIT"
            sha256 = (Get-FileHash -LiteralPath $deno -Algorithm SHA256).Hash.ToLowerInvariant()
            size = (Get-Item -LiteralPath $deno).Length
        }
    }
    $manifest = [ordered]@{
        schema = 1
        generated_at = [DateTime]::UtcNow.ToString("o")
        tools = $tools
    }
    $manifestPath = Join-Path $binDir "tools-manifest.json"
    [System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 6), [System.Text.UTF8Encoding]::new($false))
    Write-Host "Tools ready. Manifest: $manifestPath"
    Get-ChildItem -LiteralPath $binDir -Filter "*.exe" | ForEach-Object {
        $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName
        Write-Host ("{0}  {1} ({2:N0} bytes)" -f $hash.Hash, $_.Name, $_.Length)
    }
}
finally {
    if (Test-Path -LiteralPath $tempDir) {
        $resolved = [System.IO.Path]::GetFullPath($tempDir)
        $safePrefix = $tempRoot + [System.IO.Path]::DirectorySeparatorChar
        if (-not $resolved.StartsWith($safePrefix, [StringComparison]::OrdinalIgnoreCase) -or -not ([System.IO.Path]::GetFileName($resolved)).StartsWith("BraXYTDow-tools-")) {
            throw "Refusing to clean unexpected temporary directory: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
