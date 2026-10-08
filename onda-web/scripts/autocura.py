"""AutoCura 3.0: verified dependencies and immutable, gated Vercel releases.

Run only in the administrator's CI, never in a request or on a user's device.
No dependency is updated inside a running deployment. An online block is never
reported as a successful extraction and does not condemn a package version.
The explicit ``baseline`` policy can retain an identical, pre-existing YouTube
platform block while still requiring every converter and application check.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import venv

from packaging.version import InvalidVersion, Version

TOOLS = ("yt-dlp", "imageio-ffmpeg", "deno")
DIRECT_COMPONENTS = (*TOOLS, "fastapi", "uvicorn", "publicsuffixlist")
CANARIES = (
    {"name": "me_at_zoo_metadata", "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
     "purpose": "metadata_only"},
    {"name": "big_buck_bunny_metadata", "url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
     "purpose": "metadata_only", "license": "CC BY 3.0; Blender Foundation"},
)
MAX_RESPONSE = 8 * 1024 * 1024
MAX_PACKAGE_METADATA_RESPONSE = 32 * 1024 * 1024


class CureError(RuntimeError):
    """A safe error code; never include raw HTTP or subprocess output."""
    def __init__(self, code, *, http_status=None):
        super().__init__(code)
        self.http_status = http_status


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path, fallback):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError):
        raise CureError("invalid_persisted_state") from None
    if not isinstance(value, dict):
        raise CureError("invalid_persisted_state")
    return value


@contextmanager
def process_lock(path: Path):
    """OS-owned lock is released even after a process crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise CureError("update_already_running") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CureError("update_already_running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class HTTP:
    """HTTPS with platform TLS trust, inherited proxy settings and bounded reads."""
    def request(self, url, *, method="GET", payload=None, headers=None, timeout=40,
                max_response=MAX_RESPONSE):
        if (isinstance(max_response, bool) or not isinstance(max_response, int)
                or not 1 <= max_response <= MAX_PACKAGE_METADATA_RESPONSE):
            raise CureError("invalid_response_limit")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise CureError("unsafe_update_url")
        request_headers = {"User-Agent": "Onda-AutoCura/3.0", **(headers or {})}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
        class SameHostRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, new_url):
                target = urllib.parse.urlsplit(new_url)
                if target.scheme != "https" or target.hostname != parsed.hostname:
                    raise CureError("unexpected_http_redirect")
                return super().redirect_request(request, fp, code, message, headers, new_url)

        try:
            opener = urllib.request.build_opener(SameHostRedirect())
            with opener.open(request, timeout=timeout) as response:
                final = urllib.parse.urlsplit(response.geturl())
                # Credentials must never cross a redirect to another host.
                if final.scheme != "https" or final.hostname != parsed.hostname:
                    raise CureError("unexpected_http_redirect")
                content = response.read(max_response + 1)
                if len(content) > max_response:
                    raise CureError("response_too_large")
                return content, response.headers
        except urllib.error.HTTPError as exc:
            # API error codes are useful for distinguishing platform blocks from
            # a broken converter. Never propagate raw error bodies or cookies.
            try:
                body = json.loads(exc.read(MAX_RESPONSE))
                code = body.get("code", "")
                if not code and isinstance(body.get("error"), dict):
                    code = body["error"].get("code", "")
            except (ValueError, AttributeError, OSError):
                code = ""
            if re.fullmatch(r"[a-z_]{1,80}", str(code)):
                raise CureError(code, http_status=exc.code) from None
            raise CureError(f"http_{exc.code}", http_status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CureError("network_unavailable") from None

    def json(self, url, **kwargs):
        content, _ = self.request(url, **kwargs)
        try:
            return json.loads(content)
        except (ValueError, UnicodeError):
            raise CureError("invalid_server_response") from None


def subprocess_environment(*, credentials=False):
    environment = dict(os.environ)
    if not credentials:
        for key in list(environment):
            if "TOKEN" in key.upper() or "SECRET" in key.upper() or key.startswith("VERCEL_"):
                environment.pop(key, None)
    # Ignore alternate package sources, preserving HTTPS proxy and CA env vars.
    for key in list(environment):
        if key.startswith("PIP_") and key not in {"PIP_CERT", "PIP_CLIENT_CERT"}:
            environment.pop(key, None)
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["PIP_INDEX_URL"] = "https://pypi.org/simple"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def run(command, *, directory, timeout=600, credentials=False, error_code="candidate_command_failed"):
    try:
        completed = subprocess.run(command, cwd=directory, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout,
                                   env=subprocess_environment(credentials=credentials), check=False)
    except subprocess.TimeoutExpired:
        raise CureError("timeout") from None
    except (OSError, subprocess.SubprocessError):
        raise CureError("command_unavailable") from None
    if completed.returncode:
        # Classify internally, but do not return installer/build output: it can
        # contain proxy credentials or pulled environment values.
        detail = (completed.stdout + completed.stderr).lower()
        if any(marker in detail for marker in ("connection refused", "connection reset", "timed out",
                "temporary failure", "name resolution", "certificate_verify_failed", "certificate verify failed",
                "proxyerror", "network is unreachable", "could not resolve host")):
            raise CureError("network_unavailable")
        raise CureError(error_code)
    return completed.stdout.strip()


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


class DependencyCandidate:
    def __init__(self, root: Path, http=None):
        self.root = root
        self.http = http or HTTP()
        self.resolved_versions = None

    def pinned_components(self):
        """Read reviewed pins, including transitives in a generated hash lock."""
        pins = {}
        for line in (self.root / "requirements.txt").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("--hash=") or line == "--require-hashes":
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)(\[[A-Za-z0-9_,.-]+\])?==([^\s\\;]+)(?:\s+--hash=sha256:[a-f0-9]{64})?", line)
            if not match:
                raise CureError("requirements_must_be_pinned")
            name = normalize(match[1])
            if name in pins or len(pins) >= 80:
                raise CureError("invalid_component_configuration")
            pins[name] = {"version": match[3], "extra": match[2] or ""}
        if not set(TOOLS).issubset(pins):
            raise CureError("tool_pin_missing")
        return pins

    def latest(self):
        self.resolved_versions = None
        parents = {}
        pins = self.pinned_components()
        if not set(DIRECT_COMPONENTS).issubset(pins):
            raise CureError("direct_component_pin_missing")
        # Update reviewed parents, then let pip resolve compatible transitives.
        # Independently pinning every latest child can conflict with an exact
        # parent requirement (for example pydantic/pydantic-core).
        for name in DIRECT_COMPONENTS:
            # PyPI includes the full release history here, which can exceed
            # the ordinary 8 MiB budget as projects accumulate releases.
            # Only this trusted metadata request receives the larger bound;
            # deployment APIs, version-specific wheel hashes and canaries keep
            # their smaller default limit.
            metadata = self.http.json(f"https://pypi.org/pypi/{name}/json",
                                      max_response=MAX_PACKAGE_METADATA_RESPONSE)
            version = str(metadata["info"]["version"])
            if not re.fullmatch(r"[0-9][0-9A-Za-z.+!_-]{0,80}", version):
                raise CureError("invalid_package_version")
            try:
                parsed = Version(version)
            except InvalidVersion:
                raise CureError("invalid_package_version") from None
            if parsed.is_prerelease or parsed.is_devrelease:
                raise CureError("prerelease_component_not_allowed")
            parents[name] = version
        with tempfile.TemporaryDirectory(prefix="onda-resolution-") as temporary:
            report_path = Path(temporary) / "report.json"
            run([sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
                 "--only-binary=:all:", "--index-url", "https://pypi.org/simple",
                 "--report", str(report_path), *self.specifications(parents)],
                directory=self.root, error_code="dependency_resolution_failed")
            try:
                if report_path.stat().st_size > MAX_RESPONSE:
                    raise CureError("dependency_resolution_report_too_large")
                report = json.loads(report_path.read_bytes())
            except (OSError, ValueError):
                raise CureError("invalid_dependency_resolution") from None
        items = report.get("install") if isinstance(report, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= 80:
            raise CureError("invalid_dependency_resolution")
        versions = {}
        for item in items:
            metadata = item.get("metadata") if isinstance(item, dict) else None
            if not isinstance(metadata, dict):
                raise CureError("invalid_dependency_resolution")
            name, version = metadata.get("name"), metadata.get("version")
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name)
                    or not isinstance(version, str) or not re.fullmatch(r"[0-9][0-9A-Za-z.+!_-]{0,80}", version)):
                raise CureError("invalid_dependency_resolution")
            name = normalize(name)
            try:
                parsed = Version(version)
            except InvalidVersion:
                raise CureError("invalid_dependency_resolution") from None
            if name in versions or parsed.is_prerelease or parsed.is_devrelease:
                raise CureError("invalid_dependency_resolution")
            versions[name] = version
        if any(name not in versions or not same_version(versions[name], version)
               for name, version in parents.items()):
            raise CureError("resolved_parent_version_mismatch")
        self.resolved_versions = dict(versions)
        return dict(versions)

    def specifications(self, versions):
        """Apply only explicitly selected versions to reviewed components."""
        pins = self.pinned_components()
        if set(versions) - set(pins) and versions != self.resolved_versions:
            raise CureError("unreviewed_component_update")
        pins["yt-dlp"]["extra"] = "[default]"
        return [f'{name}{item["extra"]}=={versions.get(name, item["version"])}'
                for name, item in sorted(pins.items()) if name in DIRECT_COMPONENTS]

    def verify_wheels(self, folder: Path):
        """Verify every transitive wheel before installing or executing it."""
        manifest = []
        files = sorted(folder.iterdir())
        if not files or any(path.suffix != ".whl" or not path.is_file() for path in files):
            raise CureError("only_binary_wheels_are_allowed")
        for wheel in files:
            parts = wheel.name[:-4].split("-")
            if len(parts) not in (5, 6):
                raise CureError("invalid_wheel_filename")
            name, version = normalize(parts[0]), parts[1]
            metadata = self.http.json(f"https://pypi.org/pypi/{name}/{version}/json")
            artifact = next((item for item in metadata.get("urls", []) if item.get("filename") == wheel.name), None)
            if not artifact or artifact.get("yanked") or artifact.get("packagetype") != "bdist_wheel":
                raise CureError("wheel_not_in_official_release")
            origin = urllib.parse.urlsplit(artifact.get("url", ""))
            if origin.scheme != "https" or origin.hostname != "files.pythonhosted.org":
                raise CureError("untrusted_wheel_origin")
            expected = artifact.get("digests", {}).get("sha256", "")
            if not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise CureError("wheel_digest_missing")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            if digest != expected:
                raise CureError("wheel_integrity_failed")
            manifest.append({"name": name, "version": version, "filename": wheel.name, "sha256": digest})
        if len({item["name"] for item in manifest}) != len(manifest):
            raise CureError("duplicate_wheel_distribution")
        return manifest

    def prepare(self, versions):
        specifications = self.specifications(versions)
        temporary = Path(tempfile.mkdtemp(prefix="onda-candidate-"))
        wheels = temporary / "wheels"
        wheels.mkdir()
        try:
            run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-cache-dir",
                 "--index-url", "https://pypi.org/simple", "--dest", str(wheels), *specifications],
                directory=self.root, error_code="dependency_download_failed")
            artifacts = self.verify_wheels(wheels)
            if self.resolved_versions is not None:
                downloaded = {item["name"]: item["version"] for item in artifacts}
                if (versions != self.resolved_versions or set(downloaded) != set(versions)
                        or any(not same_version(downloaded[name], version) for name, version in versions.items())):
                    raise CureError("dependency_resolution_changed")
            lock = "# AutoCura: every direct and transitive wheel is pinned and verified against PyPI.\n"
            lock += "--require-hashes\n"
            for item in artifacts:
                extra = "[default]" if item["name"] == "yt-dlp" else ""
                lock += f'{item["name"]}{extra}=={item["version"]} --hash=sha256:{item["sha256"]}\n'
            lock_file = temporary / "requirements.txt"
            lock_file.write_text(lock)
            environment = temporary / "venv"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheels),
                 "--require-hashes", "--no-deps", "-r", str(lock_file)], directory=self.root,
                error_code="verified_dependency_install_failed")
            self.verify_runtime(python, versions)
            self.verify_application(python, artifacts, temporary)
            (self.root / "requirements.txt").write_text(lock)
            atomic_json(self.root / ".autocura" / "artifacts.json", {"created_at": now(), "wheels": artifacts})
            return {"versions": versions, "artifacts": artifacts}
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def verify_application(self, python: Path, production_artifacts, temporary: Path):
        """Run the whole API/security suite against the actual candidate packages."""
        test_wheels = temporary / "test-wheels"
        test_wheels.mkdir()
        constraints = temporary / "production-constraints.txt"
        constraints.write_text("".join(f'{item["name"]}=={item["version"]}\n' for item in production_artifacts))
        run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-cache-dir",
             "--index-url", "https://pypi.org/simple", "--constraint", str(constraints),
             "--dest", str(test_wheels), "pytest==8.4.2", "httpx==0.28.1"], directory=self.root,
            error_code="test_dependency_download_failed")
        test_artifacts = self.verify_wheels(test_wheels)
        lock_file = temporary / "test-requirements.txt"
        lock_file.write_text("--require-hashes\n" + "".join(
            f'{item["name"]}=={item["version"]} --hash=sha256:{item["sha256"]}\n' for item in test_artifacts))
        run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(test_wheels),
             "--require-hashes", "--no-deps", "-r", str(lock_file)], directory=self.root,
            error_code="verified_test_dependency_install_failed")
        run([str(python), "-m", "pytest", "-q"], directory=self.root, timeout=300,
            error_code="candidate_application_tests_failed")

    def verify_runtime(self, python: Path, versions):
        # Test actual codecs and JS execution, not simply the existence of files.
        probe = r'''
import importlib.metadata, json, subprocess, tempfile
from pathlib import Path
import imageio_ffmpeg, deno, yt_dlp
expected = json.loads(__import__('sys').argv[1])
for name, version in expected.items():
    assert importlib.metadata.version(name) == version
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
subprocess.run([str(deno.find_deno_bin()), 'eval', 'if(2+2!==4)Deno.exit(1)'], check=True, capture_output=True, timeout=20)
codecs = {'mp3':'libmp3lame','m4a':'aac','wav':'pcm_s16le','flac':'flac','ogg':'libvorbis','opus':'libopus','aac':'aac','aiff':'pcm_s16be'}
with tempfile.TemporaryDirectory() as directory:
    for extension, codec in codecs.items():
        output = Path(directory) / ('tone.' + extension)
        args = [ffmpeg,'-hide_banner','-nostdin','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=0.15','-c:a',codec]
        if extension == 'opus': args += ['-ar','48000']
        args += [str(output)]
        subprocess.run(args, check=True, capture_output=True, timeout=20)
        assert output.stat().st_size > 32
    # An owned synthetic clip exercises the video codecs independently of
    # network extraction. Source privacy tags must disappear after editing.
    source = Path(directory) / 'tagged.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-nostdin','-loglevel','error','-f','lavfi',
        '-i','color=c=blue:s=160x90:r=15:d=1','-f','lavfi','-i','sine=frequency=440:duration=1',
        '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-metadata','title=ONDA_CANARY_PRIVATE_TITLE',
        '-metadata','comment=ONDA_CANARY_PRIVATE_COMMENT','-metadata:s:v','handler_name=ONDA_CANARY_PRIVATE_HANDLER',
        '-metadata:s:a','language=por',str(source)],check=True,capture_output=True,timeout=20)
    import re
    for extension in ('mp4','webm','mkv','mov'):
        output = Path(directory) / ('edited.' + extension)
        video_codec, audio_codec = ('libvpx-vp9','libopus') if extension == 'webm' else ('libx264','aac')
        args = [ffmpeg,'-hide_banner','-nostdin','-loglevel','error','-i',str(source),'-ss','0.2','-t','0.6',
            '-map','0:v:0','-map','0:a:0','-map_metadata','-1','-map_metadata:s','-1','-map_chapters','-1',
            '-c:v',video_codec,'-pix_fmt','yuv420p','-c:a',audio_codec]
        if extension == 'webm': args += ['-deadline','realtime','-cpu-used','8','-ar','48000']
        args += [str(output)]
        subprocess.run(args,check=True,capture_output=True,timeout=25)
        decoded = subprocess.run([ffmpeg,'-hide_banner','-nostdin','-i',str(output),'-f','null','-'],
            check=True,capture_output=True,text=True,timeout=20).stderr
        assert re.search(r'Video: ' + ('vp9' if extension == 'webm' else 'h264') + r'\b',decoded)
        assert re.search(r'Audio: ' + ('opus' if extension == 'webm' else 'aac') + r'\b',decoded)
        assert 'ONDA_CANARY_PRIVATE' not in decoded and '(por)' not in decoded
        assert output.stat().st_size > 100
    version = subprocess.check_output([ffmpeg,'-version'],text=True,timeout=10).splitlines()[0]
print(json.dumps({'ffmpeg':version,'yt-dlp':yt_dlp.version.__version__,'deno':expected['deno'],
    'audioFormats':list(codecs),'videoFormats':['mp4','webm','mkv','mov']}))
'''
        return json.loads(run([str(python), "-c", probe, json.dumps(versions)], directory=self.root,
                              timeout=200, error_code="toolchain_functional_failed"))


