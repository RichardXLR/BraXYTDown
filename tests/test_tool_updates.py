from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from baixatube.paths import active_binary, activate_tool, executable_name
from baixatube.tool_updates import (
    DENO_RELEASE_API,
    DENO_WINDOWS_ASSET,
    FFMPEG_RELEASE_ZIP,
    YT_DLP_NIGHTLY_RELEASE_API,
    IntegrityError,
    OfficialReleaseProvider,
    ToolRelease,
    ToolUpdateError,
    ToolUpdater,
    UpdateChannel,
    UpdateMode,
    UpdatePreferences,
)
from baixatube.compatibility import CompatibilityCheck, CompatibilityLevel


class FakeHttpClient:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def get_bytes(self, url: str, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        self.calls.append(url)
        value = self.payloads[url]
        assert len(value) <= max_bytes
        return value

    def download(self, url, destination, *, max_bytes, progress=None):
        self.calls.append(url)
        value = self.payloads[url]
        assert len(value) <= max_bytes
        destination.write_bytes(value)
        if progress:
            progress(len(value), len(value))
        return hashlib.sha256(value).hexdigest()


class FakeProvider:
    def __init__(self, releases: dict[str, ToolRelease]) -> None:
        self.releases = releases
        self.calls: list[str] = []

    def resolve(self, tool: str) -> ToolRelease:
        self.calls.append(tool)
        return self.releases[tool]


class FailingProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, tool: str) -> ToolRelease:
        self.calls.append(tool)
        raise ToolUpdateError("servidor temporariamente indisponível")


def _release(tool: str, payload: bytes, *, version: str = "2026.08.06", archive: str = "executable"):
    return ToolRelease(tool, version, f"https://updates.example/{tool}", hashlib.sha256(payload).hexdigest(), archive)


def _ffmpeg_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("ffmpeg-test/bin/ffmpeg.exe", b"MZ-ffmpeg")
        archive.writestr("ffmpeg-test/bin/ffprobe.exe", b"MZ-ffprobe")
        archive.writestr("ffmpeg-test/doc/readme.txt", b"ignored")
    return output.getvalue()


def _deno_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("deno.exe", b"MZ-deno")
    return output.getvalue()


def test_preferences_support_existing_flat_settings():
    preferences = UpdatePreferences.from_settings(
        {"auto_update_tools": False, "update_channel": "stable"}
    )

    assert preferences.mode == UpdateMode.MANUAL
    assert preferences.channel == UpdateChannel.STABLE
    assert preferences.to_settings()["update_channel"] == "stable"


def test_official_nightly_release_uses_asset_digest_without_extra_checksum_request():
    digest = "a" * 64
    release = {
        "tag_name": "2026.08.06.123456",
        "assets": [
            {
                "name": "yt-dlp.exe",
                "browser_download_url": "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/download/x/yt-dlp.exe",
                "digest": f"sha256:{digest}",
            }
        ],
    }
    client = FakeHttpClient({YT_DLP_NIGHTLY_RELEASE_API: json.dumps(release).encode()})

    resolved = OfficialReleaseProvider(client, UpdateChannel.NIGHTLY).resolve("yt-dlp")

    assert resolved.version == "2026.08.06.123456"
    assert resolved.sha256 == digest
    assert client.calls == [YT_DLP_NIGHTLY_RELEASE_API]


def test_official_ffmpeg_release_reads_ver_and_sha256():
    digest = "b" * 64
    client = FakeHttpClient(
        {
            f"{FFMPEG_RELEASE_ZIP}.ver": b"8.1.2\n",
            f"{FFMPEG_RELEASE_ZIP}.sha256": f"{digest} *ffmpeg-release-essentials.zip\n".encode(),
        }
    )

    resolved = OfficialReleaseProvider(client).resolve("ffmpeg")

    assert resolved.version == "8.1.2"
    assert resolved.sha256 == digest
    assert resolved.archive == "zip"


