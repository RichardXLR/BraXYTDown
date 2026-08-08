from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

from .branding import APP_NAME, LEGACY_APP_NAME


_MANIFEST_LOCK = threading.RLock()
_ACTIVE_MANIFEST = "active.json"


def data_dir() -> Path:
    """Return the per-user, writable application data directory."""

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    target = base / APP_NAME
    legacy = base / LEGACY_APP_NAME
    # Existing installations keep using the former directory. This preserves
    # queue/history and hundreds of MB of verified tools without a risky move
    # while a download may still be active. Fresh installs use the new brand.
    if not target.exists() and legacy.is_dir():
        return legacy
    target.mkdir(parents=True, exist_ok=True)
    return target


def legacy_data_dir() -> Path:
    """Return the former application's data location without creating it."""

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / LEGACY_APP_NAME


def tools_dir() -> Path:
    """Directory containing user-managed yt-dlp/FFmpeg installations."""

    target = data_dir() / "tools"
    target.mkdir(parents=True, exist_ok=True)
    return target


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Return the directory used by PyInstaller for bundled data files."""

    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass).resolve() if meipass else application_dir()


def executable_name(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    return name if name.lower().endswith(suffix) and suffix else f"{name}{suffix}"


def bundled_binary(name: str) -> Path | None:
    """Find a binary shipped with the app, including PyInstaller onedir layouts."""

    filename = executable_name(name)
    candidates = (
        resource_dir() / "bin" / filename,
        application_dir() / "bin" / filename,
        application_dir() / "_internal" / "bin" / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _safe_component(value: str) -> str:
    result = "".join(character if character.isalnum() or character in ".-_" else "_" for character in value)
    return result.strip(". ")[:96] or "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temp_name)
        os.replace(temp_name, destination)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def active_manifest(root: Path | None = None) -> dict[str, Any]:
    root = root or tools_dir()
    path = root / _ACTIVE_MANIFEST
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"schema": 1, "tools": {}}
    if not isinstance(value, dict) or not isinstance(value.get("tools"), dict):
        return {"schema": 1, "tools": {}}
    return value


def activate_tool(tool: str, version: str, directory: Path, *, root: Path | None = None, **metadata: Any) -> None:
    """Atomically point a tool at a complete versioned installation."""

    root = (root or tools_dir()).resolve()
    resolved = directory.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("A ferramenta deve estar dentro da pasta gerenciada do usuário") from exc

    required_by_tool = {
        "yt-dlp": ("yt-dlp",),
        "ffmpeg": ("ffmpeg", "ffprobe"),
        "deno": ("deno",),
    }
    if tool not in required_by_tool:
        raise ValueError(f"Ferramenta desconhecida: {tool}")
    required = required_by_tool[tool]
    missing = [name for name in required if not (resolved / executable_name(name)).is_file()]
    if missing:
        raise FileNotFoundError(f"Instalação incompleta de {tool}: {', '.join(missing)}")

    with _MANIFEST_LOCK:
        manifest = active_manifest(root)
        manifest["schema"] = 1
        entry: dict[str, Any] = {
            "version": version,
            "directory": relative.as_posix(),
        }
        entry.update({key: value for key, value in metadata.items() if value is not None})
        manifest.setdefault("tools", {})[tool] = entry
        _atomic_json(root / _ACTIVE_MANIFEST, manifest)


def active_tool_entry(tool: str, root: Path | None = None) -> dict[str, Any] | None:
    entry = active_manifest(root).get("tools", {}).get(tool)
    return dict(entry) if isinstance(entry, dict) else None


def active_binary(name: str, root: Path | None = None) -> Path | None:
    groups = {"yt-dlp": "yt-dlp", "ffmpeg": "ffmpeg", "ffprobe": "ffmpeg", "deno": "deno"}
    tool = groups.get(name)
    if not tool:
        return None
    root = (root or tools_dir()).resolve()
    entry = active_tool_entry(tool, root)
    if not entry or not isinstance(entry.get("directory"), str):
        return None
    candidate = (root / entry["directory"] / executable_name(name)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def bootstrap_bundled_binaries(
    names: Iterable[str] = ("yt-dlp", "ffmpeg", "ffprobe", "deno"),
    *,
    root: Path | None = None,
) -> dict[str, Path]:
    """Copy packaged tools into LOCALAPPDATA without replacing an active version.

    Downloads always run from this writable, versioned area.  A digest in the
    directory name lets a newer application package coexist with running jobs.
    """

    root = (root or tools_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    requested = set(names)
    installed: dict[str, Path] = {}
    groups = []
    if "yt-dlp" in requested:
        groups.append(("yt-dlp", ("yt-dlp",)))
    if requested.intersection({"ffmpeg", "ffprobe"}):
        groups.append(("ffmpeg", ("ffmpeg", "ffprobe")))
    if "deno" in requested:
        groups.append(("deno", ("deno",)))

    for tool, members in groups:
        current = {member: active_binary(member, root) for member in members}
        if all(current.values()):
            installed.update({member: path for member, path in current.items() if path})
            continue

        sources = {member: bundled_binary(member) for member in members}
        if not all(sources.values()):
            continue
        combined = hashlib.sha256()
        for member in members:
            combined.update(_sha256(sources[member]).encode("ascii"))  # type: ignore[arg-type]
        digest = combined.hexdigest()
        destination = root / "versions" / tool / _safe_component(f"bundled-{digest[:16]}")
        for member, source in sources.items():
            target = destination / executable_name(member)
            if not target.is_file() or _sha256(target) != _sha256(source):  # type: ignore[arg-type]
                _copy_atomic(source, target)  # type: ignore[arg-type]
        activate_tool(tool, "bundled", destination, root=root, source="bundled", sha256=digest)
        installed.update({member: destination / executable_name(member) for member in members})
    return installed


def tool_path_entries(root: Path | None = None) -> list[str]:
    """Return active tool directories suitable for prepending to ``PATH``."""

    root = root or tools_dir()
    entries: list[str] = []
    for binary_name in ("deno", "ffmpeg", "yt-dlp"):
        binary = active_binary(binary_name, root)
        if binary:
            directory = str(binary.parent)
            if directory not in entries:
                entries.append(directory)
    return entries


def environment_with_tools(environment: dict[str, str] | None = None, root: Path | None = None) -> dict[str, str]:
    """Return an environment where yt-dlp can discover Deno and FFmpeg."""

    result = dict(os.environ if environment is None else environment)
    current_key = next((key for key in result if key.lower() == "path"), "PATH")
    existing = result.get(current_key, "")
    parts = tool_path_entries(root)
    if existing:
        parts.append(existing)
    result[current_key] = os.pathsep.join(parts)
    return result


def find_binary(name: str) -> str | None:
    managed = active_binary(name)
    if managed:
        return str(managed)

    try:
        managed = bootstrap_bundled_binaries((name,)).get(name)
    except OSError:
        # A read-only/misconfigured profile should not make the packaged app unusable.
        managed = None
    if managed:
        return str(managed)

    bundled = bundled_binary(name)
    if bundled:
        return str(bundled)
    return shutil.which(name)
