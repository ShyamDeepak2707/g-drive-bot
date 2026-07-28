from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.database import DatabaseRepository, SQLiteDatabase
from app.download_queue import CurrentDownloadSnapshot, DownloadQueueSnapshot
from app.health import HealthService
from app.models import FileMetadata, TelegramFileType
from app.startup_recovery import StartupRecoverySummary
from app.upload_worker import CurrentUploadSnapshot, UploadWorkerSnapshot


@dataclass
class FakeApplication:
    running: bool
    bot: object | None = object()


class FakePyrogramSession:
    def __init__(self, running: bool) -> None:
        self.running = running

    def is_running(self) -> bool:
        return self.running


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


def test_health_service_reports_runtime_health(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    _ready_upload(repository)
    startup_time = datetime(2026, 7, 27, 10, 0, 0, tzinfo=UTC)
    recovery = StartupRecoverySummary(
        downloads_recovered=1,
        uploads_recovered=2,
        cleanup_recovered=3,
    )
    download_queue = FakeDownloadQueue(
        DownloadQueueSnapshot(
            current_download=CurrentDownloadSnapshot(
                file_record_id=1,
                filename="download.bin",
                progress_percent=42,
                speed_bytes_per_second=1024.0,
            ),
            queue_length=4,
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
            is_busy=False,
        )
    )
    service = HealthService(
        startup_time=startup_time,
        database=database,
        repository=repository,
        telegram_application=FakeApplication(running=True),  # type: ignore[arg-type]
        pyrogram_client=FakePyrogramSession(running=True),
        drive_service=object(),  # type: ignore[arg-type]
        download_queue=download_queue,  # type: ignore[arg-type]
        upload_worker=upload_worker,  # type: ignore[arg-type]
        startup_recovery_summary=recovery,
        clock=lambda: startup_time + timedelta(seconds=125),
    )

    snapshot = service.snapshot()

    assert snapshot.startup_time == startup_time
    assert snapshot.uptime_seconds == 125
    assert snapshot.telegram_bot_connected is True
    assert snapshot.pyrogram_connected is True
    assert snapshot.google_drive_authenticated is True
    assert snapshot.database_connected is True
    assert snapshot.queues.pending_downloads == 4
    assert snapshot.queues.active_downloads == 1
    assert snapshot.queues.pending_uploads == 1
    assert snapshot.queues.active_uploads == 1
    assert snapshot.startup_recovery == recovery
    assert download_queue.snapshot_calls == 1
    assert upload_worker.snapshot_calls == 1
    database.close()


def test_health_service_reports_disconnected_components(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    startup_time = datetime(2026, 7, 27, 10, 0, 0, tzinfo=UTC)
    database.close()
    service = HealthService(
        startup_time=startup_time,
        database=database,
        repository=repository,
        telegram_application=FakeApplication(running=False),  # type: ignore[arg-type]
        pyrogram_client=FakePyrogramSession(running=False),
        drive_service=None,
        download_queue=None,
        upload_worker=None,
        startup_recovery_summary=StartupRecoverySummary(0, 0, 0),
        clock=lambda: startup_time - timedelta(seconds=10),
    )

    snapshot = service.snapshot()

    assert snapshot.uptime_seconds == 0
    assert snapshot.telegram_bot_connected is False
    assert snapshot.pyrogram_connected is False
    assert snapshot.google_drive_authenticated is False
    assert snapshot.database_connected is False
    assert snapshot.queues.pending_downloads == 0
    assert snapshot.queues.active_downloads == 0
    assert snapshot.queues.pending_uploads == 0
    assert snapshot.queues.active_uploads == 0


def _ready_upload(repository: DatabaseRepository) -> None:
    user = repository.create_user(telegram_user_id=123, username=None, first_name=None)
    file_record = repository.create_file_record(
        user_id=user.id,
        metadata=FileMetadata(
            telegram_file_id="telegram-file-id",
            message_id=1,
            chat_id=123,
            forward_origin_chat_id=None,
            forward_origin_message_id=None,
            original_name="example.txt",
            mime_type="text/plain",
            size=12,
            extension=".txt",
            file_type=TelegramFileType.DOCUMENT,
            created_at="2026-07-27T10:00:00+00:00",
        ),
    )
    repository.mark_file_ready_for_upload(file_record.id)
