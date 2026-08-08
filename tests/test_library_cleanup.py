import os
import time
from pathlib import Path

from baixatube.library_cleanup import remove_candidates, scan_partial_files


def test_cleanup_only_removes_old_explicit_partials_inside_destination(tmp_path: Path):
    partial = tmp_path / "video.mp4.part"
    complete = tmp_path / "video.mp4"
    partial.write_bytes(b"partial")
    complete.write_bytes(b"complete")
    old = time.time() - 48 * 3600
    os.utime(partial, (old, old))

    candidates = scan_partial_files([tmp_path])
    assert [Path(item.path).name for item in candidates] == ["video.mp4.part"]

    removed, size = remove_candidates(candidates, [tmp_path])
    assert removed == 1 and size == len(b"partial")
    assert not partial.exists() and complete.exists()
