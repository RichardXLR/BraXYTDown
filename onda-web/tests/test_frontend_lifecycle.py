"""Browser event and streamed-download regressions using the production scripts."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("script", ["download_transport.cjs", "player_lifecycle.cjs"])
def test_frontend_operation_lifecycle(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable in this API-only environment")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [node, str(root / "tests" / script)],
        cwd=root, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "passed." in completed.stdout