def classify(code):
    if code in {"network_unavailable", "upstream_timeout", "upstream_error", "platform_blocked", "busy", "unavailable", "timeout"} or code.startswith("http_4"):
        return "blocked"
    return "failed"


def deployment_url(value):
    if not value.startswith("https://"):
        value = "https://" + value
    parts = urllib.parse.urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or not parts.hostname.endswith(".vercel.app")
            or parts.username or parts.password or parts.port or parts.query or parts.fragment
            or parts.path not in ("", "/")):
        raise CureError("invalid_deployment_url")
    return value.rstrip("/")


def verify_downloaded_audio(content, audio_format):
    """Decode the response independently; MIME and byte count are insufficient."""
    import imageio_ffmpeg

    codecs = {"mp3": "mp3", "m4a": "aac", "wav": "pcm_s16le", "flac": "flac",
              "ogg": "vorbis", "opus": "opus", "aac": "aac", "aiff": "pcm_s16be"}
    if audio_format not in codecs or not 100 <= len(content) <= MAX_RESPONSE:
        raise CureError("audio_conversion_failed")
    with tempfile.TemporaryDirectory(prefix="onda-audio-probe-") as temporary:
        source = Path(temporary) / ("response." + audio_format)
        source.write_bytes(content)
        try:
            decoded = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin",
                "-protocol_whitelist", "file,pipe", "-i", str(source), "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                timeout=20, env=subprocess_environment())
        except (OSError, subprocess.SubprocessError):
            raise CureError("audio_inspection_failed") from None
        if decoded.returncode:
            raise CureError("audio_decode_failed")
        input_details = decoded.stderr.split("Output #", 1)[0]
        audio = re.search(r"^  Stream #0:\d+[^\n]*Audio:\s*(\w+)", input_details, re.M)
        video = re.search(r"^  Stream #0:\d+[^\n]*Video:", input_details, re.M)
        if not audio or audio[1] != codecs[audio_format] or video:
            raise CureError("audio_codec_failed")
        return {"audioCodec": audio[1], "decoded": True}


