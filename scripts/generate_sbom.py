from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "dist" / "BraXYTDow-sbom.cdx.json"
    components: list[dict] = []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda item: (item.metadata.get("Name") or "").casefold()):
        name = distribution.metadata.get("Name")
        if name:
            components.append({"type": "library", "name": name, "version": distribution.version, "purl": f"pkg:pypi/{name}@{distribution.version}"})
    manifest_path = root / "bin" / "tools-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for tool, item in manifest.get("tools", {}).items():
            binary = root / "bin" / ("ffmpeg.exe" if tool == "ffmpeg" else f"{tool}.exe")
            if binary.is_file():
                components.append(
                    {
                        "type": "application",
                        "name": tool,
                        "version": str(item.get("version", "unknown")),
                        "hashes": [{"alg": "SHA-256", "content": sha256(binary)}],
                    }
                )
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{__import__('uuid').uuid4()}",
        "version": 1,
        "metadata": {"timestamp": datetime.now(timezone.utc).isoformat(), "component": {"type": "application", "name": "BraXYTDow"}},
        "components": components,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
