from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TEMP_FILE_PATTERNS = ("*.part", "*.part.temp", "*.temp")


@dataclass(frozen=True)
class TemporaryFileItem:
    path: Path
    size_bytes: int
    source: str


@dataclass(frozen=True)
class TemporaryFileSummary:
    total_files: int
    total_bytes: int
    files: tuple[TemporaryFileItem, ...]


@dataclass(frozen=True)
class TempCleanupSummary:
    confirmed: bool
    total_files: int
    total_bytes: int
    deleted_files: int
    deleted_bytes: int
    files: tuple[TemporaryFileItem, ...]


class TempFileService:
    def __init__(self, *, temp_dir: Path, downloads_dir: Path) -> None:
        self._temp_dir = temp_dir
        self._downloads_dir = downloads_dir

    def temporary_file_summary(
        self,
        *,
        repository_paths: tuple[Path, ...] = (),
    ) -> TemporaryFileSummary:
        items: dict[Path, TemporaryFileItem] = {}
        for path in self._filesystem_temporary_paths():
            _add_existing_temp_file(items, path, source="filesystem")
        for path in repository_paths:
            _add_existing_temp_file(items, path, source="repository")
        files = tuple(sorted(items.values(), key=lambda item: str(item.path)))
        return TemporaryFileSummary(
            total_files=len(files),
            total_bytes=sum(item.size_bytes for item in files),
            files=files,
        )

    def preview_orphaned_temp_files(
        self,
        *,
        protected_paths: tuple[Path, ...] = (),
    ) -> TempCleanupSummary:
        files = self._orphaned_files(protected_paths)
        return TempCleanupSummary(
            confirmed=False,
            total_files=len(files),
            total_bytes=sum(item.size_bytes for item in files),
            deleted_files=0,
            deleted_bytes=0,
            files=files,
        )

    def cleanup_orphaned_temp_files(
        self,
        *,
        protected_paths: tuple[Path, ...] = (),
    ) -> TempCleanupSummary:
        files = self._orphaned_files(protected_paths)
        deleted_files = 0
        deleted_bytes = 0
        for item in files:
            try:
                item.path.unlink()
            except FileNotFoundError:
                deleted_files += 1
                deleted_bytes += item.size_bytes
                continue
            deleted_files += 1
            deleted_bytes += item.size_bytes
        return TempCleanupSummary(
            confirmed=True,
            total_files=len(files),
            total_bytes=sum(item.size_bytes for item in files),
            deleted_files=deleted_files,
            deleted_bytes=deleted_bytes,
            files=files,
        )

    def _orphaned_files(self, protected_paths: tuple[Path, ...]) -> tuple[TemporaryFileItem, ...]:
        protected = {_resolve_path(path) for path in protected_paths}
        items: dict[Path, TemporaryFileItem] = {}
        for path in self._filesystem_temporary_paths():
            resolved = _resolve_path(path)
            if resolved in protected:
                continue
            _add_existing_temp_file(items, resolved, source="filesystem")
        return tuple(sorted(items.values(), key=lambda item: str(item.path)))

    def _filesystem_temporary_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for directory in (self._temp_dir, self._downloads_dir):
            if not directory.exists() or not directory.is_dir():
                continue
            for pattern in TEMP_FILE_PATTERNS:
                paths.extend(path for path in directory.glob(pattern) if path.is_file())
        return tuple(paths)


def _add_existing_temp_file(
    items: dict[Path, TemporaryFileItem],
    path: Path,
    *,
    source: str,
) -> None:
    if not path.exists() or not path.is_file():
        return
    resolved = _resolve_path(path)
    if resolved in items:
        return
    items[resolved] = TemporaryFileItem(
        path=resolved,
        size_bytes=resolved.stat().st_size,
        source=source,
    )


def _resolve_path(path: Path) -> Path:
    return path.resolve()
