[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$ExpectedPublisher = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$distDir = Join-Path $projectRoot 'dist'
$onedir = Join-Path $distDir 'BraXYTDow'
$portableExe = Join-Path $distDir 'BraXYTDow.exe'
$onedirExe = Join-Path $onedir 'BraXYTDow.exe'
$portableZip = Join-Path $distDir "BraXYTDow-$Version-windows-x64.zip"
$installerPath = Join-Path $distDir "installer\BraXYTDow-Setup-$Version-x64.exe"

$assertParameters = @{
    Path = @($portableExe, $onedirExe)
    RequireTimestamp = $true
    RequireSignTool = $true
}
if ($ExpectedPublisher) { $assertParameters.ExpectedPublisher = $ExpectedPublisher }
& (Join-Path $PSScriptRoot 'assert_authenticode.ps1') @assertParameters
if ($LASTEXITCODE -ne 0) { throw 'Os executaveis precisam estar assinados antes de criar o ZIP e o instalador.' }

Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $onedir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.md') -Destination $onedir -Force
if (Test-Path -LiteralPath $portableZip -PathType Leaf) {
    Remove-Item -LiteralPath $portableZip -Force
}
Compress-Archive -LiteralPath $onedir -DestinationPath $portableZip -CompressionLevel Optimal

$isccCandidates = @(
    (Join-Path $projectRoot '.tools\InnoSetup6\ISCC.exe'),
    $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }),
    $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe' }),
    $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe' })
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
$isccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
$iscc = if ($isccCommand) { $isccCommand.Source } elseif ($isccCandidates) { $isccCandidates[0] } else { $null }
if (-not $iscc) { throw 'Inno Setup 6 nao foi encontrado para criar o instalador.' }

if (Test-Path -LiteralPath $installerPath -PathType Leaf) {
    Remove-Item -LiteralPath $installerPath -Force
}
& $iscc (Join-Path $projectRoot 'installer\BaixaTube.iss')
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falhou com codigo $LASTEXITCODE." }
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "O instalador nao foi produzido: $installerPath"
}

Write-Host "Pacotes externos preparados para a assinatura final:"
Write-Host "  ZIP:       $portableZip"
Write-Host "  Instalador: $installerPath"
