from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressSnapshot:
    current: int
    total: int | None
    speed_bytes_per_second: float | None = None
    eta_seconds: float | None = None


def format_progress(snapshot: ProgressSnapshot) -> str:
    percent = _format_percent(snapshot.current, snapshot.total)
    downloaded = format_bytes(snapshot.current)
    total = format_bytes(snapshot.total) if snapshot.total else "unknown"
    speed = (
        f"{format_bytes(int(snapshot.speed_bytes_per_second))}/s"
        if snapshot.speed_bytes_per_second
        else "calculating"
    )
    eta = format_duration(snapshot.eta_seconds) if snapshot.eta_seconds else "calculating"
    return (
        "Downloading\n\n"
        f"{percent}\n"
        f"{downloaded} / {total}\n"
        f"Speed: {speed}\n"
        f"ETA: {eta}"
    )


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value < 1024:
        return f"{value} B"
    units = ("KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        amount /= 1024
        if amount < 1024:
            return f"{amount:.1f} {unit}"
    return f"{amount:.1f} PB"


def format_duration(seconds: float | None) -> str:
    if seconds is None or math.isinf(seconds):
        return "unknown"
    rounded = max(0, int(seconds))
    minutes, sec = divmod(rounded, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def _format_percent(current: int, total: int | None) -> str:
    if not total:
        return "Progress: unknown"
    percent = min(100, max(0, int((current / total) * 100)))
    return f"{percent}%"
