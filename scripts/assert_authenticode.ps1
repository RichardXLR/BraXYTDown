[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,
    [string]$ExpectedPublisher = "",
    [ValidatePattern('^$|^[0-9A-Fa-f]{64}$')]
    [string]$ExpectedCertificateSha256 = "",
    [string]$OutputPath = "",
    [switch]$RequireTimestamp,
    [switch]$RequireSignTool
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-CertificateSha256 {
    param([Parameter(Mandatory)][System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($Certificate.RawData))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Resolve-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $roots = @(
        $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin' }),
        $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles 'Windows Kits\10\bin' })
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
    foreach ($root in $roots) {
        $candidate = Get-ChildItem -LiteralPath $root -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) { return $candidate.FullName }
    }
    return $null
}

$signTool = Resolve-SignTool
if ($RequireSignTool -and -not $signTool) {
    throw 'SignTool x64 nao foi encontrado. Instale o Windows SDK antes de validar uma distribuicao oficial.'
}

$expectedCertificate = $ExpectedCertificateSha256.ToLowerInvariant()
$results = @()
foreach ($candidate in $Path) {
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    $item = Get-Item -LiteralPath $resolved
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    if ([string]$signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
        throw "Assinatura Authenticode invalida em '$resolved'. Status: $($signature.Status)."
    }

    $publisher = [string]$signature.SignerCertificate.Subject
    if ($ExpectedPublisher -and $publisher.IndexOf($ExpectedPublisher, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "Publicador inesperado em '$resolved'. Recebido: '$publisher'; esperado conter: '$ExpectedPublisher'."
    }

    $certificateSha256 = Get-CertificateSha256 -Certificate $signature.SignerCertificate
    if ($expectedCertificate -and $certificateSha256 -ne $expectedCertificate) {
        throw "Certificado inesperado em '$resolved'. SHA-256 recebido: $certificateSha256."
    }

    $timestampPresent = $null -ne $signature.TimeStamperCertificate
    if ($RequireTimestamp -and -not $timestampPresent) {
        throw "A assinatura de '$resolved' nao possui carimbo de tempo verificavel."
    }

    if ($signTool) {
        & $signTool verify /pa /all /tw /q $resolved
        if ($LASTEXITCODE -ne 0) {
            throw "SignTool rejeitou a assinatura de '$resolved' (codigo $LASTEXITCODE)."
        }
    }

    $results += [ordered]@{
        path = $resolved
        name = $item.Name
        size = [long]$item.Length
        file_sha256 = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
        status = [string]$signature.Status
        publisher = $publisher
        certificate_sha256 = $certificateSha256
        certificate_thumbprint_sha1 = ([string]$signature.SignerCertificate.Thumbprint).ToLowerInvariant()
        certificate_not_before = $signature.SignerCertificate.NotBefore.ToUniversalTime().ToString('o')
        certificate_not_after = $signature.SignerCertificate.NotAfter.ToUniversalTime().ToString('o')
        timestamp_present = $timestampPresent
        timestamp_authority = $(if ($timestampPresent) { [string]$signature.TimeStamperCertificate.Subject } else { $null })
    }
}

$report = [ordered]@{
    schema = 1
    generated_at = [DateTime]::UtcNow.ToString('o')
    authenticode = 'valid'
    timestamp_required = [bool]$RequireTimestamp
    signtool_verified = [bool]$signTool
    files = @($results)
}

if ($OutputPath) {
    $fullOutput = [System.IO.Path]::GetFullPath($OutputPath)
    $parent = Split-Path -Parent $fullOutput
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    [System.IO.File]::WriteAllText(
        $fullOutput,
        ($report | ConvertTo-Json -Depth 6),
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "Relatorio de assinatura: $fullOutput"
}

Write-Host "Authenticode valido em $($results.Count) arquivo(s), com identidade e integridade verificadas."

