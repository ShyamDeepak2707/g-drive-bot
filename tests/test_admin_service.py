from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.admin_service import AdminService
from app.constants import (
    DOWNLOAD_STATUS_CANCELLED,
    DOWNLOAD_STATUS_COMPLETED,
    DOWNLOAD_STATUS_QUEUED,
    DOWNLOAD_STATUS_RUNNING,
    FILE_STATUS_CANCELLED,
    FILE_STATUS_COMPLETED,
    FILE_STATUS_FAILED,
    FILE_STATUS_READY_FOR_UPLOAD,
    FILE_STATUS_UPLOADING,
)
from app.database import DatabaseRepository, DownloadRecord, FileRecord, SQLiteDatabase
from app.download_queue import CurrentDownloadSnapshot, DownloadQueueSnapshot
from app.health import HealthSnapshot, QueueHealth
from app.job_state import JobState
from app.models import FileMetadata, TelegramFileType
from app.startup_recovery import StartupRecoverySummary
from app.upload_worker import CurrentUploadSnapshot, UploadWorkerSnapshot


@dataclass
class FakeHealthService:
    snapshot_value: HealthSnapshot
    calls: int = 0

    def snapshot(self) -> HealthSnapshot:
        self.calls += 1
        return self.snapshot_value


class FakeDownloadQueue:
    def __init__(self, snapshot: DownloadQueueSnapshot) -> None:
        self.snapshot_calls = 0
        self.retry_calls = 0
        self.retried_file_record_ids: list[int] = []
        self._snapshot = snapshot

    def snapshot(self) -> DownloadQueueSnapshot:
        self.snapshot_calls += 1
        return self._snapshot

    def retry_failed_download(
        self,
        file_record: FileRecord,
        download: DownloadRecord,
    ) -> bool:
        self.retry_calls += 1
        if "permanent" in (download.error_message or ""):
            return False
        self.retried_file_record_ids.append(file_record.id)
        return True

    def cancel(self, file_record_id: int, source: str = "manual") -> bool:
        del source
        return False


class FakeUploadWorker:
    def __init__(self, snapshot: UploadWorkerSnapshot) -> None:
        self.snapshot_calls = 0
        self.start_calls = 0
        self.cancel_calls: list[int] = []
        self._snapshot = snapshot

    def snapshot(self) -> UploadWorkerSnapshot:
        self.snapshot_calls += 1
        return self._snapshot

    def start(self) -> None:
        self.start_calls += 1

    def cancel(self, file_record_id: int, source: str = "manual") -> bool:
        del source
        self.cancel_calls.append(file_record_id)
        return False


class RepositoryBackedRetryQueue(FakeDownloadQueue):
    def __init__(self, repository: DatabaseRepository) -> None:
        super().__init__(
            DownloadQueueSnapshot(
                current_download=None,
                queue_length=0,
                completed_since_startup=0,
                failed_since_startup=0,
            )
        )
        self._repository = repository

    def retry_failed_download(
        self,
        file_record: FileRecord,
        download: DownloadRecord,
    ) -> bool:
        self.retry_calls += 1
        if "permanent" in (download.error_message or ""):
            return False
        retried = self._repository.requeue_failed_download(file_record.id)
        if retried is None:
            return False
        self.retried_file_record_ids.append(file_record.id)
        return True


def test_admin_service_runtime_statistics_reuse_health_service(tmp_path: Path) -> None:
    service, database, health, _, _ = _admin_service(tmp_path)

    stats = service.runtime_statistics()

    assert health.calls == 1
    assert stats.startup_time == health.snapshot_value.startup_time
    assert stats.uptime_seconds == 90
    assert stats.telegram_bot_connected is True
    assert stats.pyrogram_connected is True
    assert stats.google_drive_authenticated is True
    assert stats.database_connected is True
    assert stats.pending_downloads == 3
    assert stats.active_downloads == 1
    assert stats.pending_uploads == 2
    assert stats.active_uploads == 1
    assert stats.startup_recovery == StartupRecoverySummary(1, 2, 3)
    assert stats.total_tracked_files == 0
    assert stats.completed_downloads == 0
    assert stats.completed_uploads == 0
    assert stats.failed_downloads == 0
    assert stats.failed_uploads == 0
    assert stats.download_retry_attempts == 0
    assert stats.upload_retry_attempts == 0
    assert stats.scheduled_upload_retries == 0
    database.close()


