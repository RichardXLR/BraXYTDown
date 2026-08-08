[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$InstallerUrl,
    [ValidateSet('stable', 'experimental')]
    [string]$Channel = 'stable',
    [ValidateRange(1, 100)]
    [int]$Rollout = 100,
    [string]$InstallerPath = "",
    [string]$OutputPath = "",
    [string]$Notes = "",
    [switch]$Mandatory
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python da venv não encontrado: $python"
}
$version = (& $python -c "from baixatube import __version__; print(__version__)" | Select-Object -Last 1).Trim()
if (-not $InstallerPath) {
    $InstallerPath = Join-Path $projectRoot "dist\installer\BraXYTDow-Setup-$version-x64.exe"
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $projectRoot "dist\latest.json"
}
$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Instalador não encontrado: $InstallerPath"
}

$signature = Get-AuthenticodeSignature -LiteralPath $installer
if ([string]$signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
    throw "O instalador precisa de uma assinatura Authenticode válida antes da publicação. Status: $($signature.Status)"
}
$algorithm = [System.Security.Cryptography.SHA256]::Create()
try {
    $signerSha256 = ([System.BitConverter]::ToString($algorithm.ComputeHash($signature.SignerCertificate.RawData))).Replace('-', '').ToLowerInvariant()
}
finally { $algorithm.Dispose() }

$existing = $null
if (Test-Path -LiteralPath $OutputPath -PathType Leaf) {
    try { $existing = Get-Content -LiteralPath $OutputPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { $existing = $null }
}
$channels = [ordered]@{}
if ($null -ne $existing -and $null -ne $existing.channels) {
    foreach ($property in $existing.channels.PSObject.Properties) {
        $channels[$property.Name] = $property.Value
    }
}
$item = Get-Item -LiteralPath $installer
$previous = $null
if ($channels.Contains($Channel) -and [string]$channels[$Channel].version -ne $version) {
    $old = $channels[$Channel]
    $previous = [ordered]@{
        version = [string]$old.version
        installer_url = [string]$old.installer_url
        sha256 = [string]$old.sha256
        size = [long]$old.size
        publisher = [string]$old.publisher
        signer_sha256 = [string]$old.signer_sha256
    }
}
$releaseItem = [ordered]@{
    version = $version
    installer_url = $InstallerUrl
    sha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    size = $item.Length
    publisher = [string]$signature.SignerCertificate.Subject
    signer_sha256 = $signerSha256
    rollout = $Rollout
    mandatory = [bool]$Mandatory
    notes = $Notes
    published_at = [DateTime]::UtcNow.ToString("o")
}
if ($null -ne $previous) { $releaseItem.previous = $previous }
$channels[$Channel] = $releaseItem
$manifest = [ordered]@{
    schema = 2
    app = "BraXYTDow"
    channels = $channels
}
$parent = Split-Path -Parent $OutputPath
if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
[System.IO.File]::WriteAllText(
    $OutputPath,
    ($manifest | ConvertTo-Json -Depth 6),
    [System.Text.UTF8Encoding]::new($false)
)
Write-Host "Manifesto $Channel criado: $OutputPath"
Write-Host "Versão: $version | rollout: $Rollout% | certificado SHA-256: $signerSha256"
