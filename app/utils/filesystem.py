from __future__ import annotations

import logging
from pathlib import Path


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_parent_directory(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_runtime_directory(path: Path, logger: logging.Logger | None = None) -> None:
    directory = ensure_directory(path)
    for pattern in ("*.part", "*.part.temp"):
        for candidate in directory.glob(pattern):
            if not candidate.is_file():
                continue
            try:
                candidate.unlink()
            except OSError as exc:
                if logger is not None:
                    logger.warning(
                        "failed to remove stale temporary file",
                        extra={
                            "event": "runtime_temp_cleanup_failed",
                            "path": str(candidate),
                            "error": str(exc),
                        },
                    )
                continue
            if logger is not None:
                logger.info(
                    "removed stale temporary file",
                    extra={"event": "runtime_temp_cleanup", "path": str(candidate)},
                )


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
