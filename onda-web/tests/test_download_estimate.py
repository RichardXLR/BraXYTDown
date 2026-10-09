"""Estimates derive ranges from reported source data and selected output settings."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("case", [
    "audio_bitrate_and_overhead",
    "duration_and_trim",
    "unknown_duration_is_not_a_file_size",
    "pcm_and_flac_are_distinct",
    "video_uses_available_rate_not_invented_quality",
    "video_separate_audio_mute_and_trim",
    "lower_source_is_never_upscaled",
    "comparisons_only_use_reported_video_resolutions",
    "limit_warnings_are_about_estimates",
    "dense_untrusted_metadata_is_bounded",
    "byte_format_is_honest_and_localized",
    "render_unknown_ready_and_logout",
])
def test_download_estimate(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable in this API-only environment")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [node, str(root / "tests" / "download_estimate.cjs"), case],
        cwd=root, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "passed." in completed.stdout
