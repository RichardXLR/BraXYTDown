"""Windows release-signature verification used by the application updater."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class AuthenticodeResult:
    valid: bool
    status: str
    subject: str = ""
    certificate_sha256: str = ""
    detail: str = ""


def verify_authenticode(
    path: Path,
    expected_publisher: str,
    expected_certificate_sha256: str,
) -> AuthenticodeResult:
    """Verify Authenticode status and pin the publisher certificate.

    BraXYTDow targets Windows 10/11, where Windows PowerShell and the
    Authenticode provider are present.  The installer is never started when
    this check is unavailable or inconclusive.
    """

    if os.name != "nt":
        return AuthenticodeResult(False, "Unsupported", detail="A verificação Authenticode exige Windows.")
    if not path.is_file():
        return AuthenticodeResult(False, "NotFound", detail="O instalador não foi encontrado.")
    script = r"""
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $args[0]
$certificate = $signature.SignerCertificate
$certificateSha256 = ''
if ($null -ne $certificate) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $certificateSha256 = ([System.BitConverter]::ToString($algorithm.ComputeHash($certificate.RawData))).Replace('-', '').ToLowerInvariant()
    }
    finally { $algorithm.Dispose() }
}
[ordered]@{
    status = [string]$signature.Status
    subject = if ($null -ne $certificate) { [string]$certificate.Subject } else { '' }
    certificate_sha256 = $certificateSha256
    detail = [string]$signature.StatusMessage
} | ConvertTo-Json -Compress
"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode:
            return AuthenticodeResult(False, "Error", detail=(completed.stderr or completed.stdout)[-600:])
        payload = json.loads(completed.stdout.strip())
        status = str(payload.get("status", ""))
        subject = str(payload.get("subject", ""))
        fingerprint = str(payload.get("certificate_sha256", "")).casefold()
        expected_fingerprint = expected_certificate_sha256.strip().casefold()
        publisher_ok = bool(expected_publisher.strip()) and expected_publisher.casefold() in subject.casefold()
        fingerprint_ok = len(expected_fingerprint) == 64 and fingerprint == expected_fingerprint
        return AuthenticodeResult(
            status == "Valid" and publisher_ok and fingerprint_ok,
            status,
            subject,
            fingerprint,
            str(payload.get("detail", "")),
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError) as exc:
        return AuthenticodeResult(False, "Error", detail=str(exc))


__all__ = ["AuthenticodeResult", "verify_authenticode"]
