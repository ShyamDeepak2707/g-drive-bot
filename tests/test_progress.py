from __future__ import annotations

from app.progress import ProgressSnapshot, format_bytes, format_duration, format_progress


def test_format_progress() -> None:
    formatted = format_progress(
        ProgressSnapshot(
            current=45 * 1024 * 1024,
            total=56 * 1024 * 1024,
            speed_bytes_per_second=5 * 1024 * 1024,
            eta_seconds=3,
        )
    )

    assert "80%" in formatted
    assert "45.0 MB / 56.0 MB" in formatted
    assert "Speed: 5.0 MB/s" in formatted
    assert "ETA: 3s" in formatted


def test_format_helpers() -> None:
    assert format_bytes(1024) == "1.0 KB"
    assert format_duration(65) == "1m 5s"
