# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the self-contained, single-file Windows build."""

from pathlib import Path


root = Path(SPECPATH)
icon = root / "assets" / "baixatube.ico"
version_file = root / "installer" / "version_info.txt"

# Keep the tools as data instead of importing/linking them.  At runtime they
# are copied to a writable, versioned directory and launched as independent
# processes.  Missing tools are allowed by the spec so developers can still
# make a diagnostic build; scripts/build.ps1 requires them for release builds.
datas = []
for relative, destination in (
    ("bin/yt-dlp.exe", "bin"),
    ("bin/ffmpeg.exe", "bin"),
    ("bin/ffprobe.exe", "bin"),
    ("bin/deno.exe", "bin"),
    ("bin/tools-manifest.json", "bin"),
    ("assets/baixatube-icon.png", "assets"),
    ("assets/creator-richard.jpg", "assets"),
    ("assets/instagram.svg", "assets"),
    ("assets/ui-check.svg", "assets"),
    ("assets/ui-chevron.svg", "assets"),
    ("THIRD_PARTY_NOTICES.md", "."),
):
    source = root / relative
    if source.is_file():
        datas.append((str(source), destination))

a = Analysis(
    [str(root / "baixatube_launcher.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BraXYTDow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(icon) if icon.is_file() else None,
    version=str(version_file) if version_file.is_file() else None,
    uac_admin=False,
    uac_uiaccess=False,
)
