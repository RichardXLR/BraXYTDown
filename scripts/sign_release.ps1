[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Fa-f]{40}$')]
    [string]$CertificateThumbprint,
    [ValidateSet('CurrentUser', 'LocalMachine')]
    [string]$CertificateStore = 'CurrentUser',
    [ValidatePattern('^https://')]
    [string]$TimestampUrl = 'https://timestamp.digicert.com',
    [string]$Description = 'BraXYTDow',
    [ValidatePattern('^https://')]
    [string]$DescriptionUrl = 'https://www.instagram.com/richard.ittou?igsh=c210bHhzdzJwcWg1'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$thumbprint = $CertificateThumbprint.Replace(' ', '').ToUpperInvariant()
$certificatePath = "Cert:\$CertificateStore\My\$thumbprint"
$certificate = Get-Item -LiteralPath $certificatePath -ErrorAction SilentlyContinue
if (-not $certificate) { throw "Certificado nao encontrado em $certificatePath." }
if (-not $certificate.HasPrivateKey) { throw 'O certificado selecionado nao possui a chave privada necessaria para assinar.' }
$now = Get-Date
if ($certificate.NotBefore -gt $now -or $certificate.NotAfter -le $now) {
    throw "O certificado nao esta dentro do periodo de validade: $($certificate.NotBefore) ate $($certificate.NotAfter)."
}
$codeSigningOid = '1.3.6.1.5.5.7.3.3'
$ekuOids = @($certificate.Extensions |
    Where-Object { $_ -is [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension] } |
    ForEach-Object { $_.EnhancedKeyUsages } |
    ForEach-Object { $_.Value })
if ($ekuOids.Count -gt 0 -and $ekuOids -notcontains $codeSigningOid) {
    throw 'O certificado selecionado nao permite assinatura de codigo (EKU Code Signing ausente).'
}

$signtool = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $signtool) { throw 'SignTool x64 nao foi encontrado. Instale o Windows SDK.' }

foreach ($candidate in $Path) {
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    $signArguments = @(
        'sign', '/v', '/sha1', $thumbprint, '/s', 'My',
        '/fd', 'SHA256', '/tr', $TimestampUrl, '/td', 'SHA256',
        '/d', $Description, '/du', $DescriptionUrl
    )
    if ($CertificateStore -eq 'LocalMachine') { $signArguments += '/sm' }
    $signArguments += $resolved
    & $signtool.FullName @signArguments
    if ($LASTEXITCODE -ne 0) { throw "Falha ao assinar $resolved" }

    & $signtool.FullName verify /pa /all /tw $resolved
    if ($LASTEXITCODE -ne 0) { throw "Falha ao verificar a assinatura de $resolved" }
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    if ([string]$signature.Status -ne 'Valid' -or $null -eq $signature.TimeStamperCertificate) {
        throw "A assinatura de '$resolved' nao ficou valida e carimbada. Status: $($signature.Status)."
    }
    Write-Host "Assinatura Authenticode e timestamp verificados: $resolved"
}
