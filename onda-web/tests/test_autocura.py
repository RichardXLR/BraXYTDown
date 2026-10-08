"""No live YouTube requests, deployments, credentials, or package execution."""
import hashlib
import io
import json
from functools import lru_cache
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.parse

import pytest

from scripts.autocura import (AutoCura, CureError, DependencyCandidate, DeploymentChecks,
                             HTTP, MAX_RESPONSE, MAX_PACKAGE_METADATA_RESPONSE,
                             RepositoryCheckpoint, Vercel, atomic_json, deployment_url, process_lock,
                             same_version, subprocess_environment, verify_downloaded_audio, verify_downloaded_video)

VERSIONS = {"yt-dlp": "2026.8.19", "imageio-ffmpeg": "0.6.0", "deno": "2.9.7"}
OLD = {"id": "dpl_previous", "url": "https://previous.vercel.app", "readyState": "READY"}
NEW = {"id": "dpl_candidate", "url": "https://candidate.vercel.app", "readyState": "READY"}
PINS = "fastapi==0.141.1\nuvicorn==0.52.1\nyt-dlp[default]==2026.8.18\nimageio-ffmpeg==0.6.0\ndeno==2.9.6\npublicsuffixlist==1.0.2.20261003\n"


def fake_resolution_run(monkeypatch, versions, calls=None):
    def resolve(command, **_options):
        if calls is not None:
            calls.append(command)
        assert command[1:4] == ["-m", "pip", "install"]
        assert "--dry-run" in command and "--ignore-installed" in command
        assert "--only-binary=:all:" in command
        report = Path(command[command.index("--report") + 1])
        report.write_text(json.dumps({"install": [{"metadata": {"name": name, "version": version}}
                                                  for name, version in versions.items()]}))
        return ""
    monkeypatch.setattr("scripts.autocura.run", resolve)


class FakeProvider:
    def __init__(self, events):
        self.events = events
        self.active = dict(OLD)
        self.promotions = []
        self.rollbacks = []

    def current(self):
        self.events.append("current")
        return self.active

    def stage(self):
        self.events.append("stage")
        return dict(NEW)

    def promote(self, deployment):
        self.events.append("promote")
        self.promotions.append(deployment)
        self.active = dict(deployment)

    def rollback(self, deployment):
        self.events.append("rollback")
        self.rollbacks.append(deployment)
        self.active = dict(deployment)


class FakeDependencies:
    def __init__(self, root, events):
        self.root, self.events = root, events

    def latest(self):
        self.events.append("latest")
        return dict(VERSIONS)

    def prepare(self, versions):
        self.events.append("prepare")
        (self.root / "requirements.txt").write_text("new verified pins\n")
        return {"versions": versions}


class FakeChecks:
    def __init__(self, statuses, events):
        self.statuses, self.events = iter(statuses), events

    def verify(self, deployment, versions=None):
        self.events.append("test:" + deployment["id"])
        return {"status": next(self.statuses), "checks": [], "checked_at": "now"}


@pytest.fixture
def factory(tmp_path):
    (tmp_path / "requirements.txt").write_text(PINS)
    (tmp_path / "public").mkdir()

    def build(statuses=("passed", "passed"), checkpoint=True):
        events = []
        provider = FakeProvider(events)
        records = []

        def save(state, *, include_requirements=False):
            events.append("checkpoint:" + state["status"])
            records.append(json.loads(json.dumps(state)))

        manager = AutoCura(tmp_path, provider, FakeDependencies(tmp_path, events),
                          FakeChecks(statuses, events), save if checkpoint else None)
        return manager, provider, events, records

    return build


def test_promotion_is_after_all_checks_and_durable_pending_journal(factory, tmp_path):
    manager, provider, events, records = factory()
    state = manager.release()
    assert state["status"] == "passed"
    assert state["active"] == NEW and state["previous"] == OLD
    assert state["versions"] == VERSIONS
    assert "pending" not in state
    assert events.index("checkpoint:promotion_pending") < events.index("promote")
    assert len([event for event in events if event.startswith("test:")]) == 2
    assert records[0]["pending"]["previous"] == OLD
    assert provider.promotions == [NEW] and provider.rollbacks == []
    assert (tmp_path / "requirements.txt").read_text() == "new verified pins\n"


def test_vercel_candidate_uses_separate_remote_preview_without_assigning_production(monkeypatch, tmp_path):
    from scripts import autocura
    monkeypatch.setenv('VERCEL_TOKEN', 'fake-ci-token')
    monkeypatch.setenv('VERCEL_PROJECT_ID', 'prj_test')
    monkeypatch.setenv('VERCEL_ORG_ID', 'team_test')
    calls = []
    def invoke(command, **options):
        calls.append((command, options))
        return 'https://candidate.vercel.app' if command[1] == 'deploy' else ''
    monkeypatch.setattr(autocura, 'run', invoke)
    provider = Vercel(tmp_path)
    monkeypatch.setattr(provider, 'inspect', lambda host: dict(NEW) if host == 'candidate.vercel.app' else pytest.fail('Unexpected deployment'))
    assert provider.stage() == NEW
    assert [command[1] for command, _ in calls] == ['pull', 'deploy']
    command, options = calls[-1]
    assert '--prod' not in command and '--skip-domain' not in command and '--prebuilt' not in command
    assert '--environment=preview' in calls[0][0]
    assert options['credentials'] is True and options['error_code'] == 'vercel_deploy_failed'


@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_failed_candidate_never_changes_production_and_restores_pins(factory, tmp_path, status):
    manager, provider, _, _ = factory((status,))
    state = manager.release()
    assert state["status"] == status
    assert provider.active == OLD and not provider.promotions
    assert bool(state.get("quarantine")) == (status == "failed")
    assert (tmp_path / "requirements.txt").read_text() == PINS