def verify_downloaded_video(content, *, muted=False, expected_duration=.6, video_format="mp4", normalized=False):
    """Decode the actual returned bytes and check codecs, trim and privacy tags.

    FFprobe is not included in imageio-ffmpeg's wheel; bounded FFmpeg inspection
    reports container and stream tags without requesting an external resource.
    Technical tags generated by muxers are allowed, rather than claiming that
    every file/container field can be removed.
    """
    import imageio_ffmpeg

    if video_format not in {"mp4", "webm", "mkv", "mov"} or len(content) < 100 or len(content) > MAX_RESPONSE:
        raise CureError("video_conversion_failed")
    with tempfile.TemporaryDirectory(prefix="onda-video-probe-") as temporary:
        source = Path(temporary) / ("response." + video_format)
        source.write_bytes(content)
        try:
            decoded = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin",
                "-protocol_whitelist", "file,pipe", "-i", str(source), "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                timeout=20, env=subprocess_environment())
        except (OSError, subprocess.SubprocessError):
            raise CureError("video_inspection_failed") from None
        if decoded.returncode:
            raise CureError("video_decode_failed")
        details = decoded.stderr.split("Output #", 1)[0]
        # FourCCs contain hex values such as 0x31637661; these are not dimensions.
        video = re.search(r"^  Stream #0:\d+[^\n]*Video:\s*(\w+)[^\n]*?(?:,|\s)(\d{2,5})x(\d{2,5})(?:[,\s]|$)", details, re.M)
        audio = re.search(r"^  Stream #0:\d+[^\n]*Audio:\s*(\w+)", details, re.M)
        duration = re.search(r"^  Duration:\s*(\d+):(\d+):([\d.]+)", details, re.M)
        video_codec, audio_codec = ("vp9", "opus") if video_format == "webm" else ("h264", "aac")
        if not video or video[1] != video_codec or int(video[3]) > 480:
            raise CureError("video_codec_or_resolution_failed")
        if (muted and audio) or (not muted and (not audio or audio[1] != audio_codec)):
            raise CureError("video_audio_edit_failed")
        if not duration:
            raise CureError("video_duration_unavailable")
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        if abs(seconds - expected_duration) > .16:
            raise CureError("video_trim_failed")
        if "ONDA_CANARY_PRIVATE" in details:
            raise CureError("video_private_metadata_retained")
        technical = {"major_brand", "minor_version", "compatible_brands", "encoder", "vendor_id", "duration"}
        for key, value in re.findall(r"^[ \t]{4,}([A-Za-z0-9_.-]+)[ \t]*:[ \t]*(.+)$", details, re.M):
            key, value = key.lower(), value.strip()
            if key in technical:
                continue
            if key == "handler_name" and value in {"VideoHandler", "SoundHandler", "DataHandler"}:
                continue
            if key == "language" and value == "und":
                continue
            raise CureError("video_private_metadata_retained")
        languages = re.findall(r"Stream #\d+:\d+(?:\[[^\]]+\])?\(([^)]+)\)", details)
        if any(language != "und" for language in languages):
            raise CureError("video_private_metadata_retained")
        inspection = {"videoCodec": video[1], "audioCodec": audio[1] if audio else None,
                      "duration": seconds, "height": int(video[3]), "privacy": "custom_tags_removed"}
        if normalized:
            try:
                measured = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin",
                    "-protocol_whitelist", "file,pipe", "-i", str(source), "-vn",
                    "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True,
                    text=True, encoding="utf-8", errors="replace", check=False, timeout=20,
                    env=subprocess_environment())
            except (OSError, subprocess.SubprocessError):
                raise CureError("video_loudness_inspection_failed") from None
            loudness = re.search(r"Integrated loudness:\s*I:\s*(-?[\d.]+)\s*LUFS", measured.stderr)
            if measured.returncode or not loudness or abs(float(loudness[1]) + 16) > 1.0:
                raise CureError("video_normalization_failed")
            inspection["integratedLoudness"] = float(loudness[1])
        return inspection


