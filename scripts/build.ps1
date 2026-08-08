[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$Offline,
    [switch]$RefreshTools,
    [switch]$SyncDependencies,
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$RequireInstaller,
    [switch]$RequireSigning,
    [string]$CertificateThumbprint = "",
    [ValidateSet("CurrentUser", "LocalMachine")]
    [string]$CertificateStore = "CurrentUser",
    [string]$TimestampUrl = "https://timestamp.digicert.com",
    [switch]$SkipOnedir,
    [switch]$SkipPortableZip
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location -LiteralPath $projectRoot

function Resolve-Executable {
    param([Parameter(Mandatory)][string]$Value)
    if (Test-Path -LiteralPath $Value -PathType Leaf) {
        return (Get-Item -LiteralPath $Value).FullName
    }
    $command = Get-Command $Value -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Invoke-PythonCommand {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & $script:PythonExecutable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
    }
}

function Remove-GeneratedDirectory {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateSet("build", "dist")][string]$ExpectedName
    )
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $parent = [System.IO.Path]::GetFullPath((Split-Path -Parent $resolved))
    if ($parent -ne $projectRoot -or [System.IO.Path]::GetFileName($resolved) -ne $ExpectedName) {
        throw "Refusing to remove unexpected build path: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

# Prefer an explicitly requested interpreter, then the project's reproducible
# virtual environment, then an already activated virtual environment.
$script:PythonExecutable = $null
if ($Python) { $script:PythonExecutable = Resolve-Executable -Value $Python }
if (-not $script:PythonExecutable) {
    $localPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $localPython -PathType Leaf) { $script:PythonExecutable = $localPython }
}
if (-not $script:PythonExecutable -and $env:VIRTUAL_ENV) {
    $activePython = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (Test-Path -LiteralPath $activePython -PathType Leaf) { $script:PythonExecutable = $activePython }
}

