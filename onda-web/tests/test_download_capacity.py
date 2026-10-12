"""Real client policy: synthetic byte values, no large allocation or live account."""
from pathlib import Path
import shutil
import subprocess
import pytest

@pytest.mark.parametrize('case', [
    'quota_is_not_physical_disk_and_usage_is_subtracted',
    'lower_upper_range_and_reserve_classify_risk',
    'memory_fallback_prevents_false_quota_rejection',
    'unsupported_opfs_uses_existing_safe_memory_ceiling',
    'unknown_size_never_uses_source_bytes_as_final_output',
    'missing_rejected_or_malformed_quota_does_not_block',
    'zero_quota_and_usage_over_quota_are_real_no_disk_space',
    'slow_quota_has_bounded_wait_and_late_rejection_is_consumed',
    'large_outputs_use_quota_without_new_artificial_ceiling',
    'receiver_rechecks_exact_size_before_writing',
])
def test_early_storage_capacity(case):
    node = shutil.which('node')
    if not node: pytest.skip('Node is not installed in this API-only environment')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root/'tests/download_capacity.cjs'), case],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'passed.' in result.stdout
