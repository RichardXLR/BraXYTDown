"""Request-order regression test for the browser's actual account transport."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_delayed_account_reads_cannot_undo_acknowledged_writes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable in this API-only environment")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/account_consistency.cjs")],
        cwd=root, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Account consistency: delayed reads" in result.stdout