def test_quarantine_is_persisted_and_only_manual_force_retries(factory):
    manager, _, _, _ = factory(("failed",))
    manager.release()
    next_manager, provider, events, _ = factory()
    assert next_manager.release()["status"] == "quarantined"
    assert "prepare" not in events and not provider.promotions
    state = next_manager.release(force=True)
    assert state["status"] == "passed" and state["quarantine"] == []


@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_post_activation_check_rolls_back_to_previous_ready(factory, tmp_path, status):
    manager, provider, _, records = factory(("passed", status))
    state = manager.release()
    assert provider.promotions == [NEW] and provider.rollbacks == [OLD]
    assert provider.active == OLD and state["status"] == "rolled_back"
    assert bool(state["quarantine"]) == (status == "failed")
    assert (tmp_path / "requirements.txt").read_text() == PINS
    assert records[-1]["active"] == OLD


def test_without_durable_checkpoint_candidate_is_not_promoted(factory):
    manager, provider, _, _ = factory(checkpoint=False)
    state = manager.release()
    assert state["status"] == "failed"
    assert state["last_error"] == "durable_checkpoint_required_before_promotion"
    assert not provider.promotions


class SimulatedProcessDeath(BaseException):
    pass


def test_crash_after_domain_change_is_recovered_from_persisted_pending(factory):
    manager, provider, events, _ = factory()

    def die_after_promotion(deployment):
        provider.active = deployment
        raise SimulatedProcessDeath()

    provider.promote = die_after_promotion
    with pytest.raises(SimulatedProcessDeath):
        manager.release()
    disk = json.loads(manager.path.read_text())
    assert disk["pending"]["candidate"] == NEW
    recovered, _, _, _ = factory()
    recovered.provider = provider
    assert recovered.recover()
    assert provider.active == OLD
    assert "pending" not in json.loads(manager.path.read_text())
    assert recovered.state["quarantine"][0]["reason"] == "unconfirmed_interrupted_promotion"


def test_recovery_of_already_restored_domain_clears_journal_without_redundant_rollback(factory):
    manager, provider, events, records = factory()
    manager.state["pending"] = {"candidate": NEW, "previous": OLD, "versions": VERSIONS,
                                "previous_versions": {"yt-dlp": "older"}}
    manager.state["status"] = "blocked"
    manager.save()
    assert manager.recover()
    assert provider.rollbacks == [] and provider.active == OLD
    assert "pending" not in records[-1] and "pending" not in json.loads(manager.path.read_text())
    assert records[-1]["status"] == "rolled_back"
    assert records[-1]["versions"] == {"yt-dlp": "older"}
    assert records[-1]["quarantine"][0]["reason"] == "unconfirmed_interrupted_promotion"
    assert events[-1] == "checkpoint:rolled_back"


def test_after_promotion_checks_use_the_actual_public_domain(factory):
    manager, provider, _, _ = factory()
    provider.production_view = lambda deployment: {**deployment, "url": "https://onda-audio.vercel.app"}
    seen = []
    def verify(deployment, versions=None):
        seen.append(deployment["url"])
        return {"status": "passed", "checks": []}
    manager.checks.verify = verify
    assert manager.release()["status"] == "passed"
    assert seen == [NEW["url"], "https://onda-audio.vercel.app"]


def test_failure_to_rollback_keeps_journal_for_next_run(factory):
    manager, provider, _, _ = factory(("passed", "failed"))

    def unavailable(_):
        raise CureError("network_unavailable")

    provider.rollback = unavailable
    state = manager.release()
    assert state["pending"]["previous"] == OLD
    assert json.loads(manager.path.read_text())["pending"]["previous"] == OLD


def test_manual_rollback_only_uses_saved_ready_baseline(factory):
    manager, provider, _, _ = factory()
    manager.release()
    manager.rollback()
    assert provider.active == OLD
    assert manager.state["previous"] == NEW


def test_atomic_json_and_real_process_lock(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"state": "one"})
    atomic_json(path, {"state": "two"})
    assert json.loads(path.read_text()) == {"state": "two"}
    assert not list(tmp_path.glob(".state.json.*"))
    with process_lock(tmp_path / "lock"):
        with pytest.raises(CureError, match="already_running"):
            with process_lock(tmp_path / "lock"):
                pass


def test_existing_non_tool_pins_and_default_ejs_are_preserved(tmp_path):
    (tmp_path / "requirements.txt").write_text(PINS)
    specifications = DependencyCandidate(tmp_path).specifications(VERSIONS)
    assert "publicsuffixlist==1.0.2.20261003" in specifications
    assert "fastapi==0.141.1" in specifications
    assert "yt-dlp[default]==2026.8.19" in specifications


def test_a_generated_hash_lock_can_be_updated_again(tmp_path):
    (tmp_path / "requirements.txt").write_text("--require-hashes\n" + PINS.replace("\n", " --hash=sha256:" + "a" * 64 + "\n"))
    assert "deno==2.9.7" in DependencyCandidate(tmp_path).specifications(VERSIONS)


