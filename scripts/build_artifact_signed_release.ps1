[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://[a-z0-9-]+\.codesigning\.azure\.net/?$')]
    [string]$Endpoint,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?!one)(?!.*--)[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]$')]
    [string]$CodeSigningAccountName,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?!.*--)[A-Za-z][A-Za-z0-9-]{3,98}[A-Za-z0-9]$')]
    [string]$CertificateProfileName,
    [string]$ExpectedPublisher = '',
    [string]$Python = '',
    [switch]$RefreshTools,
    [switch]$SyncDependencies
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location -LiteralPath $projectRoot

$blockedNames = @('BraXYTDow.exe', 'BaixaTube.exe', 'yt-dlp.exe', 'ffmpeg.exe', 'deno.exe')
$active = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $blockedNames -contains $_.Name })
if ($active.Count -gt 0) {
    $details = ($active | ForEach-Object { "$($_.Name) (PID $($_.ProcessId))" }) -join ', '
    throw "Feche o aplicativo e aguarde downloads ativos antes da release assinada: $details"
}

$buildParameters = @{
    SkipInstaller = $true
    SkipPortableZip = $true
}
if ($Python) { $buildParameters.Python = $Python }
if ($RefreshTools) { $buildParameters.RefreshTools = $true }
if ($SyncDependencies) { $buildParameters.SyncDependencies = $true }
& (Join-Path $PSScriptRoot 'build.ps1') @buildParameters
if ($LASTEXITCODE -ne 0) { throw "O build sem assinatura falhou com codigo $LASTEXITCODE." }

$pythonPath = if ($Python) { $Python } else { Join-Path $projectRoot '.venv\Scripts\python.exe' }
$version = (& $pythonPath -c 'from baixatube import __version__; print(__version__)' | Select-Object -Last 1).Trim()
if (-not $version) { throw 'Nao foi possivel determinar a versao do BraXYTDow.' }

$artifactSigningParameters = @{
    Endpoint = $Endpoint
    CodeSigningAccountName = $CodeSigningAccountName
    CertificateProfileName = $CertificateProfileName
}

$applicationTargets = @(
    (Join-Path $projectRoot 'dist\BraXYTDow.exe'),
    (Join-Path $projectRoot 'dist\BraXYTDow\BraXYTDow.exe')
)
& (Join-Path $PSScriptRoot 'sign_release_artifact.ps1') -Path $applicationTargets @artifactSigningParameters
if ($LASTEXITCODE -ne 0) { throw 'A assinatura dos executaveis do aplicativo falhou.' }

$packageParameters = @{ Version = $version }
if ($ExpectedPublisher) { $packageParameters.ExpectedPublisher = $ExpectedPublisher }
& (Join-Path $PSScriptRoot 'package_external_release.ps1') @packageParameters
if ($LASTEXITCODE -ne 0) { throw 'A criacao dos pacotes assinados falhou.' }

$installerPath = Join-Path $projectRoot "dist\installer\BraXYTDow-Setup-$version-x64.exe"
& (Join-Path $PSScriptRoot 'sign_release_artifact.ps1') -Path @($installerPath) @artifactSigningParameters
if ($LASTEXITCODE -ne 0) { throw 'A assinatura do instalador falhou.' }

$distributionParameters = @{
    Python = $pythonPath
    Version = $version
    RequireInstaller = $true
    RequireSigning = $true
}
if ($ExpectedPublisher) { $distributionParameters.ExpectedPublisher = $ExpectedPublisher }
& (Join-Path $PSScriptRoot 'prepare_distribution_bundle.ps1') @distributionParameters
if ($LASTEXITCODE -ne 0) { throw 'A verificacao final da distribuicao assinada falhou.' }

Write-Host ''
Write-Host "Release oficial assinada pronta: BraXYTDow $version"
Write-Host "  Instalador: $installerPath"
Write-Host "  Relatorio:  $(Join-Path $projectRoot 'dist\signing-report.json')"
