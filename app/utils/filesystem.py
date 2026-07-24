from __future__ import annotations

from pathlib import Path


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_parent_directory(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_runtime_directory(path: Path) -> None:
    ensure_directory(path)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