def test_update_checks_all_reviewed_components_not_only_extraction_tools(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text(PINS)
    requested = []

    class Registry:
        def json(self, url, **kwargs):
            assert kwargs == {"max_response": MAX_PACKAGE_METADATA_RESPONSE}
            name = url.split("/")[-2]
            requested.append(name)
            return {"info": {"version": {"fastapi": "0.141.2", "uvicorn": "0.52.2",
                    "publicsuffixlist": "1.0.2.20261007"}.get(name, VERSIONS.get(name))}}

    candidate = DependencyCandidate(tmp_path, Registry())
    fake_resolution_run(monkeypatch, {**VERSIONS, "fastapi": "0.141.2", "uvicorn": "0.52.2",
                                     "publicsuffixlist": "1.0.2.20261007"})
    versions = candidate.latest()
    assert set(requested) == {"fastapi", "uvicorn", "publicsuffixlist", *VERSIONS}
    assert "fastapi==0.141.2" in candidate.specifications(versions)
    assert "uvicorn==0.52.2" in candidate.specifications(versions)


def test_parent_resolution_selects_compatible_children_and_ignores_old_flat_lock_pins(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text(PINS + "pydantic==2.13.4\npydantic-core==2.46.4\n")
    parents = {**VERSIONS, "fastapi": "0.142.4", "uvicorn": "0.54.0", "publicsuffixlist": "1.0.2.20261007"}
    compatible = {**parents, "pydantic": "2.13.5", "pydantic-core": "2.46.5", "new-dependency": "1.0"}
    requested, commands = [], []

    class Registry:
        def json(self, url, **_kwargs):
            name = url.split("/")[-2]
            requested.append(name)
            # Standalone pydantic-core 2.49.0 cannot satisfy pydantic 2.13.5's
            # exact 2.46.5 requirement. Do not independently pick child latest.
            assert name in parents
            return {"info": {"version": parents[name]}}

    fake_resolution_run(monkeypatch, compatible, commands)
    candidate = DependencyCandidate(tmp_path, Registry())
    versions = candidate.latest()
    assert versions == compatible and candidate.resolved_versions == compatible
    assert set(requested) == set(parents)
    specifications = candidate.specifications(versions)
    assert len(specifications) == 6
    assert not any(value.startswith(("pydantic==", "pydantic-core==", "new-dependency==")) for value in specifications)
    assert commands[0][-6:] == specifications
    forged = {**versions, "unresolved-package": "1.0"}
    with pytest.raises(CureError, match="unreviewed_component_update"):
        candidate.specifications(forged)
    versions["new-dependency"] = "2.0"
    assert candidate.resolved_versions["new-dependency"] == "1.0"
    with pytest.raises(CureError, match="unreviewed_component_update"):
        candidate.specifications(versions)


@pytest.mark.parametrize("fault", ["missing_parent", "parent_changed", "duplicate", "prerelease", "unsafe_name", "too_many", "bad_shape"])
def test_resolved_dependency_graph_requires_valid_bounded_compatible_metadata(tmp_path, monkeypatch, fault):
    (tmp_path / "requirements.txt").write_text(PINS)
    parents = {**VERSIONS, "fastapi": "0.142.4", "uvicorn": "0.54.0", "publicsuffixlist": "1.0.2.20261007"}

    class Registry:
        def json(self, url, **_kwargs):
            return {"info": {"version": parents[url.split("/")[-2]]}}

    def resolve(command, **_kwargs):
        items = [{"metadata": {"name": name, "version": version}} for name, version in parents.items()]
        if fault == "missing_parent": items.pop()
        elif fault == "parent_changed": items[0]["metadata"]["version"] = "1.0"
        elif fault == "duplicate": items.append(items[0])
        elif fault == "prerelease": items.append({"metadata": {"name": "child", "version": "1.0rc1"}})
        elif fault == "unsafe_name": items.append({"metadata": {"name": "https://other/child", "version": "1.0"}})
        elif fault == "too_many": items += [{"metadata": {"name": "child-" + str(i), "version": "1.0"}} for i in range(81)]
        elif fault == "bad_shape": items = {}
        Path(command[command.index("--report") + 1]).write_text(json.dumps({"install": items}))
        return ""
    monkeypatch.setattr("scripts.autocura.run", resolve)
    candidate = DependencyCandidate(tmp_path, Registry())
    with pytest.raises(CureError, match="invalid_dependency_resolution|resolved_parent_version_mismatch"):
        candidate.latest()
    assert candidate.resolved_versions is None


def test_second_resolution_must_match_discovery_before_any_wheel_is_executed(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text(PINS)
    candidate = DependencyCandidate(tmp_path)
    selected = {**VERSIONS, "fastapi": "0.142.4", "uvicorn": "0.54.0",
                "publicsuffixlist": "1.0.2.20261007", "pydantic": "2.13.5", "pydantic-core": "2.46.5"}
    candidate.resolved_versions = dict(selected)
    calls = []

    def download(command, **_kwargs):
        calls.append(command)
        assert command[1:4] == ["-m", "pip", "download"]
        return ""

    monkeypatch.setattr("scripts.autocura.run", download)
    monkeypatch.setattr(candidate, "verify_wheels", lambda _folder: [
        {"name": name, "version": "2.49.0" if name == "pydantic-core" else version}
        for name, version in selected.items()])
    monkeypatch.setattr("venv.EnvBuilder", lambda **_kwargs: pytest.fail("A changed dependency graph must not be installed"))
    with pytest.raises(CureError, match="dependency_resolution_changed"):
        candidate.prepare(selected)
    assert len(calls) == 1
    assert (tmp_path / "requirements.txt").read_text() == PINS


def test_unreviewed_packages_and_prereleases_are_not_auto_installed(tmp_path):
    (tmp_path / "requirements.txt").write_text(PINS)
    with pytest.raises(CureError, match="unreviewed_component"):
        DependencyCandidate(tmp_path).specifications({"new-unreviewed-package": "1.0"})

    class Registry:
        def json(self, _url, **kwargs):
            return {"info": {"version": "2.0rc1"}}

    with pytest.raises(CureError, match="prerelease_component"):
        DependencyCandidate(tmp_path, Registry()).latest()


class WheelMetadata:
    def __init__(self, files, *, invalid=None):
        self.files, self.invalid = files, invalid

    def json(self, url):
        name, version = url.split("/")[-3:-1]
        filename, content = self.files[name]
        digest = hashlib.sha256(content).hexdigest()
        return {"urls": [{"filename": filename, "packagetype": "bdist_wheel", "yanked": False,
                         "url": "https://files.pythonhosted.org/packages/" + filename,
                         "digests": {"sha256": "f" * 64 if self.invalid == name else digest}}]}


def test_hashes_every_transitive_wheel_before_installation(tmp_path):
    files = {"yt-dlp": ("yt_dlp-2026.8.19-py3-none-any.whl", b"primary"),
             "certifi": ("certifi-2026.7.22-py3-none-any.whl", b"transitive")}
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for filename, content in files.values():
        (wheels / filename).write_bytes(content)
    result = DependencyCandidate(tmp_path, WheelMetadata(files)).verify_wheels(wheels)
    assert {item["name"] for item in result} == {"yt-dlp", "certifi"}
    with pytest.raises(CureError, match="wheel_integrity_failed"):
        DependencyCandidate(tmp_path, WheelMetadata(files, invalid="certifi")).verify_wheels(wheels)


def test_source_distributions_are_never_executed(tmp_path):
    (tmp_path / "evil.tar.gz").write_bytes(b"untrusted setup.py")
    with pytest.raises(CureError, match="only_binary_wheels"):
        DependencyCandidate(tmp_path).verify_wheels(tmp_path)


class CanaryHTTP:
    def __init__(self, *, blocked=False, mismatch=False):
        self.blocked, self.mismatch = blocked, mismatch

    def json(self, url, **kwargs):
        if url.endswith("/api/health"):
            return {"ok": True, "formats": ["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff"],
                    "videoFormats": ["mp4", "webm", "mkv", "mov"]}
        if url.endswith("/api/compatibility"):
            return {"versions": {"ytDlp": "wrong" if self.mismatch else VERSIONS["yt-dlp"],
                                 "deno": VERSIONS["deno"], "ffmpeg": "7.0.2"}}
        if self.blocked:
            return {"ok": False, "status": "blocked", "code": "platform_blocked", "error": "Login required"}
        return {"ok": True, "status": "passed", "details": {"title": "Canary"}}

    def request(self, url, **kwargs):
        if kwargs.get("method") == "HEAD":
            return b"", {"Content-Type": "video/mp4" if url.endswith(".mp4") else "audio/wav"}
        if kwargs["payload"].get("media_type") == "video":
            payload = kwargs["payload"]
            video_format = payload["format"]
            mime = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "mov": "video/quicktime"}[video_format]
            return edited_video_bytes(muted=payload["mute"], video_format=video_format,
                                      normalized=payload["normalize_audio"]), {"Content-Type": mime}
        audio_format = kwargs["payload"]["format"]
        mime = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "flac": "audio/flac",
                "ogg": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac", "aiff": "audio/aiff"}[audio_format]
        return converted_audio_bytes(audio_format), {"Content-Type": mime}


@lru_cache(maxsize=32)
def edited_video_bytes(*, muted=False, strip_metadata=True, duration=.6, video_format="mp4", normalized=False):
    """Small real response fixtures; no network or platform content."""
    import imageio_ffmpeg

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / ("response." + video_format)
        source = Path(__file__).resolve().parents[1] / "public" / "canary.mp4"
        video_codec, audio_codec = ("libvpx-vp9", "libopus") if video_format == "webm" else ("libx264", "aac")
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error",
                   "-i", str(source), "-ss", "0.2", "-t", str(duration), "-c:v", video_codec, "-pix_fmt", "yuv420p"]
        if video_format == "webm":
            command += ["-deadline", "realtime", "-cpu-used", "8", "-ar", "48000"]
        if strip_metadata:
            command += ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"]
        command += ["-an"] if muted else ["-c:a", audio_codec]
        if normalized:
            command += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000"]
        subprocess.run(command + [str(target)], check=True, capture_output=True, timeout=20)
        return target.read_bytes()


@lru_cache(maxsize=8)
def converted_audio_bytes(audio_format):
    import imageio_ffmpeg

    codecs = {"mp3": "libmp3lame", "m4a": "aac", "wav": "pcm_s16le", "flac": "flac",
              "ogg": "libvorbis", "opus": "libopus", "aac": "aac", "aiff": "pcm_s16be"}
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / ("response." + audio_format)
        source = Path(__file__).resolve().parents[1] / "public" / "canary.wav"
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error",
                   "-i", str(source), "-c:a", codecs[audio_format]]
        if audio_format == "opus":
            command += ["-ar", "48000"]
        subprocess.run(command + [str(target)], check=True, capture_output=True, timeout=20)
        return target.read_bytes()


