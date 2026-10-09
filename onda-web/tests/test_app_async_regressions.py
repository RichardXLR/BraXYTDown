"""Production browser handlers preserve account state across asynchronous actions."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("case", [
    "reset_persists",
    "validation_cancelled_buffer",
    "validation_current_response",
    "paste_preserves_newer_link",
    "paste_after_sign_out",
    "paste_current_action",
    "paste_preserves_link_edited_back_to_original",
    "paste_latest_request_wins_with_empty_clipboard",
    "paste_older_rejection_does_not_replace_latest_feedback",
    "compatibility_preserves_maintenance",
    "compatibility_failure_before_maintenance",
    "direct_url_scope",
    "trailing_dot_platform_scope",
    "validation_failure_updates_saved_summary",
    "validation_failure_metadata_after_signout",
    "restore_metadata_preserves_newer_edit",
])
def test_app_async_action_regression(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable in this API-only environment")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [node, str(root / "tests" / "app_async_regressions.cjs"), case],
        cwd=root, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "passed." in completed.stdout