class DeploymentChecks:
    def __init__(self, http=None, canaries=None, bypass=None, audio_canary_url=None, youtube_audio_canary_url=None, video_canary_url=None, youtube_policy=None):
        self.http = http or HTTP()
        self.canaries = list(CANARIES if canaries is None else canaries)
        self.bypass = bypass or os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET")
        self.audio_canary_url = audio_canary_url or os.environ.get("AUTOCURA_AUDIO_CANARY_URL")
        self.youtube_audio_canary_url = youtube_audio_canary_url or os.environ.get("AUTOCURA_YOUTUBE_AUDIO_CANARY_URL")
        self.video_canary_url = video_canary_url or os.environ.get("AUTOCURA_VIDEO_CANARY_URL")
        self.youtube_policy = youtube_policy or os.environ.get("AUTOCURA_YOUTUBE_POLICY", "strict")
        if self.youtube_policy not in {"strict", "baseline"}:
            raise CureError("invalid_youtube_gate_policy")
        self.platform_baseline = None

    @staticmethod
    def youtube_canary(canary):
        return urllib.parse.urlsplit(canary["url"]).hostname in {"youtube.com", "www.youtube.com", "youtu.be"}

    def metadata_check(self, base, canary, headers):
        name = canary["name"]
        try:
            tested = self.http.json(base + "/api/compatibility/test", method="POST",
                payload={"url": canary["url"]}, headers=headers, timeout=65)
            details = tested.get("details", {})
            if tested.get("status") != "passed" or tested.get("ok") is not True or not details.get("title"):
                raise CureError(str(tested.get("code", "invalid_extraction_result")))
            return {"name": name, "status": "passed", "purpose": "metadata_only"}
        except CureError as exc:
            return {"name": name, "status": classify(str(exc)), "code": str(exc)}

    def capture_platform_baseline(self, deployment):
        """Compare the old deployment before changing packages or production.

        Only the precise platform_blocked result may be retained. A network
        error, missing deployment, changed result or extraction regression can
        never become permission to ignore a candidate failure.
        """
        if self.youtube_policy != "baseline":
            return None
        base = deployment_url(deployment["url"])
        headers = {"x-vercel-protection-bypass": self.bypass} if self.bypass else {}
        self.platform_baseline = {
            "deployment_id": deployment["id"], "checked_at": now(),
            "checks": [self.metadata_check(base, canary, headers)
                       for canary in self.canaries if self.youtube_canary(canary)],
        }
        return self.platform_baseline

    def verify(self, deployment, versions=None):
        base = deployment_url(deployment["url"])
        headers = {"x-vercel-protection-bypass": self.bypass} if self.bypass else {}
        checks = []
        try:
            health = self.http.json(base + "/api/health", headers=headers)
            if health.get("ok") is not True or set(health.get("formats", [])) != {"mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff"}:
                raise CureError("runtime_health_failed")
            if set(health.get("videoFormats", [])) != {"mp4", "webm", "mkv", "mov"}:
                raise CureError("video_runtime_health_failed")
            checks.append({"name": "runtime", "status": "passed"})
        except CureError as exc:
            checks.append({"name": "runtime", "status": classify(str(exc)), "code": str(exc)})
        try:
            compatibility = self.http.json(base + "/api/compatibility", headers=headers)
            actual = compatibility.get("versions", {})
            if (actual.get("ffmpeg") in {None, "indisponível"} or actual.get("deno") in {None, "indisponível"}
                    or (versions and (not same_version(actual.get("ytDlp"), versions["yt-dlp"])
                                      or not same_version(actual.get("deno"), versions["deno"])) )):
                raise CureError("packaged_runtime_version_mismatch")
            checks.append({"name": "packaged_versions", "status": "passed", "versions": actual})
        except CureError as exc:
            checks.append({"name": "packaged_versions", "status": classify(str(exc)), "code": str(exc)})
        for canary in self.canaries:
            checks.append(self.metadata_check(base, canary, headers))
        video_url = self.video_canary_url or base + "/canary.mp4"
        parsed_video = urllib.parse.urlsplit(video_url)
        video_status = None
        if (parsed_video.scheme != "https" or not parsed_video.hostname or parsed_video.username
                or parsed_video.password or parsed_video.query or parsed_video.fragment):
            video_status = "invalid_video_canary_configuration"
        else:
            try:
                _, source_headers = self.http.request(video_url, method="HEAD", timeout=30)
                if not source_headers.get("Content-Type", "").split(";")[0].startswith("video/"):
                    video_status = "video_canary_not_public"
            except CureError as exc:
                video_status = str(exc)
        for video_format, muted in (("mp4", False), ("mp4", True), ("webm", False), ("mkv", False), ("mov", False)):
            name = "owned_clip_mp4_muted" if muted else f"owned_clip_{video_format}_edited"
            if video_status:
                status = "blocked" if video_status == "video_canary_not_public" else classify(video_status)
                checks.append({"name": name, "status": status, "code": video_status})
                continue
            try:
                normalized = video_format == "mp4" and not muted
                video, response_headers = self.http.request(base + "/api/download", method="POST",
                    payload={"url": video_url, "media_type": "video", "format": video_format, "video_resolution": "480",
                             "trim_start": .2, "trim_end": .8, "strip_metadata": True, "mute": muted,
                             "normalize_audio": normalized}, headers=headers, timeout=90)
                expected = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "mov": "video/quicktime"}[video_format]
                if response_headers.get("Content-Type", "").split(";")[0] != expected:
                    raise CureError("video_conversion_failed")
                inspection = verify_downloaded_video(video, muted=muted, video_format=video_format, normalized=normalized)
                checks.append({"name": name, "status": "passed", "scope": "edited_video", "inspection": inspection})
            except CureError as exc:
                checks.append({"name": name, "status": classify(str(exc)), "code": str(exc)})
        # This tone is generated by this project. No third-party audio is downloaded.
        audio_url = self.audio_canary_url or base + "/canary.wav"
        parsed_audio = urllib.parse.urlsplit(audio_url)
        source_status = None
        if (parsed_audio.scheme != "https" or not parsed_audio.hostname or parsed_audio.username
                or parsed_audio.password or parsed_audio.query or parsed_audio.fragment):
            source_status = "invalid_audio_canary_configuration"
        else:
            try:
                # The function downloads this URL without CI's protection-bypass
                # header. Check that exact access path before testing conversion.
                _, source_headers = self.http.request(audio_url, method="HEAD", timeout=30)
                if not source_headers.get("Content-Type", "").split(";")[0].startswith(("audio/", "video/")):
                    source_status = "audio_canary_not_public"
            except CureError as exc:
                source_status = str(exc)
        for audio_format in ("mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff"):
            name = f"owned_tone_{audio_format}"
            if source_status:
                status = "blocked" if source_status == "audio_canary_not_public" else classify(source_status)
                checks.append({"name": name, "status": status, "code": source_status})
                continue
            try:
                audio, response_headers = self.http.request(base + "/api/download", method="POST",
                    payload={"url": audio_url, "format": audio_format, "quality": 192}, headers=headers, timeout=90)
                media_type = response_headers.get("Content-Type", "").split(";")[0]
                expected = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "flac": "audio/flac",
                            "ogg": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac", "aiff": "audio/aiff"}[audio_format]
                if len(audio) < 100 or media_type != expected:
                    raise CureError("audio_conversion_failed")
                inspection = verify_downloaded_audio(audio, audio_format)
                checks.append({"name": name, "status": "passed", "inspection": inspection})
            except CureError as exc:
                checks.append({"name": name, "status": classify(str(exc)), "code": str(exc)})
        if self.youtube_audio_canary_url:
            try:
                # The administrator must select a short video they own or have
                # permission to download. This stronger gate is opt-in.
                youtube = urllib.parse.urlsplit(self.youtube_audio_canary_url)
                if (youtube.scheme != "https" or youtube.hostname not in {"youtube.com", "www.youtube.com", "youtu.be"}
                        or youtube.username or youtube.password or youtube.fragment):
                    raise CureError("invalid_youtube_audio_canary_configuration")
                audio, audio_headers = self.http.request(base + "/api/download", method="POST",
                    payload={"url": self.youtube_audio_canary_url, "format": "mp3", "quality": 128}, headers=headers, timeout=180)
                if len(audio) < 100 or audio_headers.get("Content-Type", "").split(";")[0] != "audio/mpeg":
                    raise CureError("youtube_audio_canary_failed")
                checks.append({"name": "authorized_youtube_audio", "status": "passed", "scope": "audio"})
            except CureError as exc:
                checks.append({"name": "authorized_youtube_audio", "status": classify(str(exc)), "code": str(exc)})
        baseline_checks = {check["name"]: check for check in (self.platform_baseline or {}).get("checks", [])}
        youtube_names = {canary["name"] for canary in self.canaries if self.youtube_canary(canary)}
        accepted = []
        for check in checks:
            before = baseline_checks.get(check["name"], {})
            if (self.youtube_policy == "baseline" and check["name"] in youtube_names
                    and check["status"] == before.get("status") == "blocked"
                    and check.get("code") == before.get("code") == "platform_blocked"):
                accepted.append(check["name"])
        # Accepted checks stay visibly blocked. This is a regression gate, not
        # a claim that YouTube extraction worked or that the block was removed.
        required = [check for check in checks if check["name"] not in accepted]
        result = "passed" if all(check["status"] == "passed" for check in required) else "failed" if any(check["status"] == "failed" for check in required) else "blocked"
        return {"status": result, "checked_at": now(), "checks": checks,
                "release_gate": {"status": result, "youtube_policy": self.youtube_policy,
                                 "accepted_existing_blocks": accepted,
                                 "baseline_deployment_id": (self.platform_baseline or {}).get("deployment_id"),
                                 "youtube_verified": bool(youtube_names) and all(check["status"] == "passed" for check in checks if check["name"] in youtube_names or check["name"] == "authorized_youtube_audio")},
                "platform_baseline": self.platform_baseline,
                "coverage": {"youtube": "metadata_and_audio" if self.youtube_audio_canary_url else "metadata_only",
                             "youtube_status": "not_tested" if not youtube_names else "passed" if all(check["status"] == "passed" for check in checks if check["name"] in youtube_names or check["name"] == "authorized_youtube_audio") else "blocked" if accepted else result,
                             "audio_conversion": "project_owned_tone", "video_conversion": "project_owned_clip"}}


