[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://[a-z0-9-]+\.codesigning\.azure\.net/?$')]
    [string]$Endpoint,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?!one)(?!.*--)[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]$')]
    [string]$CodeSigningAccountName,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?!.*--)[A-Za-z][A-Za-z0-9-]{3,98}[A-Za-z0-9]$')]
    [string]$CertificateProfileName,
    [string]$CorrelationId = '',
    [string]$ClientToolsPath = '',
    [string]$SignToolPath = '',
    [string]$Description = 'BraXYTDow',
    [ValidatePattern('^https://')]
    [string]$DescriptionUrl = 'https://www.instagram.com/richard.ittou?igsh=c210bHhzdzJwcWg1'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-SignTool {
    if ($SignToolPath) {
        return (Resolve-Path -LiteralPath $SignToolPath).Path
    }
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidate = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($candidate) { return $candidate.FullName }
    throw 'SignTool x64 nao foi encontrado. Instale o Artifact Signing Client Tools ou o Windows SDK.'
}

function Resolve-ClientTools {
    if ($ClientToolsPath) {
        return (Resolve-Path -LiteralPath $ClientToolsPath).Path
    }
    $candidates = @(
        $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Microsoft\MicrosoftArtifactSigningClientTools' }),
        $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Microsoft\ArtifactSigningTools' })
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'Azure.CodeSigning.Dlib.dll') -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Azure.CodeSigning.Dlib.dll nao foi encontrado. Reinstale o Artifact Signing Client Tools.'
}

$signTool = Resolve-SignTool
$clientTools = Resolve-ClientTools
$dlib = Join-Path $clientTools 'Azure.CodeSigning.Dlib.dll'

$dotnet = Get-Command dotnet.exe -ErrorAction SilentlyContinue
if (-not $dotnet) { throw 'O runtime .NET 8 ou superior nao foi encontrado.' }
$runtimeMajors = @(& $dotnet.Source --list-runtimes 2>$null |
    Where-Object { $_ -match '^Microsoft\.NETCore\.App ([0-9]+)\.' } |
    ForEach-Object { [int]$Matches[1] })
if (-not ($runtimeMajors | Where-Object { $_ -ge 8 })) {
    throw 'O Artifact Signing Client Tools exige Microsoft.NETCore.App 8 ou superior.'
}

$metadata = [ordered]@{
    Endpoint = $Endpoint.TrimEnd('/')
    CodeSigningAccountName = $CodeSigningAccountName
    CertificateProfileName = $CertificateProfileName
}
if ($CorrelationId) { $metadata.CorrelationId = $CorrelationId }

$metadataPath = [System.IO.Path]::Combine(
    [System.IO.Path]::GetTempPath(),
    "braxy-artifact-signing-$([Guid]::NewGuid().ToString('N')).json"
)
try {
    [System.IO.File]::WriteAllText(
        $metadataPath,
        ($metadata | ConvertTo-Json -Depth 4),
        [System.Text.UTF8Encoding]::new($false)
    )

    foreach ($candidate in $Path) {
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        & $signTool sign /v /debug /fd SHA256 `
            /tr 'http://timestamp.acs.microsoft.com' /td SHA256 `
            /dlib $dlib /dmdf $metadataPath `
            /d $Description /du $DescriptionUrl $resolved
        if ($LASTEXITCODE -ne 0) {
            throw "Microsoft Artifact Signing falhou para '$resolved' (codigo $LASTEXITCODE)."
        }

        & $signTool verify /pa /all /tw $resolved
        if ($LASTEXITCODE -ne 0) { throw "SignTool rejeitou a assinatura de '$resolved'." }
        $signature = Get-AuthenticodeSignature -LiteralPath $resolved
        if ([string]$signature.Status -ne 'Valid' -or $null -eq $signature.TimeStamperCertificate) {
            throw "A assinatura de '$resolved' nao ficou valida e carimbada. Status: $($signature.Status)."
        }
        Write-Host "Artifact Signing verificado: $resolved"
        Write-Host "  Publicador: $($signature.SignerCertificate.Subject)"
    }
}
finally {
    if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
        Remove-Item -LiteralPath $metadataPath -Force
    }
}