if (-not $script:PythonExecutable) {
    if ($Offline) { throw "No local Python environment was found and offline mode cannot create one." }
    $venvPath = Join-Path $projectRoot ".venv"
    $basePython = Resolve-Executable -Value "python.exe"
    if ($basePython) {
        & $basePython -m venv $venvPath
    }
    else {
        $launcher = Resolve-Executable -Value "py.exe"
        if (-not $launcher) { throw "Python 3.11+ was not found. Install Python or pass -Python <path>." }
        & $launcher -3 -m venv $venvPath
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv (exit code $LASTEXITCODE)." }
    $script:PythonExecutable = Join-Path $venvPath "Scripts\python.exe"
}

Write-Host "Python: $script:PythonExecutable"
& $script:PythonExecutable -c "import importlib.metadata as m, sys; expected={'PySide6':'6.11.1','pyinstaller':'6.21.0','pyinstaller-hooks-contrib':'2026.6','pytest':'8.4.2'}; sys.exit(0 if all(m.version(k)==v for k,v in expected.items()) else 1)" 2>$null
$dependenciesReady = $LASTEXITCODE -eq 0
if ($SyncDependencies -or -not $dependenciesReady) {
    if ($Offline) { throw "Build dependencies are incomplete in offline mode." }
    Invoke-PythonCommand -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements-build-lock.txt")
}

$appVersion = (& $script:PythonExecutable -c "from baixatube import __version__; print(__version__)" | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or -not $appVersion) { throw "Could not determine the BraXYTDow version." }
if ($appVersion -ne "3.2.0") { throw "Version mismatch: runtime is $appVersion, packaging expects 3.2.0." }
Write-Host "Building BraXYTDow $appVersion"

$signingEnabled = [bool]$CertificateThumbprint
if ($RequireSigning -and -not $signingEnabled) {
    throw "A release exige assinatura, mas nenhum CertificateThumbprint foi informado."
}
if ($signingEnabled) {
    $normalizedThumbprint = $CertificateThumbprint.Replace(' ', '').ToUpperInvariant()
    $certificatePath = "Cert:\$CertificateStore\My\$normalizedThumbprint"
    $certificate = Get-Item -LiteralPath $certificatePath -ErrorAction SilentlyContinue
    if (-not $certificate -or -not $certificate.HasPrivateKey) {
        throw "O certificado de assinatura com chave privada nao foi encontrado em $certificatePath."
    }
    $signToolCandidate = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
        Select-Object -First 1
    if (-not $signToolCandidate) {
        throw "SignTool x64 nao foi encontrado. Instale o Windows SDK antes do build assinado."
    }
}

$toolParameters = @{
    Force = [bool]$RefreshTools
    Offline = [bool]$Offline
}
& (Join-Path $PSScriptRoot "prepare_binaries.ps1") @toolParameters

if (-not $SkipTests) {
    Invoke-PythonCommand -Arguments @("-m", "pytest")
}

Remove-GeneratedDirectory -Path (Join-Path $projectRoot "build") -ExpectedName "build"
Remove-GeneratedDirectory -Path (Join-Path $projectRoot "dist") -ExpectedName "dist"

Invoke-PythonCommand -Arguments @("-m", "PyInstaller", "--noconfirm", "--clean", "BaixaTube.spec")
if (-not $SkipOnedir) {
    # The project-level build directory was already removed above. Running a
    # second --clean can race with Windows antivirus/indexing and delete the
    # freshly recreated onedir work directory while PyInstaller writes its
    # base library archive.
    Invoke-PythonCommand -Arguments @("-m", "PyInstaller", "--noconfirm", "BaixaTube-onedir.spec")
}

$distDir = Join-Path $projectRoot "dist"
$portableExe = Join-Path $distDir "BraXYTDow.exe"
if (-not (Test-Path -LiteralPath $portableExe -PathType Leaf)) { throw "Portable EXE was not generated: $portableExe" }

if ($signingEnabled) {
    $signTargets = @($portableExe)
    if (-not $SkipOnedir) { $signTargets += (Join-Path $distDir "BraXYTDow\BraXYTDow.exe") }
    $signParameters = @{
        Path = $signTargets
        CertificateThumbprint = $CertificateThumbprint
        CertificateStore = $CertificateStore
        TimestampUrl = $TimestampUrl
    }
    & (Join-Path $PSScriptRoot "sign_release.ps1") @signParameters
    if ($LASTEXITCODE -ne 0) { throw "Falha ao assinar os executáveis da aplicação." }
}

$portableZip = $null
if (-not $SkipOnedir) {
    $onedir = Join-Path $distDir "BraXYTDow"
    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $onedir
    Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY_NOTICES.md") -Destination $onedir
    if (-not $SkipPortableZip) {
        $portableZip = Join-Path $distDir "BraXYTDow-$appVersion-windows-x64.zip"
        Compress-Archive -LiteralPath $onedir -DestinationPath $portableZip -CompressionLevel Optimal
    }
}

$installerPath = $null
if (-not $SkipInstaller -and -not $SkipOnedir) {
    $iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if (-not $iscc) {
        $candidates = @(@(
            $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }),
            $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe" }),
            $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe" }),
            $(if ($env:ChocolateyInstall) { Join-Path $env:ChocolateyInstall "bin\ISCC.exe" })
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })
        if ($candidates) { $iscc = Get-Item -LiteralPath $candidates[0] }
    }
    if ($iscc) {
        $isccPath = if ($iscc.PSObject.Properties.Name -contains "Source") { $iscc.Source } else { $iscc.FullName }
        & $isccPath (Join-Path $projectRoot "installer\BaixaTube.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }
        $installerPath = Join-Path $distDir "installer\BraXYTDow-Setup-$appVersion-x64.exe"
        if ($signingEnabled) {
            $installerSignParameters = @{
                Path = @($installerPath)
                CertificateThumbprint = $CertificateThumbprint
                CertificateStore = $CertificateStore
                TimestampUrl = $TimestampUrl
            }
            & (Join-Path $PSScriptRoot "sign_release.ps1") @installerSignParameters
            if ($LASTEXITCODE -ne 0) { throw "Falha ao assinar o instalador." }
        }
    }
    elseif ($RequireInstaller) {
        throw "Inno Setup 6 was not found. Install it or omit -RequireInstaller."
    }
    else {
        Write-Warning "Inno Setup 6 was not found; EXE and portable ZIP were generated without an installer."
    }
}

Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY_NOTICES.md") -Destination $distDir
$distributionArguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $PSScriptRoot "prepare_distribution_bundle.ps1"),
    "-Python", $script:PythonExecutable,
    "-Version", $appVersion
)
if ($SkipOnedir) { $distributionArguments += "-SkipOnedir" }
if ($RequireInstaller) { $distributionArguments += "-RequireInstaller" }
if ($signingEnabled) { $distributionArguments += "-RequireSigning" }
& powershell.exe @distributionArguments
if ($LASTEXITCODE -ne 0) { throw "Distribution verification failed with exit code $LASTEXITCODE." }

Write-Host ""
Write-Host "Release complete:"
Write-Host "  Single-file EXE: $portableExe"
if ($portableZip) { Write-Host "  Portable ZIP:    $portableZip" }
if ($installerPath -and (Test-Path -LiteralPath $installerPath)) { Write-Host "  Installer:       $installerPath" }
Write-Host "  Checksums:       $(Join-Path $distDir 'SHA256SUMS.txt')"
