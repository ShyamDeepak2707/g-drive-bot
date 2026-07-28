from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.admin_service import AdminService
from app.constants import FILE_STATUS_FAILED
from app.database import DatabaseRepository, FileRecord, SQLiteDatabase
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
        self._snapshot = snapshot

    def snapshot(self) -> DownloadQueueSnapshot:
        self.snapshot_calls += 1
        return self._snapshot


class FakeUploadWorker:
    def __init__(self, snapshot: UploadWorkerSnapshot) -> None:
        self.snapshot_calls = 0
        self._snapshot = snapshot

    def snapshot(self) -> UploadWorkerSnapshot:
        self.snapshot_calls += 1
        return self._snapshot


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


def _admin_service(
    tmp_path: Path,
    *,
    repository: DatabaseRepository | None = None,
    database: SQLiteDatabase | None = None,
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
