from __future__ import annotations

from pathlib import Path

import baixatube.paths as paths


def test_bootstrap_copies_bundled_tools_to_managed_version_directory(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    sources = {}
    for name in ("yt-dlp", "ffmpeg", "ffprobe"):
        source = bundle / paths.executable_name(name)
        source.write_bytes(f"MZ-{name}".encode())
        sources[name] = source
    managed_root = tmp_path / "managed"
    monkeypatch.setattr(paths, "bundled_binary", lambda name: sources.get(name))

    installed = paths.bootstrap_bundled_binaries(root=managed_root)

    assert set(installed) == {"yt-dlp", "ffmpeg", "ffprobe"}
    assert all(path.is_relative_to(managed_root) for path in installed.values())
    assert paths.active_binary("yt-dlp", managed_root).read_bytes() == b"MZ-yt-dlp"
    assert paths.active_binary("ffprobe", managed_root).read_bytes() == b"MZ-ffprobe"


def test_active_manifest_rejects_directory_outside_managed_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / paths.executable_name("yt-dlp")).write_bytes(b"MZ")

    try:
        paths.activate_tool("yt-dlp", "test", outside, root=tmp_path / "managed")
    except ValueError as exc:
        assert "pasta gerenciada" in str(exc)
    else:
        raise AssertionError("activate_tool accepted a path outside its managed root")


def test_environment_prepends_active_deno_and_ffmpeg_directories(tmp_path):
    for tool, members in {"deno": ("deno",), "ffmpeg": ("ffmpeg", "ffprobe")}.items():
        directory = tmp_path / "versions" / tool / "test"
        directory.mkdir(parents=True)
        for member in members:
            (directory / paths.executable_name(member)).write_bytes(b"MZ")
        paths.activate_tool(tool, "test", directory, root=tmp_path)

    environment = paths.environment_with_tools({"PATH": "C:\\Windows"}, tmp_path)

    assert str(paths.active_binary("deno", tmp_path).parent) in environment["PATH"]
    assert environment["PATH"].endswith("C:\\Windows")
