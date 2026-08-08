[CmdletBinding()]
param(
    [string]$Python = '.venv\Scripts\python.exe',
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [switch]$SkipOnedir,
    [switch]$RequireInstaller,
    [switch]$RequireSigning,
    [string]$ExpectedPublisher = '',
    [ValidatePattern('^$|^[0-9A-Fa-f]{64}$')]
    [string]$ExpectedCertificateSha256 = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$distDir = Join-Path $projectRoot 'dist'
$pythonPath = if (Test-Path -LiteralPath $Python -PathType Leaf) {
    (Resolve-Path -LiteralPath $Python).Path
} else {
    $Python
}

$verifyArguments = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', (Join-Path $PSScriptRoot 'verify_release.ps1'),
    '-Python', $pythonPath,
    '-Version', $Version
)
if ($SkipOnedir) { $verifyArguments += '-SkipOnedir' }
& powershell.exe @verifyArguments
if ($LASTEXITCODE -ne 0) { throw "A verificacao funcional da release falhou com codigo $LASTEXITCODE." }

$portableExe = Join-Path $distDir 'BraXYTDow.exe'
$portableZip = Join-Path $distDir "BraXYTDow-$Version-windows-x64.zip"
$installerPath = Join-Path $distDir "installer\BraXYTDow-Setup-$Version-x64.exe"
$sbomPath = Join-Path $distDir 'BraXYTDow-sbom.cdx.json'
$signingReportPath = Join-Path $distDir 'signing-report.json'

if ($RequireInstaller -and -not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "O instalador obrigatorio nao foi encontrado: $installerPath"
}

if ($RequireSigning) {
    $signTargets = @($portableExe)
    if (-not $SkipOnedir) {
        $signTargets += (Join-Path $distDir 'BraXYTDow\BraXYTDow.exe')
    }
    if (Test-Path -LiteralPath $installerPath -PathType Leaf) {
        $signTargets += $installerPath
    }
    $assertParameters = @{
        Path = $signTargets
        OutputPath = $signingReportPath
        RequireTimestamp = $true
        RequireSignTool = $true
    }
    if ($ExpectedPublisher) { $assertParameters.ExpectedPublisher = $ExpectedPublisher }
    if ($ExpectedCertificateSha256) { $assertParameters.ExpectedCertificateSha256 = $ExpectedCertificateSha256 }
    & (Join-Path $PSScriptRoot 'assert_authenticode.ps1') @assertParameters
    if ($LASTEXITCODE -ne 0) { throw "A verificacao Authenticode da distribuicao falhou com codigo $LASTEXITCODE." }
}
elseif (Test-Path -LiteralPath $signingReportPath -PathType Leaf) {
    Remove-Item -LiteralPath $signingReportPath -Force
}

& $pythonPath (Join-Path $PSScriptRoot 'generate_sbom.py') $sbomPath
if ($LASTEXITCODE -ne 0) { throw "A geracao do SBOM falhou com codigo $LASTEXITCODE." }

$artifactPaths = @($portableExe)
if (Test-Path -LiteralPath $portableZip -PathType Leaf) { $artifactPaths += $portableZip }
if (Test-Path -LiteralPath $installerPath -PathType Leaf) { $artifactPaths += $installerPath }
$artifactPaths += $sbomPath
if (Test-Path -LiteralPath $signingReportPath -PathType Leaf) { $artifactPaths += $signingReportPath }

$artifacts = foreach ($artifact in $artifactPaths) {
    $item = Get-Item -LiteralPath $artifact
    $relativePath = $item.FullName.Substring($distDir.Length).TrimStart('\').Replace('\', '/')
    [ordered]@{
        name = $item.Name
        relative_path = $relativePath
        size = [long]$item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

$release = [ordered]@{
    schema = 2
    app_version = $Version
    architecture = 'x86_64'
    generated_at = [DateTime]::UtcNow.ToString('o')
    python = (& $pythonPath --version 2>&1 | Select-Object -First 1).ToString()
    pyinstaller = (& $pythonPath -c 'import PyInstaller; print(PyInstaller.__version__)' | Select-Object -First 1).ToString()
    distribution = [ordered]@{
        installer_required = [bool]$RequireInstaller
        authenticode_required = [bool]$RequireSigning
        authenticode_verified = [bool]($RequireSigning -and (Test-Path -LiteralPath $signingReportPath -PathType Leaf))
        signing_report = $(if (Test-Path -LiteralPath $signingReportPath -PathType Leaf) { 'signing-report.json' } else { $null })
    }
    artifacts = @($artifacts)
}

[System.IO.File]::WriteAllText(
    (Join-Path $distDir 'release-manifest.json'),
    ($release | ConvertTo-Json -Depth 7),
    [System.Text.UTF8Encoding]::new($false)
)
$checksumLines = $artifacts | ForEach-Object { "$($_.sha256)  $($_.relative_path)" }
[System.IO.File]::WriteAllLines(
    (Join-Path $distDir 'SHA256SUMS.txt'),
    $checksumLines,
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "Pacote de distribuicao finalizado: BraXYTDow $Version"
Write-Host "  Authenticode exigido: $([bool]$RequireSigning)"
Write-Host "  Manifesto: $(Join-Path $distDir 'release-manifest.json')"
Write-Host "  Checksums: $(Join-Path $distDir 'SHA256SUMS.txt')"