def same_version(first, second):
    try:
        return Version(str(first)) == Version(str(second))
    except InvalidVersion:
        return False


class Vercel:
    def __init__(self, root: Path, http=None):
        self.root = root
        self.http = http or HTTP()
        self.token = os.environ.get("VERCEL_TOKEN", "")
        self.project = os.environ.get("VERCEL_PROJECT_ID", "")
        self.team = os.environ.get("VERCEL_ORG_ID") or os.environ.get("VERCEL_TEAM_ID", "")
        self.production_alias = urllib.parse.urlsplit(deployment_url(
            os.environ.get("AUTOCURA_PRODUCTION_URL", "https://onda-audio.vercel.app"))).hostname
        if not self.token or not self.project or not self.team:
            raise CureError("vercel_credentials_not_configured")

    def api(self, path, *, payload=None, method="GET"):
        url = "https://api.vercel.com" + path + "?" + urllib.parse.urlencode({"teamId": self.team})
        return self.http.json(url, method=method, payload=payload,
                              headers={"Authorization": "Bearer " + self.token})

    def inspect(self, deployment_id):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", deployment_id):
            raise CureError("invalid_deployment_identifier")
        value = self.api("/v13/deployments/" + deployment_id)
        if value.get("projectId") != self.project or value.get("readyState") != "READY":
            raise CureError("deployment_not_ready_for_this_project")
        return {"id": value["id"], "url": deployment_url(value["url"]), "readyState": "READY"}

    def current(self):
        project = self.api("/v9/projects/" + urllib.parse.quote(self.project, safe=""))
        if project.get("id") != self.project:
            raise CureError("wrong_vercel_project")
        # A latest production build is not proof that the user's domain moved.
        alias = self.api("/v4/aliases/" + urllib.parse.quote(self.production_alias, safe=""))
        if alias.get("projectId") != self.project:
            raise CureError("wrong_production_alias_project")
        target = alias.get("deploymentId") or (alias.get("deployment") or {}).get("id")
        if not target:
            return None
        return self.inspect(target)

    def stage(self):
        # Build a separate preview from the reviewed hash-locked sources.
        # Only explicit promotion can move the production domain after tests.
        run(["vercel", "pull", "--yes", "--environment=preview"], directory=self.root, credentials=True,
            error_code="vercel_pull_failed")
        output = run(["vercel", "deploy", "--yes"],
                     directory=self.root, timeout=900, credentials=True, error_code="vercel_deploy_failed")
        url = deployment_url(output.splitlines()[-1])
        # Look up the deployment URL; IDs and ownership are validated before any mutation.
        return self.inspect(urllib.parse.urlsplit(url).hostname)

    def promote(self, deployment):
        ready = self.inspect(deployment["id"])
        try:
            self.api(f'/v2/deployments/{ready["id"]}/aliases', payload={"alias": self.production_alias}, method="POST")
        except CureError as error:
            current = self.current() if error.http_status in {409, 422} else None
            if not current or current["id"] != ready["id"]:
                raise
        self.wait_current(ready["id"])

    def rollback(self, deployment):
        ready = self.inspect(deployment["id"])
        try:
            self.api(f'/v2/deployments/{ready["id"]}/aliases', payload={"alias": self.production_alias}, method="POST")
        except CureError as error:
            current = self.current() if error.http_status in {409, 422} else None
            if not current or current["id"] != ready["id"]:
                raise
        self.wait_current(ready["id"])

    def production_view(self, deployment):
        current = self.current()
        if not current or current["id"] != deployment["id"]:
            raise CureError("production_mapping_not_confirmed")
        return {**current, "url": "https://" + self.production_alias}

    def wait_current(self, expected_id):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = self.current()
            if current and current["id"] == expected_id:
                return
            time.sleep(2)
        raise CureError("production_mapping_not_confirmed")


