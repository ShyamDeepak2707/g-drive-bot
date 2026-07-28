from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.database import DatabaseRepository, FileRecord
from app.download_queue import (
    CurrentDownloadSnapshot,
    DownloadQueue,
    DownloadQueueSnapshot,
)
from app.health import HealthService
from app.startup_recovery import StartupRecoverySummary
from app.upload_worker import CurrentUploadSnapshot, UploadWorker, UploadWorkerSnapshot


@dataclass(frozen=True)
class RuntimeStatistics:
    startup_time: datetime
    uptime_seconds: float
    telegram_bot_connected: bool
    pyrogram_connected: bool
    google_drive_authenticated: bool
    database_connected: bool
    pending_downloads: int
    active_downloads: int
    pending_uploads: int
    active_uploads: int
    startup_recovery: StartupRecoverySummary


@dataclass(frozen=True)
class QueueInspection:
    pending_downloads: int
    active_download: CurrentDownloadSnapshot | None
    pending_uploads: int
    active_upload: CurrentUploadSnapshot | None
    upload_worker_busy: bool


@dataclass(frozen=True)
class FailedJobSummary:
    file_record_id: int
    filename: str
    status: str
    file_type: str | None
    size: int | None
    download_status: str | None
    error_message: str | None
    upload_retry_count: int
    updated_at: str


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


class AdminService:
    def __init__(
        self,
        *,
        health_service: HealthService,
        repository: DatabaseRepository,
        download_queue: DownloadQueue | None,
        upload_worker: UploadWorker | None,
        temp_dir: Path,
        downloads_dir: Path,
    ) -> None:
        self._health_service = health_service
        self._repository = repository
        self._download_queue = download_queue
        self._upload_worker = upload_worker
        self._temp_dir = temp_dir
        self._downloads_dir = downloads_dir

    def runtime_statistics(self) -> RuntimeStatistics:
        health = self._health_service.snapshot()
        return RuntimeStatistics(
            startup_time=health.startup_time,
            uptime_seconds=health.uptime_seconds,
            telegram_bot_connected=health.telegram_bot_connected,
            pyrogram_connected=health.pyrogram_connected,
            google_drive_authenticated=health.google_drive_authenticated,
            database_connected=health.database_connected,
            pending_downloads=health.queues.pending_downloads,
            active_downloads=health.queues.active_downloads,
            pending_uploads=health.queues.pending_uploads,
            active_uploads=health.queues.active_uploads,
            startup_recovery=health.startup_recovery,
        )

    def queue_inspection(self) -> QueueInspection:
        health = self._health_service.snapshot()
        download_snapshot = self._download_snapshot()
        upload_snapshot = self._upload_snapshot()
        return QueueInspection(
            pending_downloads=health.queues.pending_downloads,
            active_download=(
                download_snapshot.current_download if download_snapshot is not None else None
            ),
            pending_uploads=health.queues.pending_uploads,
            active_upload=upload_snapshot.current_upload if upload_snapshot is not None else None,
            upload_worker_busy=upload_snapshot.is_busy if upload_snapshot is not None else False,
        )

    def failed_job_summaries(self, limit: int = 20) -> tuple[FailedJobSummary, ...]:
        records = self._repository.list_failed_file_records(limit=max(0, limit))
        return tuple(self._failed_job_summary(record) for record in records)

    def temporary_file_summaries(self) -> TemporaryFileSummary:
        files = tuple(sorted(self._temporary_file_items(), key=lambda item: str(item.path)))
        return TemporaryFileSummary(
            total_files=len(files),
            total_bytes=sum(item.size_bytes for item in files),
            files=files,
        )

    def _download_snapshot(self) -> DownloadQueueSnapshot | None:
        if self._download_queue is None:
            return None
        return self._download_queue.snapshot()

    def _upload_snapshot(self) -> UploadWorkerSnapshot | None:
        if self._upload_worker is None:
            return None
        return self._upload_worker.snapshot()

    def _failed_job_summary(self, file_record: FileRecord) -> FailedJobSummary:
        download = self._repository.get_download(file_record.id)
        return FailedJobSummary(
            file_record_id=file_record.id,
            filename=file_record.original_name
            or f"{file_record.file_type or 'file'}-{file_record.id}",
            status=file_record.status,
            file_type=file_record.file_type,
            size=file_record.size,
            download_status=download.status if download is not None else None,
            error_message=_job_error_message(
                file_record,
                download.error_message if download else None,
            ),
            upload_retry_count=file_record.upload_retry_count,
            updated_at=file_record.updated_at,
        )

    def _temporary_file_items(self) -> list[TemporaryFileItem]:
        items: dict[Path, TemporaryFileItem] = {}
        for path in self._filesystem_temporary_paths():
            _add_existing_temp_file(items, path, source="filesystem")
        for path in self._repository_temporary_paths():
            _add_existing_temp_file(items, path, source="repository")
        return list(items.values())

    def _filesystem_temporary_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for directory in (self._temp_dir, self._downloads_dir):
            if not directory.exists() or not directory.is_dir():
                continue
            for pattern in ("*.part", "*.part.temp", "*.temp"):
                paths.extend(path for path in directory.glob(pattern) if path.is_file())
        return tuple(paths)

    def _repository_temporary_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for download in self._repository.list_download_records_with_paths():
            if download.temp_path:
                temp_path = Path(download.temp_path)
                paths.extend((temp_path, Path(f"{temp_path}.temp")))
            if download.local_path:
                local_path = Path(download.local_path)
                paths.extend(
                    (
                        Path(f"{local_path}.part"),
                        Path(f"{local_path}.part.temp"),
                        Path(f"{local_path}.temp"),
                    )
                )
        return tuple(paths)


def _job_error_message(file_record: FileRecord, download_error: str | None) -> str | None:
    if file_record.upload_error_message:
        return file_record.upload_error_message
    return download_error


def _add_existing_temp_file(
    items: dict[Path, TemporaryFileItem],
    path: Path,
    *,
    source: str,
) -> None:
    if not path.exists() or not path.is_file():
        return
    resolved = path.resolve()
    if resolved in items:
        return
    items[resolved] = TemporaryFileItem(
        path=resolved,
        size_bytes=resolved.stat().st_size,
        source=source,
    )
