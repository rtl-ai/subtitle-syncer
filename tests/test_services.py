from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.services import cleanup_old_jobs, normalize_encoding_label


@pytest.mark.parametrize(
    "label,expected",
    [
        ("UTF-8\n", "utf-8"),
        ("cp949", "uhc"),
        ("  windows-949  ", "uhc"),
        ("unknown", None),
        (None, None),
    ],
)
def test_normalize_encoding_label(label, expected):
    assert normalize_encoding_label(label) == expected


def test_cleanup_old_jobs(tmp_path: Path):
    base = tmp_path / "jobs"
    base.mkdir()
    fresh = base / "fresh"
    fresh.mkdir()
    old = base / "old"
    old.mkdir()

    old_file = old / "file.txt"
    old_file.write_text("data")

    # Ensure the "old" directory has an older modification time.
    past = time.time() - 7200
    os.utime(old, (past, past))

    removed = cleanup_old_jobs(base, ttl_seconds=3600)
    assert old in removed
    assert not old.exists()
    assert fresh.exists()
