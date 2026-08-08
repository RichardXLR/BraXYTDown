from __future__ import annotations

import hashlib
import json
from pathlib import Path

from baixatube import app_updates
from baixatube.app_updates import AppRelease, AppUpdater
from baixatube.release_integrity import AuthenticodeResult


def release_payload(url: str = "https://updates.example/BraXYTDow-Setup.exe", rollout: int = 100) -> dict:
    return {
        "schema": 2,
        "app": "BraXYTDow",
        "channels": {
            "stable": {
                "version": "99.0.0",
                "installer_url": url,
                "sha256": "a" * 64,
                "size": 1024 * 1024 + 2,
                "publisher": "CN=Richard Ittou",
                "signer_sha256": "b" * 64,
                "rollout": rollout,
                "notes": "AutoCura aprimorado",
            }
        },
    }


def signed_release(payload: bytes, digest: str | None = None) -> AppRelease:
    return AppRelease(
        "99.0.0",
        "https://updates.example/setup.exe",
        digest or hashlib.sha256(payload).hexdigest(),
        len(payload),
        "CN=Richard Ittou",
        "b" * 64,
    )


def valid_signature(_path, _publisher, _fingerprint):
    return AuthenticodeResult(True, "Valid", "CN=Richard Ittou", "b" * 64)


def test_empty_channel_is_explicitly_not_configured():
    result = AppUpdater("").check()

    assert result.available is False
    assert result.error == ""
    assert "não publicado" in result.message


def test_https_manifest_exposes_new_release(monkeypatch):
    updater = AppUpdater("https://updates.example/latest.json", device_id="fixture")
    monkeypatch.setattr(updater, "_read", lambda _url, _limit: json.dumps(release_payload()).encode())

    result = updater.check()

    assert result.available is True
    assert result.release is not None
    assert result.release.version == "99.0.0"
    assert result.release.rollout == 100


def test_non_https_installer_is_rejected(monkeypatch):
    updater = AppUpdater("https://updates.example/latest.json")
    monkeypatch.setattr(updater, "_read", lambda _url, _limit: json.dumps(release_payload("http://updates.example/setup.exe")).encode())

    result = updater.check()

    assert result.available is False
    assert "HTTPS" in result.error


def test_download_verifies_hash_size_and_signature_before_publishing(monkeypatch, tmp_path):
    payload = b"MZ" + b"x" * (1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    release = signed_release(payload, digest)
    updater = AppUpdater("https://updates.example/latest.json", signature_verifier=valid_signature)
    monkeypatch.setattr(app_updates, "data_dir", lambda: tmp_path)

    def fake_download(_url, destination, progress):
        destination.write_bytes(payload)
        if progress:
            progress(100)
        return digest, len(payload)

    monkeypatch.setattr(updater, "_download", fake_download)

    result = updater.download(release)

    assert result.error == ""
    assert result.path is not None
    assert open(result.path, "rb").read() == payload
    assert "assinatura" in result.message


def test_download_rejects_digest_mismatch(monkeypatch, tmp_path):
    payload = b"MZ" + b"x" * (1024 * 1024)
    release = signed_release(payload, "0" * 64)
    updater = AppUpdater("https://updates.example/latest.json", signature_verifier=valid_signature)
    monkeypatch.setattr(app_updates, "data_dir", lambda: tmp_path)

    def fake_download(_url, destination, _progress):
        destination.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest(), len(payload)

    monkeypatch.setattr(updater, "_download", fake_download)

    result = updater.download(release)

    assert result.path is None
    assert "SHA-256" in result.error


def test_download_rejects_untrusted_authenticode_signature(monkeypatch, tmp_path):
    payload = b"MZ" + b"x" * (1024 * 1024)
    updater = AppUpdater(
        "https://updates.example/latest.json",
        signature_verifier=lambda *_args: AuthenticodeResult(False, "HashMismatch", detail="certificado diferente"),
    )
    monkeypatch.setattr(app_updates, "data_dir", lambda: tmp_path)
    release = signed_release(payload)
    monkeypatch.setattr(updater, "_download", lambda _url, destination, _progress: (destination.write_bytes(payload) and hashlib.sha256(payload).hexdigest(), len(payload)))

    result = updater.download(release)

    assert result.path is None
    assert "Authenticode" in result.error


def test_three_failed_startups_request_verified_rollback(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(app_updates, "data_dir", lambda: tmp_path)
    updates = tmp_path / "app-updates"
    updates.mkdir()
    rollback = updates / "BraXYTDow-Setup-2.3.0-x64.exe"
    rollback.write_bytes(b"installer")
    (updates / "pending-update.json").write_text(
        json.dumps(
            {
                "from_version": "2.3.0",
                "to_version": app_updates.__version__,
                "rollback_installer": str(rollback),
                "attempts": 2,
            }
        ),
        encoding="utf-8",
    )

    recovery = app_updates.evaluate_startup_recovery()

    assert recovery.action == "rollback"
    assert recovery.installer_path == str(rollback)
