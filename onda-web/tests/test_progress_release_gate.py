"""No external media, credentials, remote builds or production writes."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import subprocess

import pytest

from scripts import autocura
from scripts.autocura import (CANONICAL_ORIGIN, CureError, DeploymentChecks,
                             DOWNLOAD_PROGRESS_CONTENT_TYPE as PROTOCOL,
                             DOWNLOAD_PROGRESS_JSON_LIMIT, DOWNLOAD_PROGRESS_BINARY_LIMIT,
                             MAX_RESPONSE, verify_progress_download)


NEW = {"id": "dpl_progress", "url": "https://candidate.vercel.app", "projectId": "prj_fixture"}


def frame(value, *, kind=1):
    payload = json.dumps(value, separators=(",", ":")).encode() if kind == 1 else value
    return bytes([kind]) + struct.pack(">I", len(payload)) + payload


def events(audio):
    size = len(audio)
    return [
        {"type": "progress", "stage": "extracting"},
        {"type": "heartbeat"},
        {"type": "progress", "stage": "downloading", "downloadedBytes": size,
         "totalBytes": size, "speedBytesPerSecond": 256000, "attempt": 1},
        {"type": "progress", "stage": "converting", "processedSeconds": 1,
         "durationSeconds": 1, "outputBytes": size},
        {"type": "file", "mime": "audio/mpeg", "name": "Owned tone.mp3", "title": "Owned tone",
         "size": size, "recovery": {"attempts": 1, "resumed": False, "conversion_recovered": False, "queued": False}},
        {"type": "progress", "stage": "delivering", "downloadedBytes": 0, "totalBytes": size},
        audio,
        {"type": "progress", "stage": "delivering", "downloadedBytes": size, "totalBytes": size},
        {"type": "progress", "stage": "ready", "downloadedBytes": size, "totalBytes": size},
        {"type": "complete", "size": size},
    ]


def encode(items):
    return b"".join(frame(item, kind=2 if isinstance(item, bytes) else 1) for item in items)


HEADERS = {"Content-Type": PROTOCOL}


@pytest.fixture(scope="module")
def encoded_audio(tmp_path_factory):
    import imageio_ffmpeg
    directory = tmp_path_factory.mktemp("progress-owned-tone")
    source = Path(__file__).resolve().parents[1] / "public" / "canary.wav"
    result = {}
    for extension, codec in (("mp3", "libmp3lame"), ("flac", "flac")):
        target = directory / ("tone." + extension)
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error",
                        "-protocol_whitelist", "file,pipe", "-i", str(source), "-c:a", codec, str(target)],
                       check=True, capture_output=True, timeout=20)
        result[extension] = target.read_bytes()
    return result


def test_owned_tone_stream_is_decoded_and_reports_only_controlled_fields(encoded_audio):
    audio = encoded_audio["mp3"]
    values = events(audio)
    values[4]["title"] = "https://private.example/secret"
    values[4]["name"] = "private title.mp3"
    values[2]["upstreamDetail"] = "Cookie: secret-value"
    inspection = verify_progress_download(encode(values), HEADERS)
    assert inspection == {"protocol": PROTOCOL,
                          "stages": ["extracting", "downloading", "converting", "delivering", "ready"],
                          "completed": True, "bytes": len(audio),
                          "inspection": {"audioCodec": "mp3", "decoded": True}}
    assert "secret" not in json.dumps(inspection) and "private" not in json.dumps(inspection)


@pytest.mark.parametrize("position", [1, 4, 5, 17, -1])
def test_truncated_header_payload_and_footer_never_pass(encoded_audio, position):
    content = encode(events(encoded_audio["mp3"]))
    with pytest.raises(CureError, match="download_progress_truncated"):
        verify_progress_download(content[:position], HEADERS)


@pytest.mark.parametrize("mime", [None, "audio/mpeg", "application/x-onda-download", "application/x-onda-download;version=2"])
def test_plain_file_or_wrong_protocol_cannot_pass_the_gate(encoded_audio, mime):
    with pytest.raises(CureError, match="download_progress_not_supported"):
        verify_progress_download(encode(events(encoded_audio["mp3"])), {"Content-Type": mime})


@pytest.mark.parametrize("kind,limit", [(1, DOWNLOAD_PROGRESS_JSON_LIMIT), (2, DOWNLOAD_PROGRESS_BINARY_LIMIT)])
def test_declared_frames_are_bounded_before_payload_read(kind, limit):
    with pytest.raises(CureError, match="download_progress_invalid"):
        verify_progress_download(bytes([kind]) + struct.pack(">I", limit + 1), HEADERS)


@pytest.mark.parametrize("kind", [0, 3, 255])
def test_unknown_frame_kinds_are_rejected(kind):
    with pytest.raises(CureError, match="download_progress_invalid"):
        verify_progress_download(bytes([kind]) + struct.pack(">I", 1) + b"x", HEADERS)


@pytest.mark.parametrize("payload", [b"not json", b"\xff", b"null", b"[]", b"{" * 2000])
def test_invalid_metadata_is_controlled(payload):
    with pytest.raises(CureError, match="download_progress_invalid"):
        verify_progress_download(b"\x01" + struct.pack(">I", len(payload)) + payload, HEADERS)


@pytest.mark.parametrize("field,value", [
    ("downloadedBytes", -1), ("downloadedBytes", True), ("downloadedBytes", "100"),
    ("speedBytesPerSecond", float("nan")), ("processedSeconds", float("inf")),
    ("totalBytes", 2 ** 53), ("totalBytes", 10 ** 400), ("attempt", 4), ("attempt", 1.5),
])
def test_progress_counters_require_bounded_numbers(encoded_audio, field, value):
    values = events(encoded_audio["mp3"])
    values[2][field] = value
    with pytest.raises(CureError, match="download_progress_invalid"):
        verify_progress_download(encode(values), HEADERS)


@pytest.mark.parametrize("removed", [0, 2, 3, 4, 5, 8, 9])
def test_required_stages_file_and_complete_cannot_be_omitted(encoded_audio, removed):
    values = events(encoded_audio["mp3"])
    del values[removed]
    with pytest.raises(CureError, match="download_progress_"):
        verify_progress_download(encode(values), HEADERS)


@pytest.mark.parametrize("change", ["out_of_order", "duplicate_file", "binary_first", "binary_after_ready", "after_complete",
                                   "no_downloading_counter", "file_size", "footer_size", "ready_bytes", "recovery_missing"])
def test_false_success_and_incoherent_stream_order_fail(encoded_audio, change):
    values = events(encoded_audio["mp3"])
    if change == "out_of_order":
        values[2], values[3] = values[3], values[2]
    elif change == "duplicate_file":
        values.insert(5, deepcopy(values[4]))
    elif change == "binary_first":
        values.insert(0, b"data")
    elif change == "binary_after_ready":
        values.insert(9, b"data")
    elif change == "after_complete":
        values.append({"type": "heartbeat"})
    elif change == "no_downloading_counter":
        values[2].pop("downloadedBytes")
    elif change == "file_size":
        values[4]["size"] += 1
    elif change == "footer_size":
        values[9]["size"] += 1
    elif change == "ready_bytes":
        values[8]["downloadedBytes"] -= 1
    elif change == "recovery_missing":
        values[4]["recovery"].pop("queued")
    with pytest.raises(CureError, match="download_progress_"):
        verify_progress_download(encode(values), HEADERS)


def test_returned_junk_and_wrong_codec_cannot_be_success(encoded_audio):
    for content in (b"junk" * 100, encoded_audio["flac"]):
        with pytest.raises(CureError, match="download_progress_audio_invalid"):
            verify_progress_download(encode(events(content)), HEADERS)


def test_error_event_never_exposes_server_text_or_codes():
    content = frame({"type": "error", "code": "Cookie: secret", "error": "https://private.example/password"})
    with pytest.raises(CureError, match="^download_progress_remote_error$") as error:
        verify_progress_download(content, HEADERS)
    assert "secret" not in str(error.value) and "private" not in str(error.value)


class GateHTTP:
    def __init__(self, capability=None, error=None):
        self.capability, self.error, self.requests = capability, error, []

    def json(self, url, **_kwargs):
        if url.endswith("/api/health"):
            health = {"ok": True, "formats": ["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff"],
                      "videoFormats": ["mp4", "webm", "mkv", "mov"]}
            if self.capability is not None:
                health["downloadProgress"] = self.capability
            return health
        if url.endswith("/api/compatibility"):
            return {"versions": {"ytDlp": "2026.8.19", "deno": "2.9.7", "ffmpeg": "7.0.2"}}
        return {"status": "passed", "ok": True, "details": {"title": "Owned metadata fixture"}}

    def request(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if kwargs.get("method") == "HEAD":
            return b"", {"Content-Type": "video/mp4" if url.endswith(".mp4") else "audio/wav"}
        if kwargs.get("headers", {}).get("Accept") == PROTOCOL:
            if self.error:
                raise CureError(self.error)
            return encode(events(b"fixture" * 100)), HEADERS
        mime = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "flac": "audio/flac",
                "ogg": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac", "aiff": "audio/aiff",
                "mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "mov": "video/quicktime"}
        return b"fixture" * 100, {"Content-Type": mime[kwargs["payload"]["format"]]}


@pytest.fixture
def gated_checks(monkeypatch):
    monkeypatch.setattr(autocura, "verify_downloaded_audio", lambda *_args: {"audioCodec": "mp3", "decoded": True})
    monkeypatch.setattr(autocura, "verify_downloaded_video", lambda *_args, **_kwargs: {"decoded": True})

    def create(capability=None, error=None, **kwargs):
        http = GateHTTP(capability, error)
        return DeploymentChecks(http, canaries=[], **kwargs), http
    return create


def progress_requests(http):
    return [options for _, options in http.requests if options.get("headers", {}).get("Accept") == PROTOCOL]


def test_old_baseline_remains_compatible_without_an_extra_operation(gated_checks):
    checks, http = gated_checks()
    report = checks.verify(NEW)
    assert report["status"] == "passed"
    assert report["coverage"]["download_progress"] == "legacy_not_advertised"
    assert not progress_requests(http)


@pytest.mark.parametrize("capability", [None, {}, {"enabled": False}, {"enabled": True, "protocol": "version2"}])
def test_new_candidate_cannot_skip_progress_by_removing_capability(gated_checks, capability):
    checks, http = gated_checks(capability)
    report = checks.verify_candidate(NEW)
    assert report["status"] == "failed"
    progress = next(check for check in report["checks"] if check["name"] == "download_progress")
    assert progress["code"] == "download_progress_not_supported"
    assert not progress_requests(http)


def test_advertised_stream_runs_exactly_one_owned_mp3_probe(gated_checks):
    checks, http = gated_checks({"enabled": True, "protocol": PROTOCOL}, bypass="fixture_bypass",
                              audio_canary_url=CANONICAL_ORIGIN + "/canary.wav")
    report = checks.verify_candidate(NEW)
    assert report["status"] == "passed" and report["coverage"]["download_progress"] == "passed"
    requests = progress_requests(http)
    assert len(requests) == 1
    assert requests[0]["payload"] == {"url": CANONICAL_ORIGIN + "/canary.wav", "format": "mp3", "quality": 192}
    assert requests[0]["headers"] == {"Accept": PROTOCOL, "x-vercel-protection-bypass": "fixture_bypass"}
    assert requests[0]["timeout"] == 90 and "max_response" not in requests[0]


def test_probe_retains_existing_machine_credentials_without_expanding_canaries(gated_checks):
    class Credentials:
        def token(self):
            return "mt_fixture"
    checks, http = gated_checks({"enabled": True, "protocol": PROTOCOL}, machine_credentials=Credentials(),
                              project_id="prj_fixture", audio_canary_url=CANONICAL_ORIGIN + "/canary.wav")
    # Authentication itself has separate tests. Assert this probe uses the
    # existing verified machine-origin helper, with no new credential source.
    checks.authentication_check = lambda *_args: None
    report = checks.verify_candidate(NEW)
    assert report["status"] == "passed"
    assert progress_requests(http)[0]["headers"]["Authorization"] == "Bearer mt_fixture"
    assert "mt_fixture" not in json.dumps(report)


@pytest.mark.parametrize("url", ["https://example.com/canary.wav", "https://candidate.vercel.app/other.wav"])
def test_stream_probe_only_downloads_known_project_owned_source(gated_checks, url):
    checks, http = gated_checks({"enabled": True, "protocol": PROTOCOL}, audio_canary_url=url)
    report = checks.verify_candidate(NEW)
    progress = next(check for check in report["checks"] if check["name"] == "download_progress")
    assert progress["code"] == "download_progress_canary_scope" and progress["status"] == "failed"
    assert not progress_requests(http)


def test_arbitrary_http_error_cannot_enter_progress_journal(gated_checks):
    checks, _ = gated_checks({"enabled": True, "protocol": PROTOCOL}, error="Cookie: secret url https://private")
    report = checks.verify_candidate(NEW)
    progress = next(check for check in report["checks"] if check["name"] == "download_progress")
    assert progress["code"] == "download_progress_failed"
    assert "secret" not in json.dumps(report) and "https://private" not in json.dumps(report)


def test_response_bound_cannot_be_increased_by_wire_metadata():
    with pytest.raises(CureError, match="download_progress_invalid"):
        verify_progress_download(b"x" * (MAX_RESPONSE + 1), HEADERS)


@pytest.mark.parametrize("statuses,expected,promoted,rolled_back", [
    (["failed"], "failed", False, False),
    (["passed", "failed"], "rolled_back", True, True),
    (["passed", "passed"], "passed", True, False),
])
def test_release_and_post_promotion_use_mandatory_candidate_gate(tmp_path, statuses, expected, promoted, rolled_back):
    """The orchestration must not accidentally use legacy-compatible verify."""
    (tmp_path / "requirements.txt").write_text("reviewed fixture pins\n")
    (tmp_path / "public").mkdir()
    previous = {"id": "dpl_previous", "url": "https://previous.vercel.app"}

    class Provider:
        active = previous
        did_promote = did_rollback = False

        def current(self):
            return self.active

        def stage(self):
            return NEW

        def promote(self, deployment):
            self.did_promote, self.active = True, deployment

        def rollback(self, deployment):
            self.did_rollback, self.active = True, deployment

    class Dependencies:
        def latest(self):
            return {"yt-dlp": "fixture"}

        def prepare(self, _versions):
            pass

    class Checks:
        def __init__(self):
            self.calls = []
            self.statuses = iter(statuses)

        def verify(self, *_args, **_kwargs):
            raise AssertionError("Legacy verifier cannot gate a new publication")

        def verify_candidate(self, deployment, versions=None):
            self.calls.append(deployment["id"])
            status = next(self.statuses)
            return {"status": status, "checks": [{"name": "download_progress", "status": status}]}

    provider, checks = Provider(), Checks()
    manager = autocura.AutoCura(tmp_path, provider, Dependencies(), checks,
                               checkpoint=lambda *_args, **_kwargs: None)
    state = manager.release(force=True)
    assert state["status"] == expected
    assert provider.did_promote is promoted and provider.did_rollback is rolled_back
    assert checks.calls == [NEW["id"]] * len(statuses)
