"""Regression checks for the actual browser source-session request helpers."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_pasted_browser_exports_and_platform_scope_use_production_helpers():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable in this API-only environment")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [node, str(root / "tests/source_session.cjs")],
        cwd=root, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "pasted-export privacy" in completed.stdout