def test_admin_service_runtime_statistics_include_repository_aggregates(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    completed_download = _create_file(repository, message_id=1, name="download-complete.txt")
    repository.save_download(completed_download.id, total_bytes=12)
    repository.mark_download_complete(completed_download.id, "downloads/download-complete.txt", 12)
    completed_upload = _create_file(repository, message_id=2, name="upload-complete.txt")
    repository.mark_file_completed(completed_upload.id, "drive-file-id")
    failed_download = _create_file(repository, message_id=3, name="download-failed.txt")
    repository.save_download(failed_download.id, total_bytes=12)
    repository.mark_file_queued(failed_download.id)
    repository.mark_download_failed(failed_download.id, "download failed", retry_count=2)
    failed_upload = _create_file(repository, message_id=4, name="upload-failed.txt")
    repository.transition_file_state(failed_upload.id, JobState.READY_FOR_UPLOAD)
    repository.mark_file_uploading(failed_upload.id)
    repository.schedule_upload_retry(
        failed_upload.id,
        retry_count=3,
        retry_after="2026-07-28T11:00:00+00:00",
        error_message="upload retry",
    )
    repository.mark_upload_failed_permanently(failed_upload.id, "upload failed")
    scheduled_retry = _create_file(repository, message_id=5, name="upload-retry.txt")
    repository.transition_file_state(scheduled_retry.id, JobState.READY_FOR_UPLOAD)
    repository.mark_file_uploading(scheduled_retry.id)
    repository.schedule_upload_retry(
        scheduled_retry.id,
        retry_count=1,
        retry_after="2026-07-28T11:00:00+00:00",
        error_message="rate limited",
    )
    service, _, _, _, _ = _admin_service(tmp_path, repository=repository, database=database)

    stats = service.runtime_statistics()

    assert stats.total_tracked_files == 5
    assert stats.completed_downloads == 1
    assert stats.completed_uploads == 1
    assert stats.failed_downloads == 1
    assert stats.failed_uploads == 1
    assert stats.download_retry_attempts == 2
    assert stats.upload_retry_attempts == 4
    assert stats.scheduled_upload_retries == 1
    database.close()


def test_admin_service_queue_inspection_uses_existing_snapshots(tmp_path: Path) -> None:
    service, database, health, download_queue, upload_worker = _admin_service(tmp_path)

    queue = service.queue_inspection()

    assert health.calls == 1
    assert download_queue.snapshot_calls == 1
    assert upload_worker.snapshot_calls == 1
    assert queue.pending_downloads == 3
    assert queue.pending_uploads == 2
    assert queue.active_download is not None
    assert queue.active_download.filename == "download.bin"
    assert queue.active_upload is not None
    assert queue.active_upload.filename == "upload.bin"
    assert queue.upload_worker_busy is True
    database.close()


def test_admin_service_failed_job_summaries_are_read_only(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    download_failed = _create_file(repository, message_id=1, name="download-failed.txt")
    repository.save_download(download_failed.id, total_bytes=12)
    repository.mark_file_queued(download_failed.id)
    repository.mark_download_failed(download_failed.id, "download exploded", retry_count=2)
    upload_failed = _create_file(repository, message_id=2, name="upload-failed.txt")
    repository.transition_file_state(upload_failed.id, JobState.READY_FOR_UPLOAD)
    repository.mark_upload_failed_permanently(upload_failed.id, "upload denied")
    service, _, _, _, _ = _admin_service(tmp_path, repository=repository, database=database)

    summaries = service.failed_job_summaries(limit=10)

    assert len(summaries) == 2
    by_name = {summary.filename: summary for summary in summaries}
    assert by_name["download-failed.txt"].status == FILE_STATUS_FAILED
    assert by_name["download-failed.txt"].download_status == "failed"
    assert by_name["download-failed.txt"].error_message == "download exploded"
    assert by_name["download-failed.txt"].upload_retry_count == 0
    assert by_name["upload-failed.txt"].error_message == "upload denied"
    assert by_name["upload-failed.txt"].download_status is None
    updated_download_failed = repository.get_file_record(download_failed.id)
    updated_upload_failed = repository.get_file_record(upload_failed.id)
    assert updated_download_failed is not None
    assert updated_upload_failed is not None
    assert updated_download_failed.status == FILE_STATUS_FAILED
    assert updated_upload_failed.status == FILE_STATUS_FAILED
    database.close()


def test_admin_service_failed_job_summaries_respect_limit(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    first = _create_file(repository, message_id=1, name="first.txt")
    second = _create_file(repository, message_id=2, name="second.txt")
    repository.update_file_status(first.id, FILE_STATUS_FAILED)
    repository.update_file_status(second.id, FILE_STATUS_FAILED)
    service, _, _, _, _ = _admin_service(tmp_path, repository=repository, database=database)

    assert len(service.failed_job_summaries(limit=1)) == 1
    assert service.failed_job_summaries(limit=0) == ()
    database.close()


def test_admin_service_retry_failed_jobs_reports_no_eligible_jobs(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    permanent_upload = _create_file(repository, message_id=1, name="permanent-upload.txt")
    repository.transition_file_state(permanent_upload.id, JobState.READY_FOR_UPLOAD)
    repository.mark_upload_failed_permanently(permanent_upload.id, "missing local file")
    retry_queue = RepositoryBackedRetryQueue(repository)
    upload_worker = FakeUploadWorker(_idle_upload_snapshot())
    service, _, _, _, _ = _admin_service(
        tmp_path,
        repository=repository,
        database=database,
        download_queue=retry_queue,
        upload_worker=upload_worker,
    )

    summary = service.retry_failed_jobs()

    assert summary.eligible_jobs == 0
    assert summary.requeued_jobs == 0
    assert summary.skipped_jobs == 1
    assert retry_queue.retry_calls == 0
    assert upload_worker.start_calls == 0
    database.close()


def test_admin_service_retry_failed_jobs_handles_mixed_failures(
    tmp_path: Path,
) -> None:
    service, database, repository, retry_queue, upload_worker = _retry_service(tmp_path)

    summary = service.retry_failed_jobs()

    assert summary.eligible_jobs == 2
    assert summary.requeued_jobs == 2
    assert summary.skipped_jobs == 2
    assert retry_queue.retry_calls == 2
    assert len(retry_queue.retried_file_record_ids) == 1
    assert upload_worker.start_calls == 1

    retried_download = repository.get_download(retry_queue.retried_file_record_ids[0])
    assert retried_download is not None
    assert retried_download.status == DOWNLOAD_STATUS_QUEUED
    retried_upload = repository.get_file_record(3)
    assert retried_upload is not None
    assert retried_upload.status == FILE_STATUS_READY_FOR_UPLOAD
    database.close()


def test_admin_service_retry_failed_jobs_is_idempotent(tmp_path: Path) -> None:
    service, database, _, retry_queue, upload_worker = _retry_service(tmp_path)

    first = service.retry_failed_jobs()
    second = service.retry_failed_jobs()

    assert first.eligible_jobs == 2
    assert first.requeued_jobs == 2
    assert first.skipped_jobs == 2
    assert second.eligible_jobs == 0
    assert second.requeued_jobs == 0
    assert second.skipped_jobs == 2
    assert len(retry_queue.retried_file_record_ids) == 1
    assert upload_worker.start_calls == 1
    database.close()


def test_admin_service_retry_failed_jobs_skips_cancelled_jobs(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    cancelled = _create_file(repository, message_id=1, name="cancelled.txt")
    repository.transition_file_state(cancelled.id, JobState.READY_FOR_UPLOAD)
    repository.cancel_upload(cancelled.id)
    retry_queue = RepositoryBackedRetryQueue(repository)
    upload_worker = FakeUploadWorker(_idle_upload_snapshot())
    service, _, _, _, _ = _admin_service(
        tmp_path,
        repository=repository,
        database=database,
        download_queue=retry_queue,
        upload_worker=upload_worker,
    )

    summary = service.retry_failed_jobs()

    assert summary.eligible_jobs == 0
    assert summary.requeued_jobs == 0
    assert summary.skipped_jobs == 0
    assert retry_queue.retry_calls == 0
    assert upload_worker.start_calls == 0
    updated = repository.get_file_record(cancelled.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_CANCELLED
    database.close()


def test_admin_service_cancel_queued_download(tmp_path: Path) -> None:
    database, repository, file_record = _download_cancel_record(
        tmp_path,
        download_status=DOWNLOAD_STATUS_QUEUED,
    )
    service, _, _, _, _ = _admin_service(tmp_path, repository=repository, database=database)

    summary = service.cancel_job(file_record.id)

    assert summary.cancelled is True
    assert summary.job_type == "download"
    assert summary.status == "Cancelled"
    download = repository.get_download(file_record.id)
    updated = repository.get_file_record(file_record.id)
    assert download is not None
    assert updated is not None
    assert download.status == DOWNLOAD_STATUS_CANCELLED
    assert updated.status == FILE_STATUS_CANCELLED
    database.close()


def test_admin_service_cancel_running_download(tmp_path: Path) -> None:
    database, repository, file_record = _download_cancel_record(
        tmp_path,
        download_status=DOWNLOAD_STATUS_RUNNING,
    )
    service, _, _, _, _ = _admin_service(tmp_path, repository=repository, database=database)

    summary = service.cancel_job(file_record.id, job_type="download")

    assert summary.cancelled is True
    assert summary.job_type == "download"
    assert repository.get_download(file_record.id).status == DOWNLOAD_STATUS_CANCELLED  # type: ignore[union-attr]
    database.close()


def test_admin_service_cancel_queued_upload(tmp_path: Path) -> None:
    database, repository, file_record = _upload_cancel_record(
        tmp_path,
        status=FILE_STATUS_READY_FOR_UPLOAD,
    )
    service, _, _, _, upload_worker = _admin_service(
        tmp_path,
        repository=repository,
        database=database,
    )

    summary = service.cancel_job(file_record.id)

    assert summary.cancelled is True
    assert summary.job_type == "upload"
    assert upload_worker.cancel_calls == [file_record.id]
    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_CANCELLED
    database.close()


def test_admin_service_cancel_running_upload(tmp_path: Path) -> None:
    database, repository, file_record = _upload_cancel_record(
        tmp_path,
        status=FILE_STATUS_UPLOADING,
    )
    service, _, _, _, upload_worker = _admin_service(
        tmp_path,
        repository=repository,
        database=database,
    )

    summary = service.cancel_job(file_record.id, job_type="upload")

    assert summary.cancelled is True
    assert summary.job_type == "upload"
    assert upload_worker.cancel_calls == [file_record.id]
    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_CANCELLED
    database.close()


def test_admin_service_cancel_completed_job_is_noop(tmp_path: Path) -> None:
    database, repository, file_record = _download_cancel_record(
        tmp_path,
        download_status=DOWNLOAD_STATUS_COMPLETED,
    )
    repository.update_file_status(file_record.id, FILE_STATUS_COMPLETED)
    service, _, _, _, _ = _admin_service(tmp_path, repository=repository, database=database)

    summary = service.cancel_job(file_record.id)

    assert summary.cancelled is False
    assert summary.message == "Job already completed."
    assert repository.get_file_record(file_record.id).status == FILE_STATUS_COMPLETED  # type: ignore[union-attr]
    database.close()


def test_admin_service_cancel_failed_job_is_noop(tmp_path: Path) -> None:
    database, repository, file_record = _download_cancel_record(
        tmp_path,
        download_status=DOWNLOAD_STATUS_QUEUED,
    )
    repository.mark_download_failed(file_record.id, "failed", retry_count=1)
    service, _, _, _, _ = _admin_service(tmp_path, repository=repository, database=database)

    summary = service.cancel_job(file_record.id)

    assert summary.cancelled is False
    assert summary.message == "Job already failed."
    assert repository.get_file_record(file_record.id).status == FILE_STATUS_FAILED  # type: ignore[union-attr]
    database.close()


def test_admin_service_cancel_already_cancelled_job_is_idempotent(tmp_path: Path) -> None:
    database, repository, file_record = _download_cancel_record(
        tmp_path,
        download_status=DOWNLOAD_STATUS_QUEUED,
    )
    first = repository.cancel_download(file_record.id)
    assert first is not None
    service, _, _, _, _ = _admin_service(tmp_path, repository=repository, database=database)

    summary = service.cancel_job(file_record.id)

    assert summary.cancelled is False
    assert summary.message == "Job already cancelled."
    assert repository.get_download(file_record.id).status == DOWNLOAD_STATUS_CANCELLED  # type: ignore[union-attr]
    database.close()


def test_admin_service_cancel_job_not_found(tmp_path: Path) -> None:
    service, database, _, _, _ = _admin_service(tmp_path)

    summary = service.cancel_job(999)

    assert summary.cancelled is False
    assert summary.message == "Job not found."
    assert summary.status == "Not Found"
    database.close()


def test_admin_service_temporary_file_summaries_dedupe_sources(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    temp_dir = tmp_path / "tmp"
    downloads_dir = tmp_path / "downloads"
    temp_dir.mkdir()
    downloads_dir.mkdir()
    temp_path = temp_dir / "active.part"
    temp_path.write_bytes(b"abc")
    local_path = downloads_dir / "movie.mkv"
    local_part = Path(f"{local_path}.part")
    local_part.write_bytes(b"12345")
    ignored = downloads_dir / "finished.mkv"
    ignored.write_bytes(b"not temporary")
    file_record = _create_file(repository, message_id=1, name="movie.mkv")
    repository.save_download(
        file_record.id,
        local_path=str(local_path),
        temp_path=str(temp_path),
        total_bytes=8,
    )
    service, _, _, _, _ = _admin_service(
        tmp_path,
        repository=repository,
        database=database,
        temp_dir=temp_dir,
        downloads_dir=downloads_dir,
    )

    summary = service.temporary_file_summaries()

    assert summary.total_files == 2
    assert summary.total_bytes == 8
    paths = {item.path for item in summary.files}
    assert temp_path.resolve() in paths
    assert local_part.resolve() in paths
    assert ignored.resolve() not in paths
    database.close()


def test_admin_service_cleanup_temp_deletes_only_orphans_and_protects_tracked_files(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    temp_dir = tmp_path / "tmp"
    downloads_dir = tmp_path / "downloads"
    temp_dir.mkdir()
    downloads_dir.mkdir()
    orphan = temp_dir / "orphan.part"
    orphan.write_bytes(b"orphan")
    tracked_temp = temp_dir / "active.part"
    tracked_temp.write_bytes(b"active")
    local_path = downloads_dir / "movie.mkv"
    tracked_local_part = Path(f"{local_path}.part")
    tracked_local_part.write_bytes(b"tracked")
    file_record = _create_file(repository, message_id=1, name="movie.mkv")
    repository.save_download(
        file_record.id,
        local_path=str(local_path),
        temp_path=str(tracked_temp),
        total_bytes=12,
    )
    service, _, _, _, _ = _admin_service(
        tmp_path,
        repository=repository,
        database=database,
        temp_dir=temp_dir,
        downloads_dir=downloads_dir,
    )

    preview = service.cleanup_temp(confirm=False)

    assert preview.confirmed is False
    assert preview.total_files == 1
    assert preview.files[0].path == orphan.resolve()
    assert orphan.exists()

    confirmed = service.cleanup_temp(confirm=True)
    repeated = service.cleanup_temp(confirm=True)

    assert confirmed.confirmed is True
    assert confirmed.deleted_files == 1
    assert confirmed.deleted_bytes == len(b"orphan")
    assert not orphan.exists()
    assert tracked_temp.exists()
    assert tracked_local_part.exists()
    assert repeated.total_files == 0
    assert repeated.deleted_files == 0
    database.close()


def _admin_service(
    tmp_path: Path,
    *,
    repository: DatabaseRepository | None = None,
    database: SQLiteDatabase | None = None,
    download_queue: FakeDownloadQueue | None = None,
    upload_worker: FakeUploadWorker | None = None,
    temp_dir: Path | None = None,
    downloads_dir: Path | None = None,
) -> tuple[
    AdminService,
    SQLiteDatabase,
    FakeHealthService,
    FakeDownloadQueue,
    FakeUploadWorker,
]:
    if database is None:
        database = SQLiteDatabase(tmp_path / "app.sqlite3")
        database.initialize()
    if repository is None:
        repository = DatabaseRepository(database)
    startup_time = datetime(2026, 7, 27, 10, 0, 0, tzinfo=UTC)
    health = FakeHealthService(
        HealthSnapshot(
            startup_time=startup_time,
            uptime_seconds=90,
            telegram_bot_connected=True,
            pyrogram_connected=True,
            google_drive_authenticated=True,
            database_connected=True,
            queues=QueueHealth(
                pending_downloads=3,
                active_downloads=1,
                pending_uploads=2,
                active_uploads=1,
            ),
            startup_recovery=StartupRecoverySummary(1, 2, 3),
        )
    )
    if download_queue is None:
        download_queue = FakeDownloadQueue(
            DownloadQueueSnapshot(
                current_download=CurrentDownloadSnapshot(
                    file_record_id=1,
                    filename="download.bin",
                    progress_percent=40,
                    speed_bytes_per_second=2048.0,
                ),
                queue_length=3,
                completed_since_startup=0,
                failed_since_startup=0,
            )
        )
    if upload_worker is None:
        upload_worker = FakeUploadWorker(
            UploadWorkerSnapshot(
                current_upload=CurrentUploadSnapshot(
                    file_record_id=2,
                    filename="upload.bin",
                    progress_percent=None,
                    status="uploading",
                ),
                completed_since_startup=0,
                failed_since_startup=0,
                is_busy=True,
            )
        )
    service = AdminService(
        health_service=health,  # type: ignore[arg-type]
        repository=repository,
        download_queue=download_queue,  # type: ignore[arg-type]
        upload_worker=upload_worker,  # type: ignore[arg-type]
        temp_dir=temp_dir or tmp_path / "tmp",
        downloads_dir=downloads_dir or tmp_path / "downloads",
    )
    return service, database, health, download_queue, upload_worker


def _retry_service(
    tmp_path: Path,
) -> tuple[
    AdminService,
    SQLiteDatabase,
    DatabaseRepository,
    RepositoryBackedRetryQueue,
    FakeUploadWorker,
]:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)

    transient_download = _create_file(repository, message_id=1, name="download-transient.txt")
    repository.save_download(
        transient_download.id,
        total_bytes=12,
        status_chat_id=123,
        status_message_id=99,
    )
    repository.mark_file_queued(transient_download.id)
    repository.mark_download_failed(transient_download.id, "temporary network error", retry_count=3)

    permanent_download = _create_file(repository, message_id=2, name="download-permanent.txt")
    repository.save_download(
        permanent_download.id,
        total_bytes=12,
        status_chat_id=123,
        status_message_id=100,
    )
    repository.mark_file_queued(permanent_download.id)
    repository.mark_download_failed(permanent_download.id, "permanent bot api failure", 3)

    transient_upload = _create_file(repository, message_id=3, name="upload-transient.txt")
    repository.transition_file_state(transient_upload.id, JobState.READY_FOR_UPLOAD)
    repository.mark_file_uploading(transient_upload.id)
    repository.schedule_upload_retry(
        transient_upload.id,
        retry_count=1,
        retry_after="2026-07-28T11:00:00+00:00",
        error_message="rate limited",
    )
    repository.mark_upload_failed_permanently(transient_upload.id, "retry limit reached")

    permanent_upload = _create_file(repository, message_id=4, name="upload-permanent.txt")
    repository.transition_file_state(permanent_upload.id, JobState.READY_FOR_UPLOAD)
    repository.mark_upload_failed_permanently(permanent_upload.id, "missing local file")

    retry_queue = RepositoryBackedRetryQueue(repository)
    upload_worker = FakeUploadWorker(_idle_upload_snapshot())
    service, _, _, _, _ = _admin_service(
        tmp_path,
        repository=repository,
        database=database,
        download_queue=retry_queue,
        upload_worker=upload_worker,
    )
    return service, database, repository, retry_queue, upload_worker


def _download_cancel_record(
    tmp_path: Path,
    *,
    download_status: str,
) -> tuple[SQLiteDatabase, DatabaseRepository, FileRecord]:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    file_record = _create_file(repository, message_id=1, name="download.txt")
    repository.save_download(
        file_record.id,
        total_bytes=12,
        status_chat_id=123,
        status_message_id=99,
    )
    if download_status == DOWNLOAD_STATUS_RUNNING:
        repository.update_download_progress(file_record.id, bytes_downloaded=1, total_bytes=12)
    elif download_status == DOWNLOAD_STATUS_COMPLETED:
        repository.mark_download_complete(file_record.id, "downloads/download.txt", 12)
    elif download_status != DOWNLOAD_STATUS_QUEUED:
        raise ValueError(f"Unsupported download status: {download_status}")
    return database, repository, file_record


def _upload_cancel_record(
    tmp_path: Path,
    *,
    status: str,
) -> tuple[SQLiteDatabase, DatabaseRepository, FileRecord]:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    file_record = _create_file(repository, message_id=1, name="upload.txt")
    repository.transition_file_state(file_record.id, JobState.READY_FOR_UPLOAD)
    if status == FILE_STATUS_UPLOADING:
        uploading = repository.transition_file_state(file_record.id, JobState.UPLOADING)
        if uploading is None:
            raise RuntimeError("Upload record was not available after transition.")
        file_record = uploading
    elif status != FILE_STATUS_READY_FOR_UPLOAD:
        raise ValueError(f"Unsupported upload status: {status}")
    updated = repository.get_file_record(file_record.id)
    if updated is None:
        raise RuntimeError("File record disappeared.")
    return database, repository, updated


def _idle_upload_snapshot() -> UploadWorkerSnapshot:
    return UploadWorkerSnapshot(
        current_upload=None,
        completed_since_startup=0,
        failed_since_startup=0,
        is_busy=False,
    )


def _create_file(
    repository: DatabaseRepository,
    *,
    message_id: int,
    name: str,
) -> FileRecord:
    user = repository.create_user(
        telegram_user_id=message_id,
        username=None,
        first_name=None,
    )
    return repository.create_file_record(
        user_id=user.id,
        metadata=FileMetadata(
            telegram_file_id=f"telegram-file-id-{message_id}",
            message_id=message_id,
            chat_id=123,
            forward_origin_chat_id=None,
            forward_origin_message_id=None,
            original_name=name,
            mime_type="text/plain",
            size=12,
            extension=".txt",
            file_type=TelegramFileType.DOCUMENT,
            created_at="2026-07-27T10:00:00+00:00",
        ),
    )