def test_official_deno_release_uses_separate_checksum_asset():
    digest = "c" * 64
    package_url = f"https://github.com/denoland/deno/releases/download/v2.4.0/{DENO_WINDOWS_ASSET}"
    checksum_url = f"{package_url}.sha256sum"
    metadata = {
        "tag_name": "v2.4.0",
        "assets": [
            {"name": DENO_WINDOWS_ASSET, "browser_download_url": package_url},
            {"name": f"{DENO_WINDOWS_ASSET}.sha256sum", "browser_download_url": checksum_url},
        ],
    }
    client = FakeHttpClient(
        {
            DENO_RELEASE_API: json.dumps(metadata).encode(),
            checksum_url: f"{digest}  {DENO_WINDOWS_ASSET}\n".encode(),
        }
    )

    resolved = OfficialReleaseProvider(client).resolve("deno")

    assert resolved.version == "2.4.0"
    assert resolved.sha256 == digest
    assert resolved.archive == "deno-zip"


def test_valid_yt_dlp_update_is_installed_and_activated_atomically(tmp_path):
    payload = b"MZ-fake-yt-dlp"
    release = _release("yt-dlp", payload)
    client = FakeHttpClient({release.download_url: payload})
    updater = ToolUpdater(
        client=client,
        provider=FakeProvider({"yt-dlp": release}),
        version_probe=lambda _tool, _path: release.version,
        root=tmp_path,
    )

    result = updater.update(("yt-dlp",))[0]

    assert result.changed is True
    assert result.installed_version == release.version
    assert Path(result.path).read_bytes() == payload
    assert active_binary("yt-dlp", tmp_path) == Path(result.path)


def test_bad_sha256_never_replaces_active_version(tmp_path):
    old_dir = tmp_path / "versions" / "yt-dlp" / "old"
    old_dir.mkdir(parents=True)
    old_binary = old_dir / executable_name("yt-dlp")
    old_binary.write_bytes(b"MZ-old")
    activate_tool("yt-dlp", "2025.01.01", old_dir, root=tmp_path, source="test")

    payload = b"MZ-tampered"
    release = ToolRelease("yt-dlp", "2026.08.06", "https://updates.example/yt-dlp", "0" * 64)
    updater = ToolUpdater(
        client=FakeHttpClient({release.download_url: payload}),
        provider=FakeProvider({"yt-dlp": release}),
        version_probe=lambda _tool, _path: "2025.01.01",
        root=tmp_path,
    )

    result = updater.update(("yt-dlp",))[0]

    assert result.changed is False
    assert "SHA256" in result.error
    assert active_binary("yt-dlp", tmp_path) == old_binary


def test_same_version_with_republished_digest_updates_only_downloaded_build(tmp_path):
    current_dir = tmp_path / "versions" / "yt-dlp" / "current"
    current_dir.mkdir(parents=True)
    (current_dir / executable_name("yt-dlp")).write_bytes(b"MZ-current")
    activate_tool(
        "yt-dlp",
        "2026.08.06",
        current_dir,
        root=tmp_path,
        source="downloaded",
        sha256="a" * 64,
    )
    release = ToolRelease(
        "yt-dlp",
        "2026.08.06",
        "https://updates.example/yt-dlp",
        "b" * 64,
    )
    updater = ToolUpdater(
        client=FakeHttpClient({}),
        provider=FakeProvider({"yt-dlp": release}),
        version_probe=lambda _tool, _path: "2026.08.06",
        root=tmp_path,
    )

    check = updater.check(("yt-dlp",))[0]

    assert check.available is True


def test_ffmpeg_zip_installs_only_required_binaries(tmp_path):
    payload = _ffmpeg_zip()
    release = _release("ffmpeg", payload, version="8.1.2", archive="zip")
    updater = ToolUpdater(
        client=FakeHttpClient({release.download_url: payload}),
        provider=FakeProvider({"ffmpeg": release}),
        version_probe=lambda _tool, _path: "8.1.2",
        root=tmp_path,
    )

    result = updater.update(("ffmpeg",))[0]
    install_dir = Path(result.path).parent

    assert result.changed is True
    assert (install_dir / executable_name("ffmpeg")).read_bytes() == b"MZ-ffmpeg"
    assert (install_dir / executable_name("ffprobe")).read_bytes() == b"MZ-ffprobe"
    assert not (install_dir / "readme.txt").exists()