class RepositoryCheckpoint:
    """Commit the recovery journal before changing production, without force pushes."""
    def __init__(self, root: Path, http=None):
        self.root = root
        self.http = http or HTTP()
        self.repository = os.environ.get("GITHUB_REPOSITORY", "")
        self.branch = os.environ.get("GITHUB_REF_NAME", "main")
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.expected_head = os.environ.get("GITHUB_SHA", "")
        prefix = os.environ.get("AUTOCURA_REPOSITORY_SUBDIRECTORY", "").strip()
        self.prefix = PurePosixPath(prefix)
        if (prefix and (self.prefix.is_absolute() or ".." in self.prefix.parts
                or self.prefix.as_posix() != prefix or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                                                          for part in self.prefix.parts)
                or any(part in {".", ".git", ".github"} for part in self.prefix.parts))):
            raise CureError("unsafe_repository_subdirectory")
        workspace = os.environ.get("GITHUB_WORKSPACE")
        if workspace and self.root.resolve() != (Path(workspace).resolve() / prefix).resolve():
            raise CureError("repository_subdirectory_mismatch")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) or not self.token or self.branch != "main":
            raise CureError("durable_repository_checkpoint_not_configured")

    def api(self, path, *, method="GET", payload=None):
        return self.http.json("https://api.github.com/repos/" + self.repository + path,
                              headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json"},
                              method=method, payload=payload)

    def __call__(self, state, *, include_requirements=False):
        reference = self.api("/git/ref/heads/main")
        parent = reference["object"]["sha"]
        if self.expected_head and parent != self.expected_head:
            raise CureError("reviewed_repository_commit_changed")
        commit = self.api("/git/commits/" + parent)
        paths = [".autocura/state.json", "public/autocura.json"]
        if include_requirements:
            paths += ["requirements.txt", ".autocura/artifacts.json"]
        tree = []
        for path in paths:
            content = (self.root / path).read_text()
            blob = self.api("/git/blobs", method="POST", payload={"content": content, "encoding": "utf-8"})
            # Preserve the desktop project and every other repository file.
            tree.append({"path": (self.prefix / path).as_posix(), "mode": "100644", "type": "blob", "sha": blob["sha"]})
        created_tree = self.api("/git/trees", method="POST", payload={"base_tree": commit["tree"]["sha"], "tree": tree})
        created = self.api("/git/commits", method="POST", payload={"message": "AutoCura: " + state["status"] + " [skip ci]",
                       "tree": created_tree["sha"], "parents": [parent]})
        # A concurrent commit makes this fail instead of overwriting reviewed source.
        self.api("/git/refs/heads/main", method="PATCH", payload={"sha": created["sha"], "force": False})
        self.expected_head = created["sha"]