def test_real_checks_do_not_label_platform_blocks_healthy():
    result = DeploymentChecks(CanaryHTTP(blocked=True)).verify(NEW, versions=VERSIONS)
    assert result["status"] == "blocked"
    assert sum(check["status"] == "blocked" for check in result["checks"]) == 2


def test_baseline_policy_keeps_existing_youtube_block_visible_without_stalling_codecs():
    checks = DeploymentChecks(CanaryHTTP(blocked=True), youtube_policy="baseline")
    captured = checks.capture_platform_baseline(OLD)
    result = checks.verify(NEW, versions=VERSIONS)
    assert captured["deployment_id"] == OLD["id"]
    assert result["status"] == "passed"  # The release gate passed, not YouTube.
    assert result["coverage"]["youtube_status"] == "blocked"
    assert result["release_gate"]["youtube_verified"] is False
    assert len(result["release_gate"]["accepted_existing_blocks"]) == 2
    assert sum(check["status"] == "blocked" for check in result["checks"]) == 2
    assert all(check["status"] == "passed" for check in result["checks"] if check["name"].startswith("owned_"))
    assert checks.verify(NEW, versions=VERSIONS)["platform_baseline"] == captured


@pytest.mark.parametrize("baseline_code", [None, "network_unavailable", "busy", "http_403"])
def test_new_or_unverified_platform_block_still_prevents_promotion(baseline_code):
    class ChangingPlatform(CanaryHTTP):
        def json(self, url, **kwargs):
            if "/api/compatibility/test" in url and "previous.vercel.app" in url:
                if baseline_code is None:
                    return {"ok": True, "status": "passed", "details": {"title": "Baseline works"}}
                raise CureError(baseline_code)
            return super().json(url, **kwargs)

    checks = DeploymentChecks(ChangingPlatform(blocked=True), youtube_policy="baseline")
    checks.capture_platform_baseline(OLD)
    result = checks.verify(NEW, versions=VERSIONS)
    assert result["status"] == "blocked"
    assert result["release_gate"]["accepted_existing_blocks"] == []