def test_deno_is_optional_but_can_be_installed_and_found(tmp_path):
    payload = _deno_zip()
    release = _release("deno", payload, version="2.4.0", archive="deno-zip")
    updater = ToolUpdater(
        client=FakeHttpClient({release.download_url: payload}),
        provider=FakeProvider({"deno": release}),
        version_probe=lambda _tool, _path: "2.4.0",
        root=tmp_path,
    )

    result = updater.update(("deno",))[0]

    assert result.changed is True
    assert active_binary("deno", tmp_path) == Path(result.path)


def test_recent_check_skips_network(tmp_path):
    (tmp_path / "update-state.json").write_text(
        json.dumps({"last_check": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    provider = FakeProvider({})
    updater = ToolUpdater(client=FakeHttpClient({}), provider=provider, root=tmp_path)

    batch = updater.check_and_update(UpdatePreferences(check_interval_hours=24))

    assert batch.skipped_reason
    assert provider.calls == []


def test_all_failed_checks_do_not_postpone_next_automatic_attempt(tmp_path):
    previous = "2020-01-01T00:00:00+00:00"
    state_path = tmp_path / "update-state.json"
    state_path.write_text(json.dumps({"last_check": previous}), encoding="utf-8")
    provider = FailingProvider()
    updater = ToolUpdater(client=FakeHttpClient({}), provider=provider, root=tmp_path)

    checks = updater.check(("yt-dlp", "ffmpeg", "deno"))

    assert all(check.error for check in checks)
    assert provider.calls == ["yt-dlp", "ffmpeg", "deno"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["last_check"] == previous
    assert updater._check_due(24) is True


def test_explicit_empty_tool_selection_never_falls_back_to_all_tools(tmp_path):
    provider = FakeProvider({})
    updater = ToolUpdater(client=FakeHttpClient({}), provider=provider, root=tmp_path)

    assert updater.check(()) == []
    assert updater.update(()) == []
    batch = updater.check_and_update(
        UpdatePreferences(
            update_yt_dlp=False,
            update_ffmpeg=False,
            update_deno=False,
        ),
        force=True,
    )

    assert batch.checks == []
    assert batch.results == []
    assert provider.calls == []


def test_forced_repair_ignores_stale_cached_release(tmp_path):
    payload = b"MZ-latest"
    latest = ToolRelease(
        "yt-dlp",
        "2026.08.07",
        "https://updates.example/yt-dlp-latest",
        hashlib.sha256(payload).hexdigest(),
    )
    provider = FakeProvider({"yt-dlp": latest})
    updater = ToolUpdater(
        client=FakeHttpClient({latest.download_url: payload}),
        provider=provider,
        version_probe=lambda _tool, _path: latest.version,
        root=tmp_path,
    )
    updater._release_cache["yt-dlp"] = ToolRelease(
        "yt-dlp",
        "2026.08.01",
        "https://updates.example/stale",
        "0" * 64,
    )

    result = updater.update(("yt-dlp",), force=True)[0]

    assert result.changed is True
    assert result.installed_version == latest.version
    assert provider.calls == ["yt-dlp"]
    assert updater._release_cache["yt-dlp"] == latest


def test_pruning_keeps_active_and_one_previous_version(tmp_path):
    updater = ToolUpdater(client=FakeHttpClient({}), provider=FakeProvider({}), root=tmp_path)
    directories = []
    for index in range(3):
        directory = tmp_path / "versions" / "yt-dlp" / f"version-{index}"
        directory.mkdir(parents=True)
        (directory / executable_name("yt-dlp")).write_bytes(f"MZ-{index}".encode())
        directories.append(directory)
    activate_tool("yt-dlp", "3", directories[-1], root=tmp_path, source="downloaded")

    removed = updater.prune_old_versions("yt-dlp", keep=2)

    assert directories[-1].exists()
    assert sum(directory.exists() for directory in directories) == 2
    assert len(removed) == 1


def test_failed_functional_probe_quarantines_candidate_and_preserves_active(tmp_path):
    old_dir = tmp_path / "versions" / "yt-dlp" / "old"
    old_dir.mkdir(parents=True)
    old_binary = old_dir / executable_name("yt-dlp")
    old_binary.write_bytes(b"MZ-old")
    activate_tool("yt-dlp", "2026.08.01", old_dir, root=tmp_path, source="downloaded", sha256="a" * 64)
    payload = b"MZ-bad-but-starts"
    release = _release("yt-dlp", payload, version="2026.08.08")
    provider = FakeProvider({"yt-dlp": release})
    updater = ToolUpdater(
        client=FakeHttpClient({release.download_url: payload}),
        provider=provider,
        version_probe=lambda _tool, path: "2026.08.08" if path.read_bytes() == payload else "2026.08.01",
        health_probe=lambda tool, _path, _deep: CompatibilityCheck(
            tool, CompatibilityLevel.FAILED, "challenge incompatível"
        ),
        functional_validation=True,
        root=tmp_path,
    )

    result = updater.update(("yt-dlp",))[0]
    checked = updater.check(("yt-dlp",))[0]

    assert result.changed is False
    assert "quarentena" in result.error
    assert active_binary("yt-dlp", tmp_path) == old_binary
    assert checked.quarantined is True
    assert checked.available is False
    assert checked.quarantine_reason == "challenge incompatível"


def test_manual_rollback_restores_newest_valid_previous_copy(tmp_path):
    previous = tmp_path / "versions" / "yt-dlp" / "previous"
    current = tmp_path / "versions" / "yt-dlp" / "current"
    previous.mkdir(parents=True)
    current.mkdir(parents=True)
    (previous / executable_name("yt-dlp")).write_bytes(b"MZ-previous")
    (current / executable_name("yt-dlp")).write_bytes(b"MZ-current")
    activate_tool("yt-dlp", "2026.08.01", previous, root=tmp_path, source="downloaded")
    activate_tool("yt-dlp", "2026.08.08", current, root=tmp_path, source="downloaded")
    updater = ToolUpdater(
        client=FakeHttpClient({}),
        provider=FakeProvider({}),
        version_probe=lambda _tool, path: "2026.08.01" if "previous" in str(path) else "2026.08.08",
        root=tmp_path,
    )

    result = updater.rollback("yt-dlp", reason="teste")

    assert result.changed is True
    assert result.restored_version == "2026.08.01"
    assert active_binary("yt-dlp", tmp_path) == previous / executable_name("yt-dlp")


def test_recording_check_preserves_autocure_state(tmp_path):
    state_path = tmp_path / "update-state.json"
    state_path.write_text(json.dumps({"quarantine": {"keep": {"reason": "x"}}}), encoding="utf-8")
    release = _release("yt-dlp", b"MZ", version="2026.08.08")
    updater = ToolUpdater(
        client=FakeHttpClient({}),
        provider=FakeProvider({"yt-dlp": release}),
        version_probe=lambda _tool, _path: None,
        root=tmp_path,
    )

    updater.check(("yt-dlp",))

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "last_check" in saved
    assert saved["quarantine"]["keep"]["reason"] == "x"


def test_manual_autocure_does_not_create_failure_or_redownload_healthy_tool(tmp_path):
    current = tmp_path / "versions" / "yt-dlp" / "current"
    current.mkdir(parents=True)
    binary = current / executable_name("yt-dlp")
    binary.write_bytes(b"MZ-current")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    activate_tool("yt-dlp", "2026.08.08", current, root=tmp_path, source="downloaded", sha256=digest)
    release = ToolRelease("yt-dlp", "2026.08.08", "https://updates.example/yt-dlp", digest)
    provider = FakeProvider({"yt-dlp": release})
    updater = ToolUpdater(
        client=FakeHttpClient({}),
        provider=provider,
        version_probe=lambda _tool, _path: "2026.08.08",
        health_probe=lambda tool, _path, _deep: CompatibilityCheck(
            tool, CompatibilityLevel.HEALTHY, "operacional", version="2026.08.08"
        ),
        root=tmp_path,
    )

    result = updater.autocure("manual", tools=("yt-dlp",), report_failure=False)

    assert result.health_after and result.health_after.ok
    assert result.updates == []
    assert "failures" not in json.loads((tmp_path / "update-state.json").read_text(encoding="utf-8"))
    assert provider.calls == ["yt-dlp"]
