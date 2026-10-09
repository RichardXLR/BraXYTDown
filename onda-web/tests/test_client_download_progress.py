"""Run the actual incremental browser decoder with adversarial frame boundaries."""
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("case", ["split_every_byte", "several_frames_one_read", "missing_footer", "incomplete_binary",
    "incomplete_header", "duplicate_file", "duplicate_complete", "binary_before_metadata", "exceeds_declared_size",
    "exceeds_user_budget", "excessive_frame", "unknown_frame_kind", "footer_wrong_size", "rejects_invalid_progress",
    "propagated_source_error", "abort_pending_reader", "stale_operation_after_buffered_read", "no_ready_before_valid_eof", "all_server_mimes", "rejects_preconditions_and_closes_body"])
def test_browser_progress_protocol(case):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", str(root / "tests/download_progress.cjs"), case], cwd=root,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