def test_baseline_policy_cannot_skip_missing_baseline_or_real_converter_failure():
    checks = DeploymentChecks(CanaryHTTP(blocked=True), youtube_policy="baseline")
    assert checks.verify(NEW, versions=VERSIONS)["status"] == "blocked"

    class BrokenConverter(CanaryHTTP):
        def request(self, url, **kwargs):
            if kwargs.get("payload", {}).get("format") == "flac":
                raise CureError("audio_conversion_failed")
            return super().request(url, **kwargs)

    checks = DeploymentChecks(BrokenConverter(blocked=True), youtube_policy="baseline")
    checks.capture_platform_baseline(OLD)
    result = checks.verify(NEW, versions=VERSIONS)
    assert result["status"] == "failed"
    assert len(result["release_gate"]["accepted_existing_blocks"]) == 2


def test_strict_policy_remains_the_default_and_invalid_policy_is_rejected():
    checks = DeploymentChecks(CanaryHTTP(blocked=True), youtube_policy="strict")
    assert checks.capture_platform_baseline(OLD) is None
    assert checks.verify(NEW, versions=VERSIONS)["status"] == "blocked"
    with pytest.raises(CureError, match="invalid_youtube_gate_policy"):
        DeploymentChecks(youtube_policy="skip_all")


def test_packaged_runtime_must_match_candidate_versions():
    result = DeploymentChecks(CanaryHTTP(mismatch=True)).verify(NEW, versions=VERSIONS)
    assert result["status"] == "failed"
    assert next(item for item in result["checks"] if item["name"] == "packaged_versions")["code"] == "packaged_runtime_version_mismatch"


def test_pypi_normalization_does_not_quarantine_valid_ytdlp_versions():
    assert same_version("2026.08.19", "2026.8.19")
    assert not same_version("2026.08.18", "2026.8.19")


@pytest.mark.parametrize("cause", ["403", "login_page"])
def test_protected_source_is_blocked_without_condemning_the_converter(cause):
    class ProtectedAudio(CanaryHTTP):
        def request(self, url, **kwargs):
            if kwargs.get("method") == "HEAD":
                assert "headers" not in kwargs  # Backend fetch will not receive CI's bypass either.
                if cause == "403":
                    raise CureError("http_403")
                return b"", {"Content-Type": "text/html"}
            raise AssertionError("Do not download a protected or HTML source")

    result = DeploymentChecks(ProtectedAudio(), bypass="private-bypass").verify(NEW, versions=VERSIONS)
    assert result["status"] == "blocked"
    assert sum(check["status"] == "blocked" for check in result["checks"]) == 13


def test_authorized_public_audio_source_can_be_configured_for_protected_staging():
    class ConfiguredSource(CanaryHTTP):
        def request(self, url, **kwargs):
            if url.endswith(".mp4") or kwargs.get("payload", {}).get("media_type") == "video":
                return super().request(url, **kwargs)
            if kwargs.get("method") == "HEAD":
                assert url == "https://public-production.vercel.app/canary.wav"
            else:
                assert kwargs["payload"]["url"] == "https://public-production.vercel.app/canary.wav"
            return super().request(url, **kwargs)

    checks = DeploymentChecks(ConfiguredSource(), audio_canary_url="https://public-production.vercel.app/canary.wav")
    assert checks.verify(NEW, versions=VERSIONS)["status"] == "passed"


def test_candidate_api_suite_failure_quarantines_and_preserves_the_baseline(factory):
    manager, provider, _, _ = factory()

    def failed_suite(_versions):
        raise CureError("candidate_application_tests_failed")

    manager.dependencies.prepare = failed_suite
    state = manager.release()
    assert state["status"] == "failed"
    assert state["quarantine"][0]["reason"] == "candidate_application_tests_failed"
    assert not provider.promotions and provider.active == OLD