class AutoCura:
    def __init__(self, root: Path, provider, dependencies=None, checks=None, checkpoint=None):
        self.root = root
        self.provider = provider
        self.dependencies = dependencies or DependencyCandidate(root)
        configuration = read_json(root / ".autocura" / "canaries.json", {"canaries": list(CANARIES)})
        canaries = configuration.get("canaries")
        if not isinstance(canaries, list) or not 1 <= len(canaries) <= 10:
            raise CureError("invalid_canary_configuration")
        for canary in canaries:
            if not isinstance(canary, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", canary.get("name", "")):
                raise CureError("invalid_canary_configuration")
            parsed = urllib.parse.urlsplit(canary.get("url", ""))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise CureError("invalid_canary_configuration")
        self.checks = checks or DeploymentChecks(canaries=canaries)
        self.checkpoint = checkpoint
        self.path = root / ".autocura" / "state.json"
        self.state = read_json(self.path, {"schema": 2, "status": "unconfigured", "quarantine": [], "history": []})

    def save(self, *, durable=False, include_requirements=False):
        self.state["updated_at"] = now()
        atomic_json(self.path, self.state)
        report = {"schema": 3, "name": "AutoCura 3.0", "automation_enabled": bool(self.checkpoint),
                  "mode": "deployment", "automated": bool(self.checkpoint), "state": self.state["status"],
                  "status": self.state["status"], "snapshot_at": now(), "snapshot_only": True,
                  "versions": self.state.get("versions", {}), "last_check": self.state.get("last_check"),
                  "quarantine": self.state.get("quarantine", []), "history": self.state.get("history", [])[-20:],
                  "canaries": getattr(self.checks, "canaries", list(CANARIES)), "strategy": "immutable_deployment",
                  "youtube_policy": getattr(self.checks, "youtube_policy", "strict"),
                  "baseline_platform_check": self.state.get("baseline_platform_check"),
                  "schedule": "23 5 * * *",
                  "components": "all_pinned_python_dependencies",
                  "execution": {"repository": os.environ.get("GITHUB_REPOSITORY", ""),
                                "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                                "workflow": os.environ.get("GITHUB_WORKFLOW", "")},
                  "message": "Todas as conversões e regressões devem passar. No modo baseline, um bloqueio idêntico do YouTube já presente na versão anterior continua indicado como bloqueado e não impede atualizações dos componentes."}
        atomic_json(self.root / "public" / "autocura.json", report)
        if durable:
            if self.checkpoint is None:
                raise CureError("durable_checkpoint_required_before_promotion")
            self.checkpoint(self.state, include_requirements=include_requirements)

    def quarantine(self, versions, reason, deployment=None):
        self.state.setdefault("quarantine", []).append({"versions": versions, "reason": reason,
                                                        "created_at": now(), "deployment_id": (deployment or {}).get("id")})

    def recover(self):
        pending = self.state.get("pending")
        if not pending:
            return False
        previous = pending.get("previous")
        if not previous:
            self.state["status"] = "recovery_requires_baseline"
            self.save(durable=True)
            raise CureError("missing_known_good_baseline")
        # A prior recovery may already have restored the known good domain.
        # Vercel can reject a redundant rollback, so check the actual alias first.
        current = self.provider.current()
        if not current or current["id"] != previous["id"]:
            self.provider.rollback(previous)
        self.quarantine(pending.get("versions", {}), "unconfirmed_interrupted_promotion", pending.get("candidate"))
        self.state.pop("pending", None)
        self.state["status"] = "rolled_back"
        self.state["active"] = previous
        self.state["versions"] = pending.get("previous_versions", {})
        self.save(durable=True)
        return True

    def release(self, *, force=False):
        with process_lock(self.root / ".autocura" / "update.lock"):
            self.recover()
            original = (self.root / "requirements.txt").read_bytes()
            committed = False
            versions = {}
            previous_versions = dict(self.state.get("versions", {}))
            candidate = None
            try:
                versions = self.dependencies.latest()
                if not force and any(item.get("versions") == versions for item in self.state.get("quarantine", [])):
                    self.state["status"] = "quarantined"
                    self.save(durable=True)
                    return self.state
                if not force and versions == self.state.get("versions") and self.state.get("active"):
                    current = self.provider.current()
                    if current is None:
                        raise CureError("missing_known_good_baseline")
                    self.capture_baseline(current)
                    self.state["last_check"] = self.checks.verify(current, versions=versions)
                    self.state["status"] = self.state["last_check"]["status"]
                    self.save(durable=True)
                    return self.state
                previous = self.provider.current()
                if previous is None:
                    raise CureError("missing_known_good_baseline")
                self.capture_baseline(previous)
                self.dependencies.prepare(versions)
                self.state["status"] = "candidate_verification_pending"
                self.save()
                candidate = self.provider.stage()
                report = self.checks.verify(candidate, versions=versions)
                self.state["last_check"] = report
                if report["status"] != "passed":
                    self.state["status"] = report["status"]
                    if report["status"] == "failed":
                        self.quarantine(versions, "candidate_compatibility_failed", candidate)
                    self.save(durable=True)
                    return self.state
                self.state["pending"] = {"candidate": candidate, "previous": previous, "versions": versions,
                                         "previous_versions": previous_versions, "created_at": now(),
                                         "platform_baseline": self.state.get("baseline_platform_check")}
                self.state["status"] = "promotion_pending"
                # This journal is committed to GitHub before assigning domains.
                self.save(durable=True)
                try:
                    self.provider.promote(candidate)
                    production_view = getattr(self.provider, "production_view", None)
                    post = self.checks.verify(production_view(candidate) if production_view else candidate, versions=versions)
                    self.state["last_check"] = post
                    if post["status"] != "passed":
                        raise CureError("post_promotion_check_failed")
                    self.state["active"] = candidate
                    self.state["previous"] = previous
                    self.state["previous_versions"] = previous_versions
                    self.state["versions"] = versions
                    self.state["status"] = "passed"
                    self.state.pop("last_error", None)
                    self.state["quarantine"] = [item for item in self.state.get("quarantine", []) if item.get("versions") != versions]
                    self.state.pop("pending", None)
                    self.state.setdefault("history", []).append({"event": "promoted", "at": now(), "versions": versions, "deployment_id": candidate["id"]})
                    self.save(durable=True, include_requirements=True)
                    committed = True
                except Exception:
                    # Keep the durable pending journal if rollback itself fails.
                    self.provider.rollback(previous)
                    if self.state.get("last_check", {}).get("status") == "failed":
                        self.quarantine(versions, "post_promotion_compatibility_failed", candidate)
                    self.state.pop("pending", None)
                    self.state["active"] = previous
                    self.state["versions"] = previous_versions
                    self.state["status"] = "rolled_back"
                    self.state.setdefault("history", []).append({"event": "rolled_back", "at": now(), "deployment_id": previous["id"]})
                    self.save(durable=True)
                return self.state
            except CureError as exc:
                self.state["status"] = classify(str(exc))
                self.state["last_error"] = str(exc)
                if str(exc) in {"wheel_integrity_failed", "verified_dependency_install_failed",
                                "toolchain_functional_failed", "candidate_application_tests_failed",
                                "wheel_not_in_official_release", "dependency_download_failed",
                                "verified_test_dependency_install_failed"}:
                    self.quarantine(versions, str(exc), candidate)
                self.save(durable=bool(self.checkpoint))
                return self.state
            finally:
                if not committed:
                    (self.root / "requirements.txt").write_bytes(original)

    def capture_baseline(self, deployment):
        capture = getattr(self.checks, "capture_platform_baseline", None)
        if capture:
            baseline = capture(deployment)
            if baseline is not None:
                self.state["baseline_platform_check"] = baseline

    def test(self):
        """Persist manual verification so the service can show its real result."""
        with process_lock(self.root / ".autocura" / "update.lock"):
            self.recover()
            current = self.provider.current()
            if current is None:
                raise CureError("missing_known_good_baseline")
            self.capture_baseline(current)
            self.state["last_check"] = self.checks.verify(current)
            self.state["status"] = self.state["last_check"]["status"]
            self.state.setdefault("history", []).append({"event": "verified", "at": now(),
                                                         "deployment_id": current["id"]})
            self.save(durable=bool(self.checkpoint))
            return self.state

    def rollback(self):
        with process_lock(self.root / ".autocura" / "update.lock"):
            self.recover()
            previous = self.state.get("previous")
            if not previous:
                raise CureError("missing_previous_deployment")
            current = self.provider.current()
            current_versions = self.state.get("versions", {})
            self.provider.rollback(previous)
            self.state["active"], self.state["previous"] = previous, current
            self.state["versions"], self.state["previous_versions"] = self.state.get("previous_versions", {}), current_versions
            self.state["status"] = "rolled_back"
            self.save(durable=True)
            return self.state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("release", "rollback", "test"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--force", action="store_true", help="Manually retest quarantined versions.")
    arguments = parser.parse_args()
    try:
        root = arguments.root.resolve()
        provider = Vercel(root)
        checkpoint = RepositoryCheckpoint(root) if arguments.command != "test" or os.environ.get("GITHUB_TOKEN") else None
        manager = AutoCura(root, provider, checkpoint=checkpoint)
        if arguments.command == "release":
            result = manager.release(force=arguments.force)
        elif arguments.command == "rollback":
            result = manager.rollback()
        else:
            result = manager.test()
        print(json.dumps({"status": result["status"], "updated_at": result.get("updated_at")}, ensure_ascii=False))
        return 0 if result["status"] in {"passed", "rolled_back"} else 1
    except CureError as exc:
        print(json.dumps({"status": "disabled" if "not_configured" in str(exc) else "failed", "code": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
