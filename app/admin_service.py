from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import constants
from app.database import DatabaseRepository, FileRecord
from app.download_queue import (
    CurrentDownloadSnapshot,
    DownloadQueue,
    DownloadQueueSnapshot,
)
from app.health import HealthService
from app.job_state import InvalidJobStateTransition, JobState
from app.startup_recovery import StartupRecoverySummary
from app.temp_files import (
    TempCleanupSummary,
    TempFileService,
    TemporaryFileSummary,
)
from app.temp_files import (
    TemporaryFileItem as TemporaryFileItem,
)
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
    total_tracked_files: int
    completed_downloads: int
    completed_uploads: int
    failed_downloads: int
    failed_uploads: int
    download_retry_attempts: int
    upload_retry_attempts: int
    scheduled_upload_retries: int


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
class RetryFailedSummary:
    eligible_jobs: int
    requeued_jobs: int
    skipped_jobs: int


@dataclass(frozen=True)
class CancelJobSummary:
    job_id: int
    job_type: str | None
    filename: str | None
    status: str
    message: str
    cancelled: bool


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
        temp_file_service: TempFileService | None = None,
    ) -> None:
        self._health_service = health_service
        self._repository = repository
        self._download_queue = download_queue
        self._upload_worker = upload_worker
        self._temp_file_service = temp_file_service or TempFileService(
            temp_dir=temp_dir,
            downloads_dir=downloads_dir,
        )

    def runtime_statistics(self) -> RuntimeStatistics:
        health = self._health_service.snapshot()
        repository_statistics = self._repository.runtime_statistics()
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
            total_tracked_files=repository_statistics.total_tracked_files,
            completed_downloads=repository_statistics.completed_downloads,
            completed_uploads=repository_statistics.completed_uploads,
            failed_downloads=repository_statistics.failed_downloads,
            failed_uploads=repository_statistics.failed_uploads,
            download_retry_attempts=repository_statistics.download_retry_attempts,
            upload_retry_attempts=repository_statistics.upload_retry_attempts,
            scheduled_upload_retries=repository_statistics.scheduled_upload_retries,
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

    def retry_failed_jobs(self, limit: int = 100) -> RetryFailedSummary:
        eligible_jobs = 0
        requeued_jobs = 0
        skipped_jobs = 0
        upload_requeued = False

        for file_record in self._repository.list_failed_file_records(limit=max(0, limit)):
            if self._retry_failed_download(file_record):
                eligible_jobs += 1
                requeued_jobs += 1
                continue
            if _is_failed_download(file_record, self._repository):
                skipped_jobs += 1
                continue

            if _is_retry_eligible_failed_upload(file_record):
                eligible_jobs += 1
                if self._retry_failed_upload(file_record):
                    requeued_jobs += 1
                    upload_requeued = True
                else:
                    skipped_jobs += 1
                continue

            skipped_jobs += 1

        if upload_requeued and self._upload_worker is not None:
            self._upload_worker.start()

        return RetryFailedSummary(
            eligible_jobs=eligible_jobs,
            requeued_jobs=requeued_jobs,
            skipped_jobs=skipped_jobs,
        )

    def temporary_file_summaries(self) -> TemporaryFileSummary:
        return self._temp_file_service.temporary_file_summary(
            repository_paths=self._repository_temporary_paths(),
        )

    def cleanup_temp(self, *, confirm: bool = False) -> TempCleanupSummary:
        protected_paths = self._repository_protected_paths()
        if confirm:
            return self._temp_file_service.cleanup_orphaned_temp_files(
                protected_paths=protected_paths,
            )
        return self._temp_file_service.preview_orphaned_temp_files(
            protected_paths=protected_paths,
        )

    def cancel_job(self, job_id: int, job_type: str | None = None) -> CancelJobSummary:
        normalized_job_type = job_type.casefold() if job_type is not None else None
        if normalized_job_type not in {None, "download", "upload"}:
            return CancelJobSummary(
                job_id=job_id,
                job_type=None,
                filename=None,
                status="Not Found",
                message="Job not found.",
                cancelled=False,
            )

        file_record = self._repository.get_file_record(job_id)
        if file_record is None:
            return CancelJobSummary(
                job_id=job_id,
                job_type=normalized_job_type,
                filename=None,
                status="Not Found",
                message="Job not found.",
                cancelled=False,
            )

        download = self._repository.get_download(job_id)
        if normalized_job_type in {None, "download"} and download is not None:
            if download.status in {
                constants.DOWNLOAD_STATUS_QUEUED,
                constants.DOWNLOAD_STATUS_RUNNING,
            }:
                cancelled = self._cancel_download(file_record)
                return CancelJobSummary(
                    job_id=job_id,
                    job_type="download",
                    filename=_file_record_filename(file_record),
                    status="Cancelled" if cancelled else "Not Cancelled",
                    message="Cancelled" if cancelled else "Unable to cancel download.",
                    cancelled=cancelled,
                )
            if normalized_job_type == "download":
                return _non_cancellable_summary(
                    job_id=job_id,
                    job_type="download",
                    filename=_file_record_filename(file_record),
                    status=download.status,
                )

        if normalized_job_type in {None, "upload"}:
            if file_record.status in {
                constants.FILE_STATUS_READY_FOR_UPLOAD,
                constants.FILE_STATUS_UPLOADING,
            }:
                cancelled = self._cancel_upload(file_record)
                return CancelJobSummary(
                    job_id=job_id,
                    job_type="upload",
                    filename=_file_record_filename(file_record),
                    status="Cancelled" if cancelled else "Not Cancelled",
                    message="Cancelled" if cancelled else "Unable to cancel upload.",
                    cancelled=cancelled,
                )
            return _non_cancellable_summary(
                job_id=job_id,
                job_type="upload" if normalized_job_type == "upload" else None,
                filename=_file_record_filename(file_record),
                status=file_record.status,
            )

        return _non_cancellable_summary(
            job_id=job_id,
            job_type=normalized_job_type,
            filename=_file_record_filename(file_record),
            status=file_record.status,
        )

    def _download_snapshot(self) -> DownloadQueueSnapshot | None:
        if self._download_queue is None:
            return None
        return self._download_queue.snapshot()

    def _upload_snapshot(self) -> UploadWorkerSnapshot | None:
        if self._upload_worker is None:
            return None
        return self._upload_worker.snapshot()

    def _retry_failed_download(self, file_record: FileRecord) -> bool:
        if self._download_queue is None:
            return False
        download = self._repository.get_download(file_record.id)
        if download is None:
            return False
        return self._download_queue.retry_failed_download(file_record, download)

    def _retry_failed_upload(self, file_record: FileRecord) -> bool:
        try:
            updated = self._repository.transition_file_state(
                file_record.id,
                JobState.READY_FOR_UPLOAD,
            )
        except InvalidJobStateTransition:
            return False
        if updated is None:
            return False
        self._repository.clear_upload_retry_schedule(file_record.id)
        return True

    def _cancel_download(self, file_record: FileRecord) -> bool:
        cancelled = False
        if self._download_queue is not None:
            cancelled = self._download_queue.cancel(
                file_record.id,
                source="admin_command",
            )
        if not cancelled:
            cancelled_download = self._repository.cancel_download(file_record.id)
            cancelled = (
                cancelled_download is not None
                and cancelled_download.status == constants.DOWNLOAD_STATUS_CANCELLED
            )
        return cancelled

    def _cancel_upload(self, file_record: FileRecord) -> bool:
        cancelled = False
        if self._upload_worker is not None:
            cancelled = self._upload_worker.cancel(
                file_record.id,
                source="admin_command",
            )
        if not cancelled:
            cancelled_upload = self._repository.cancel_upload(file_record.id)
            cancelled = (
                cancelled_upload is not None
                and cancelled_upload.status == constants.FILE_STATUS_CANCELLED
            )
        return cancelled

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

    def _repository_protected_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for download in self._repository.list_download_records_with_paths():
            if download.temp_path:
                temp_path = Path(download.temp_path)
                paths.extend((temp_path, Path(f"{temp_path}.temp")))
            if download.local_path:
                local_path = Path(download.local_path)
                paths.extend(
                    (
                        local_path,
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


def _is_failed_download(file_record: FileRecord, repository: DatabaseRepository) -> bool:
    download = repository.get_download(file_record.id)
    return download is not None and download.status == constants.DOWNLOAD_STATUS_FAILED


def _is_retry_eligible_failed_upload(file_record: FileRecord) -> bool:
    return (
        file_record.status == constants.FILE_STATUS_FAILED
        and file_record.google_drive_file_id is None
        and file_record.upload_retry_count > 0
    )


def _file_record_filename(file_record: FileRecord) -> str:
    return file_record.original_name or f"{file_record.file_type or 'file'}-{file_record.id}"


def _non_cancellable_summary(
    *,
    job_id: int,
    job_type: str | None,
    filename: str,
    status: str,
) -> CancelJobSummary:
    if status in {
        constants.FILE_STATUS_COMPLETED,
        constants.FILE_STATUS_UPLOADED,
        constants.DOWNLOAD_STATUS_COMPLETED,
    }:
        message = "Job already completed."
        display_status = "Completed"
    elif status in {constants.FILE_STATUS_CANCELLED, constants.DOWNLOAD_STATUS_CANCELLED}:
        message = "Job already cancelled."
        display_status = "Cancelled"
    elif status in {constants.FILE_STATUS_FAILED, constants.DOWNLOAD_STATUS_FAILED}:
        message = "Job already failed."
        display_status = "Failed"
    else:
        message = "Job is not cancellable."
        display_status = status.replace("_", " ").title()
    return CancelJobSummary(
        job_id=job_id,
        job_type=job_type,
        filename=filename,
        status=display_status,
        message=message,
        cancelled=False,
    )