def test_new_test_dependencies_are_verified_before_candidate_suite_executes(tmp_path, monkeypatch):
    candidate = DependencyCandidate(tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return ""

    def bad_wheels(_folder):
        raise CureError("wheel_integrity_failed")

    monkeypatch.setattr("scripts.autocura.run", fake_run)
    candidate.verify_wheels = bad_wheels
    with pytest.raises(CureError, match="integrity_failed"):
        candidate.verify_application(Path("/candidate/bin/python"), [], tmp_path)
    assert all("pytest" not in command for command in calls)


def test_real_checks_require_all_eight_audio_and_four_video_containers():
    result = DeploymentChecks(CanaryHTTP()).verify(NEW, versions=VERSIONS)
    assert result["status"] == "passed" and len(result["checks"]) == 17
    assert result["coverage"]["youtube"] == "metadata_only"
    assert result["coverage"]["video_conversion"] == "project_owned_clip"
    assert sum(check["name"].startswith("owned_tone_") for check in result["checks"]) == 8
    assert sum(check["name"].startswith("owned_clip_") for check in result["checks"]) == 5


@pytest.mark.parametrize("muted", [False, True])
def test_video_gate_decodes_codec_trim_and_audio_edit(muted):
    result = verify_downloaded_video(edited_video_bytes(muted=muted), muted=muted)
    assert result["videoCodec"] == "h264" and abs(result["duration"] - .6) < .16
    assert result["audioCodec"] == (None if muted else "aac")
    assert result["privacy"] == "custom_tags_removed"


def test_video_gate_allows_technical_muxer_tags_but_rejects_private_source_tags():
    verify_downloaded_video(edited_video_bytes())
    with pytest.raises(CureError, match="private_metadata_retained"):
        verify_downloaded_video(edited_video_bytes(strip_metadata=False))


def test_video_gate_rejects_ignored_trim_or_mute_settings():
    with pytest.raises(CureError, match="video_trim_failed"):
        verify_downloaded_video(edited_video_bytes(duration=1.5))
    with pytest.raises(CureError, match="video_audio_edit_failed"):
        verify_downloaded_video(edited_video_bytes(), muted=True)


def test_video_gate_measures_normalization_instead_of_trusting_request_flag():
    inspection = verify_downloaded_video(edited_video_bytes(normalized=True), normalized=True)
    assert abs(inspection["integratedLoudness"] + 16) < 1
    with pytest.raises(CureError, match="video_normalization_failed"):
        verify_downloaded_video(edited_video_bytes(), normalized=True)


@pytest.mark.parametrize("video_format", ["mp4", "webm", "mkv", "mov"])
def test_remote_gate_decodes_every_video_container(video_format):
    result = verify_downloaded_video(edited_video_bytes(video_format=video_format), video_format=video_format)
    assert result["videoCodec"] == ("vp9" if video_format == "webm" else "h264")


@pytest.mark.parametrize("audio_format", ["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff"])
def test_remote_gate_decodes_every_audio_container(audio_format):
    assert verify_downloaded_audio(converted_audio_bytes(audio_format), audio_format)["decoded"] is True


def test_declared_audio_mime_cannot_make_junk_or_wrong_codec_pass():
    with pytest.raises(CureError, match="audio_decode_failed"):
        verify_downloaded_audio(b"not audio" * 30, "mp3")
    with pytest.raises(CureError, match="audio_codec_failed"):
        verify_downloaded_audio(converted_audio_bytes("flac"), "mp3")


def test_invalid_or_nonvideo_bytes_cannot_pass_the_video_gate():
    with pytest.raises(CureError, match="video_decode_failed"):
        verify_downloaded_video(b"pretend video" * 30)


def test_retained_private_tags_fail_candidate_compatibility_gate():
    class LeakingResponse(CanaryHTTP):
        def request(self, url, **kwargs):
            if kwargs.get("payload", {}).get("media_type") == "video":
                return edited_video_bytes(strip_metadata=False), {"Content-Type": "video/mp4"}
            return super().request(url, **kwargs)

    result = DeploymentChecks(LeakingResponse()).verify(NEW, versions=VERSIONS)
    assert result["status"] == "failed"
    assert any(check.get("code") == "video_private_metadata_retained" for check in result["checks"])


def test_public_video_source_configuration_and_edit_payload_are_verified():
    class ConfiguredVideo(CanaryHTTP):
        def request(self, url, **kwargs):
            if kwargs.get("method") == "HEAD" and url.endswith(".mp4"):
                assert url == "https://public-production.vercel.app/canary.mp4"
                assert "headers" not in kwargs
            if kwargs.get("payload", {}).get("media_type") == "video":
                payload = kwargs["payload"]
                assert payload["url"] == "https://public-production.vercel.app/canary.mp4"
                assert payload["video_resolution"] == "480" and payload["strip_metadata"] is True
                assert payload["trim_start"] == .2 and payload["trim_end"] == .8
                assert payload["normalize_audio"] is (payload["format"] == "mp4" and not payload["mute"])
            return super().request(url, **kwargs)

    result = DeploymentChecks(ConfiguredVideo(), video_canary_url="https://public-production.vercel.app/canary.mp4").verify(NEW, versions=VERSIONS)
    assert result["status"] == "passed"


def test_release_gate_uses_real_api_editing_and_local_converters(monkeypatch):
    """Mock only remote acquisition; validate actual API settings and bytes."""
    from fastapi.testclient import TestClient
    from api import compatibility, engine, index

    public = Path(__file__).resolve().parents[1] / "public"

    def acquire_video(url, directory, guard, video_resolution="source", cookies=None, mute=False):
        guard.check()
        source = directory / "source.mp4"
        source.write_bytes((public / "canary.mp4").read_bytes())
        return source, {"title": "Owned clip", "duration": 2, "source": "Arquivo direto", "webpage_url": url}

    def acquire_audio(url, directory, guard, audio_format, cookies=None):
        guard.check()
        source = directory / "source.wav"
        source.write_bytes((public / "canary.wav").read_bytes())
        return source, {"title": "Owned tone", "duration": 1, "source": "Arquivo direto", "webpage_url": url}

    monkeypatch.setattr(engine, "acquire_video_media", acquire_video)
    monkeypatch.setattr(engine, "acquire_media", acquire_audio)
    monkeypatch.setattr(engine, "inspect_media", lambda url, guard, **kwargs: {"title": "Owned fixture metadata"})
    if hasattr(compatibility.versions, "cache_clear"):
        compatibility.versions.cache_clear()
    with TestClient(index.app) as client:
        class ActualAPI:
            def json(self, url, **kwargs):
                response = client.request(kwargs.get("method", "GET"), urllib.parse.urlsplit(url).path,
                                          json=kwargs.get("payload"))
                if response.status_code >= 400:
                    raise CureError(response.json()["code"])
                return response.json()

            def request(self, url, **kwargs):
                if kwargs.get("method") == "HEAD":
                    return b"", {"Content-Type": "video/mp4" if url.endswith(".mp4") else "audio/wav"}
                response = client.post("/api/download", json=kwargs["payload"])
                if response.status_code >= 400:
                    raise CureError(response.json()["code"])
                return response.content, response.headers

        result = DeploymentChecks(ActualAPI()).verify(NEW)
    assert result["status"] == "passed", result
    video_checks = [check for check in result["checks"] if check.get("scope") == "edited_video"]
    assert len(video_checks) == 5
    assert video_checks[0]["inspection"]["audioCodec"] == "aac"
    assert video_checks[1]["inspection"]["audioCodec"] is None


def test_authorized_youtube_audio_is_a_real_additional_gate_when_configured():
    result = DeploymentChecks(CanaryHTTP(), youtube_audio_canary_url="https://youtu.be/owned_short_video").verify(NEW, versions=VERSIONS)
    assert result["status"] == "passed"
    assert result["coverage"]["youtube"] == "metadata_and_audio"
    assert result["checks"][-1]["name"] == "authorized_youtube_audio"


def test_blocked_optional_youtube_audio_canary_prevents_promotion():
    class BlockedYoutubeAudio(CanaryHTTP):
        def request(self, url, **kwargs):
            if kwargs.get("payload", {}).get("url", "").startswith("https://youtu.be/"):
                raise CureError("platform_blocked")
            return super().request(url, **kwargs)

    result = DeploymentChecks(BlockedYoutubeAudio(), youtube_audio_canary_url="https://youtu.be/owned_short_video").verify(NEW, versions=VERSIONS)
    assert result["status"] == "blocked"
    assert result["checks"][-1]["code"] == "platform_blocked"


@pytest.mark.parametrize("url", ["https://attacker.example", "https://a.vercel.app@attacker.example", "https://a.vercel.app/?token=secret", "https://a.vercel.app:443"])
def test_only_expected_https_deployment_origins_are_used(url):
    with pytest.raises(CureError, match="invalid_deployment_url"):
        deployment_url(url)


def test_dependency_processes_do_not_receive_deployment_credentials(monkeypatch):
    monkeypatch.setenv("VERCEL_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://evil.example")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    environment = subprocess_environment()
    assert "VERCEL_TOKEN" not in environment and "GITHUB_TOKEN" not in environment
    assert "PIP_EXTRA_INDEX_URL" not in environment
    assert environment["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert environment["PIP_INDEX_URL"] == "https://pypi.org/simple"


def test_http_error_codes_do_not_expose_cookie_or_raw_body(monkeypatch):
    secret = b'{"code":"platform_blocked","error":"cookie=SECRET"}'

    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError("https://candidate.vercel.app", 422, "secret", {}, io.BytesIO(secret))

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(CureError) as caught:
        HTTP().json("https://candidate.vercel.app/api/inspect")
    assert str(caught.value) == "platform_blocked"
    assert "SECRET" not in str(caught.value)


def fake_http_bytes(monkeypatch, body):
    class Response(io.BytesIO):
        headers = {}

        def geturl(self):
            return self.url

    class Opener:
        def open(self, request, **_kwargs):
            response = Response(body)
            response.url = request.full_url
            return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())


def test_large_pypi_release_metadata_needs_an_explicit_larger_budget(monkeypatch):
    # Exercise actual byte reads beyond 8 MiB, rather than a declared length.
    body = json.dumps({"info": {"version": "2.46.5"}, "releases": "x" * MAX_RESPONSE}).encode()
    assert MAX_RESPONSE < len(body) < MAX_PACKAGE_METADATA_RESPONSE
    fake_http_bytes(monkeypatch, body)
    url = "https://pypi.org/pypi/pydantic-core/json"
    with pytest.raises(CureError, match="response_too_large"):
        HTTP().json(url)
    metadata = HTTP().json(url, max_response=MAX_PACKAGE_METADATA_RESPONSE)
    assert metadata["info"]["version"] == "2.46.5"
    assert len(metadata["releases"]) == MAX_RESPONSE
    with pytest.raises(CureError, match="response_too_large"):
        HTTP().request("https://candidate.vercel.app/api/download")


@pytest.mark.parametrize("limit", [0, -1, MAX_PACKAGE_METADATA_RESPONSE + 1, True, None, 1.5, "33554432"])
def test_http_response_budget_rejects_invalid_or_unbounded_limits(monkeypatch, limit):
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: pytest.fail("Invalid budgets must not open a connection"))
    with pytest.raises(CureError, match="invalid_response_limit"):
        HTTP().request("https://pypi.org/pypi/pydantic-core/json", max_response=limit)


def test_explicit_response_budget_still_enforces_actual_size(monkeypatch):
    fake_http_bytes(monkeypatch, b"12")
    with pytest.raises(CureError, match="response_too_large"):
        HTTP().request("https://pypi.org/pypi/example/json", max_response=1)
    assert HTTP().request("https://pypi.org/pypi/example/json", max_response=2)[0] == b"12"


def test_canary_binary_inspection_keeps_its_original_eight_mib_limit(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: pytest.fail("Oversized canaries must fail before decoding"))
    body = b"x" * (MAX_RESPONSE + 1)
    with pytest.raises(CureError, match="audio_conversion_failed"):
        verify_downloaded_audio(body, "mp3")
    with pytest.raises(CureError, match="video_conversion_failed"):
        verify_downloaded_video(body)


def test_provider_refuses_to_promote_another_project(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL_TOKEN", "secret")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "prj_expected")
    monkeypatch.setenv("VERCEL_ORG_ID", "team_expected")

    class WrongProject:
        def json(self, url, **kwargs):
            return {"id": NEW["id"], "projectId": "prj_other", "readyState": "READY", "url": "other.vercel.app"}

    provider = Vercel(tmp_path, WrongProject())
    with pytest.raises(CureError, match="not_ready_for_this_project"):
        provider.promote(NEW)


def test_current_deployment_follows_the_public_alias_instead_of_latest_build(tmp_path, monkeypatch):
    monkeypatch.setenv('VERCEL_TOKEN', 'fake')
    monkeypatch.setenv('VERCEL_PROJECT_ID', 'prj_test')
    monkeypatch.setenv('VERCEL_ORG_ID', 'team_test')
    class Mapping:
        def json(self, url, **options):
            if '/projects/' in url:
                return {'id': 'prj_test', 'targets': {'production': {'id': NEW['id']}}}
            if '/aliases/' in url:
                return {'projectId': 'prj_test', 'deploymentId': OLD['id']}
            assert OLD['id'] in url
            return {**OLD, 'projectId': 'prj_test'}
    assert Vercel(tmp_path, Mapping()).current() == OLD


def test_production_view_refuses_a_stale_alias(tmp_path, monkeypatch):
    monkeypatch.setenv('VERCEL_TOKEN', 'fake')
    monkeypatch.setenv('VERCEL_PROJECT_ID', 'prj_test')
    monkeypatch.setenv('VERCEL_ORG_ID', 'team_test')
    provider = Vercel(tmp_path)
    monkeypatch.setattr(provider, 'current', lambda: dict(OLD))
    with pytest.raises(CureError, match='production_mapping_not_confirmed'):
        provider.production_view(NEW)


@pytest.mark.parametrize('operation,target', [('promote', NEW), ('rollback', OLD)])
@pytest.mark.parametrize('already_mapped', [True, False])
@pytest.mark.parametrize('status', [409, 422])
def test_conflicts_are_idempotent_only_when_the_actual_alias_matches(tmp_path, monkeypatch, operation, target, already_mapped, status):
    monkeypatch.setenv('VERCEL_TOKEN', 'fake')
    monkeypatch.setenv('VERCEL_PROJECT_ID', 'prj_test')
    monkeypatch.setenv('VERCEL_ORG_ID', 'team_test')
    provider = Vercel(tmp_path)
    monkeypatch.setattr(provider, 'inspect', lambda _: dict(target))
    monkeypatch.setattr(provider, 'current', lambda: dict(target) if already_mapped else {'id': 'dpl_other'})
    def conflict(*args, **options):
        assert '/aliases' in args[0]
        assert options['payload'] == {'alias': provider.production_alias}
        raise CureError('conflict', http_status=status)
    monkeypatch.setattr(provider, 'api', conflict)
    if already_mapped:
        getattr(provider, operation)(target)
    else:
        with pytest.raises(CureError, match='conflict'):
            getattr(provider, operation)(target)


def test_nested_vercel_error_retains_status_without_disclosing_message(monkeypatch):
    body = b'{"error":{"code":"conflict","message":"token=SECRET"}}'
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://api.vercel.com', 409, 'secret', {}, io.BytesIO(body))
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    with pytest.raises(CureError) as caught:
        HTTP().json('https://api.vercel.com/v10/projects/test/promote/test')
    assert str(caught.value) == 'conflict' and caught.value.http_status == 409
    assert 'SECRET' not in str(caught.value)


def test_missing_credentials_are_not_claimed_enabled(tmp_path, monkeypatch):
    for name in ("VERCEL_TOKEN", "VERCEL_PROJECT_ID", "VERCEL_ORG_ID", "VERCEL_TEAM_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CureError, match="not_configured"):
        Vercel(tmp_path)


def test_manual_test_persists_actual_checks_and_a_history_entry(factory, tmp_path):
    manager, provider, _, records = factory(("passed",))
    state = manager.test()
    assert not provider.promotions
    assert state["last_check"]["status"] == "passed"
    assert state["history"][-1]["event"] == "verified"
    assert records[-1]["last_check"]["status"] == "passed"
    report = json.loads((tmp_path / "public" / "autocura.json").read_text())
    assert report["schema"] == 3 and report["last_check"]["status"] == "passed"


def test_baseline_is_captured_before_candidates_and_kept_in_durable_promotion_journal(factory):
    manager, provider, events, records = factory()

    def capture(deployment):
        events.append("capture_baseline")
        assert deployment == OLD
        return {"deployment_id": OLD["id"], "checked_at": "now",
                "checks": [{"name": "canary", "status": "blocked", "code": "platform_blocked"}]}

    manager.checks.capture_platform_baseline = capture
    manager.release()
    assert events.index("capture_baseline") < events.index("prepare")
    pending = next(record["pending"] for record in records if record["status"] == "promotion_pending")
    assert pending["platform_baseline"]["deployment_id"] == OLD["id"]


def checkpoint_environment(monkeypatch, workspace, subdirectory="onda-web"):
    monkeypatch.setenv("GITHUB_REPOSITORY", "RichardXLR/BraXYTDown")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_TOKEN", "unit-test-placeholder")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("AUTOCURA_REPOSITORY_SUBDIRECTORY", subdirectory)


def test_subdirectory_checkpoint_preserves_desktop_repository_paths(tmp_path, monkeypatch):
    root = tmp_path / "onda-web"
    (root / ".autocura").mkdir(parents=True)
    (root / "public").mkdir()
    for path in (".autocura/state.json", "public/autocura.json", ".autocura/artifacts.json", "requirements.txt"):
        (root / path).write_text("{}")
    checkpoint_environment(monkeypatch, tmp_path)
    calls = []

    class Repository:
        def json(self, url, **kwargs):
            path = urllib.parse.urlsplit(url).path.split("/BraXYTDown", 1)[-1]
            calls.append((path, kwargs))
            if path == "/git/ref/heads/main":
                return {"object": {"sha": "a" * 40}}
            if path == "/git/commits/" + "a" * 40:
                return {"tree": {"sha": "existing-desktop-tree"}}
            return {"sha": "new-verified-object"}

    checkpoint = RepositoryCheckpoint(root, Repository())
    checkpoint({"status": "passed"}, include_requirements=True)
    created_tree = next(kwargs["payload"] for path, kwargs in calls if path == "/git/trees")
    assert created_tree["base_tree"] == "existing-desktop-tree"
    assert {entry["path"] for entry in created_tree["tree"]} == {
        "onda-web/.autocura/state.json", "onda-web/public/autocura.json",
        "onda-web/.autocura/artifacts.json", "onda-web/requirements.txt"}
    reference_update = next(kwargs["payload"] for path, kwargs in calls if path == "/git/refs/heads/main")
    assert reference_update["force"] is False


@pytest.mark.parametrize("prefix", ["../onda-web", "/onda-web", "onda-web/..", "onda-web//", ".github", ".git"])
def test_checkpoint_rejects_unsafe_repository_prefixes(tmp_path, monkeypatch, prefix):
    checkpoint_environment(monkeypatch, tmp_path, prefix)
    with pytest.raises(CureError, match="unsafe_repository_subdirectory"):
        RepositoryCheckpoint(tmp_path / "onda-web")


def test_checkpoint_requires_runner_working_directory_to_match_prefix(tmp_path, monkeypatch):
    checkpoint_environment(monkeypatch, tmp_path)
    with pytest.raises(CureError, match="repository_subdirectory_mismatch"):
        RepositoryCheckpoint(tmp_path / "different-web")
