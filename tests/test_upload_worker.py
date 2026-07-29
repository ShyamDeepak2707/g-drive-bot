from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from app.constants import (
    FILE_STATUS_CANCELLED,
    FILE_STATUS_COMPLETED,
    FILE_STATUS_FAILED,
    FILE_STATUS_READY_FOR_UPLOAD,
    FILE_STATUS_UPLOADED,
    FILE_STATUS_UPLOADING,
)
from app.database import DatabaseRepository, FileRecord, SQLiteDatabase
from app.job_state import JobState
from app.models import FileMetadata, Folder, TelegramFileType
from app.progress import ProgressSnapshot
from app.upload_worker import (
    GoogleDriveUploader,
    UploadedFile,
    UploadProgressCallback,
    UploadWorker,
)


class FakeUploader:
    def __init__(
        self,
        drive_file_id: str = "drive-file-id",
        fail: bool = False,
        uploaded_size: int | None = None,
    ) -> None:
        self.drive_file_id = drive_file_id
        self.fail = fail
        self.uploaded_size = uploaded_size
        self.calls = 0
        self.local_path: Path | None = None
        self.filename: str | None = None
        self.mime_type: str | None = None
        self.folder_id: str | None = None

    def upload_file(
        self,
        local_path: Path,
        filename: str,
        mime_type: str | None,
        folder_id: str,
        progress_callback: UploadProgressCallback | None = None,
    ) -> UploadedFile:
        self.calls += 1
        self.local_path = local_path
        self.filename = filename
        self.mime_type = mime_type
        self.folder_id = folder_id
        if self.fail:
            raise RuntimeError("temporary drive failure")
        if progress_callback is not None:
            size = local_path.stat().st_size
            progress_callback(
                ProgressSnapshot(
                    current=size,
                    total=size,
                    speed_bytes_per_second=1024.0,
                    eta_seconds=0,
                )
            )
        return UploadedFile(
            drive_file_id=self.drive_file_id,
            size=(
                self.uploaded_size if self.uploaded_size is not None else local_path.stat().st_size
            ),
        )


class BlockingUploader(FakeUploader):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def upload_file(
        self,
        local_path: Path,
        filename: str,
        mime_type: str | None,
        folder_id: str,
        progress_callback: UploadProgressCallback | None = None,
    ) -> UploadedFile:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("upload test timed out")
        return super().upload_file(local_path, filename, mime_type, folder_id, progress_callback)


class FakeNotificationBot:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[dict[str, object]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> object:
        if self.fail:
            raise RuntimeError("Telegram send failed")
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
            }
        )
        return object()

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> object:
        if self.fail:
            raise RuntimeError("Telegram edit failed")
        self.messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
            }
        )
        return object()


class FakeUploadStatus:
    def __init__(self, current: int, total: int) -> None:
        self.resumable_progress = current
        self.total_size = total

    def progress(self) -> float:
        return self.resumable_progress / self.total_size


class FakeDriveRequest:
    def __init__(
        self,
        response: dict[str, object],
        chunks: list[tuple[FakeUploadStatus | None, dict[str, object] | None]] | None = None,
    ) -> None:
        self.response = response
        self.chunks = chunks or []

    def execute(self) -> dict[str, object]:
        return self.response

    def next_chunk(self) -> tuple[FakeUploadStatus | None, dict[str, object] | None]:
        if not self.chunks:
            return None, self.response
        return self.chunks.pop(0)


class FakeDriveFilesResource:
    def __init__(self) -> None:
        self.created: dict[str, object] | None = None
        self.fetched_file_id: str | None = None

    def create(self, **kwargs: object) -> FakeDriveRequest:
        self.created = kwargs
        return FakeDriveRequest(
            {"id": "drive-file-id"},
            chunks=[
                (FakeUploadStatus(2, 5), None),
                (FakeUploadStatus(5, 5), {"id": "drive-file-id"}),
            ],
        )

    def get(self, **kwargs: object) -> FakeDriveRequest:
        self.fetched_file_id = str(kwargs["fileId"])
        return FakeDriveRequest({"id": kwargs["fileId"], "size": "5"})


class FakeDriveService:
    def __init__(self) -> None:
        self.files_resource = FakeDriveFilesResource()

    def files(self) -> FakeDriveFilesResource:
        return self.files_resource


def test_google_drive_uploader_fetches_metadata_after_upload(tmp_path: Path) -> None:
    local_path = tmp_path / "example.txt"
    local_path.write_text("hello", encoding="utf-8")
    service = FakeDriveService()
    uploader = GoogleDriveUploader(service)
    progress: list[ProgressSnapshot] = []

    uploaded = uploader.upload_file(
        local_path=local_path,
        filename="example.txt",
        mime_type="text/plain",
        folder_id="folder-id",
        progress_callback=progress.append,
    )

    assert uploaded.drive_file_id == "drive-file-id"
    assert uploaded.size == 5
    assert service.files_resource.created is not None
    assert service.files_resource.fetched_file_id == "drive-file-id"
    assert [snapshot.current for snapshot in progress] == [2, 5, 5]
    assert all(snapshot.total == 5 for snapshot in progress)


def test_upload_worker_exits_cleanly_when_no_ready_jobs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"))

    with caplog.at_level(logging.INFO, logger="test.upload_worker"):
        asyncio.run(worker.run_once())

    assert any(getattr(record, "event", None) == "upload_worker_idle" for record in caplog.records)
    database.close()


def test_upload_worker_uploads_next_ready_job_and_persists_drive_id(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    with caplog.at_level(logging.INFO, logger="test.upload_worker"):
        asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_COMPLETED
    assert updated.google_drive_file_id == "drive-file-id"
    assert not local_path.exists()
    assert uploader.calls == 1
    assert uploader.local_path == local_path
    assert uploader.filename == "example-1.txt"
    assert uploader.mime_type == "text/plain"
    assert uploader.folder_id == "folder-id"
    assert any(
        getattr(record, "event", None) == "upload_started"
        and getattr(record, "file_record_id", None) == file_record.id
        for record in caplog.records
    )
    assert any(
        getattr(record, "event", None) == "upload_verified"
        and getattr(record, "file_record_id", None) == file_record.id
        and getattr(record, "expected_size", None) == 5
        and getattr(record, "actual_size", None) == 5
        for record in caplog.records
    )
    assert any(
        getattr(record, "event", None) == "upload_uploaded"
        and getattr(record, "file_record_id", None) == file_record.id
        for record in caplog.records
    )
    assert any(
        getattr(record, "event", None) == "upload_finalized"
        and getattr(record, "file_record_id", None) == file_record.id
        for record in caplog.records
    )
    database.close()


def test_upload_worker_edits_upload_progress_message(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    _ready_file(
        repository,
        local_path=local_path,
        status_chat_id=123,
        status_message_id=99,
    )
    uploader = FakeUploader()
    bot = FakeNotificationBot()
    worker = UploadWorker(
        repository,
        logging.getLogger("test.upload_worker"),
        uploader,
        notification_bot=bot,
        progress_interval_seconds=0.001,
    )

    asyncio.run(worker.run_once())

    texts = [str(message["text"]) for message in bot.messages]
    assert any("⬆️ Uploading" in text and "<b>0%</b>" in text for text in texts)
    assert any("⬆️ Uploading" in text and "<b>100%</b>" in text for text in texts)
    assert any("✅ Done" in text for text in texts)
    assert all(message["chat_id"] == 123 for message in bot.messages)
    assert all(message["message_id"] == 99 for message in bot.messages)
    database.close()


def test_upload_worker_claims_only_one_ready_job(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    first_path = tmp_path / "example-1.txt"
    second_path = tmp_path / "example-2.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    first = _ready_file(repository, message_id=1, local_path=first_path)
    second = _ready_file(repository, message_id=2, local_path=second_path)
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    asyncio.run(worker.run_once())

    first_updated = repository.get_file_record(first.id)
    second_updated = repository.get_file_record(second.id)
    assert first_updated is not None
    assert second_updated is not None
    assert first_updated.status == FILE_STATUS_COMPLETED
    assert second_updated.status == FILE_STATUS_READY_FOR_UPLOAD
    assert uploader.calls == 1
    database.close()


def test_upload_worker_run_until_idle_drains_multiple_ready_jobs(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    paths = [
        tmp_path / "example-1.txt",
        tmp_path / "example-2.txt",
        tmp_path / "example-3.txt",
    ]
    for index, path in enumerate(paths, start=1):
        path.write_text(f"file {index}", encoding="utf-8")
        _ready_file(repository, message_id=index, local_path=path)
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    asyncio.run(worker.run_until_idle())

    for file_id, path in enumerate(paths, start=1):
        updated = repository.get_file_record(file_id)
        assert updated is not None
        assert updated.status == FILE_STATUS_COMPLETED
        assert updated.google_drive_file_id == "drive-file-id"
        assert not path.exists()
    assert uploader.calls == 3
    database.close()


def test_upload_worker_stop_waits_for_active_upload_to_persist_state(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    uploader = BlockingUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    async def scenario() -> None:
        worker.start()
        assert await asyncio.to_thread(uploader.started.wait, 2)
        snapshot = worker.snapshot()
        assert snapshot.is_busy
        assert snapshot.current_upload is not None
        assert snapshot.current_upload.filename == "example-1.txt"
        assert snapshot.current_upload.progress_percent is None
        stop_task = asyncio.create_task(worker.stop())
        await asyncio.sleep(0.05)
        assert not stop_task.done()
        uploader.release.set()
        await stop_task

    asyncio.run(scenario())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_COMPLETED
    assert updated.google_drive_file_id == "drive-file-id"
    assert not local_path.exists()
    assert uploader.calls == 1
    snapshot = worker.snapshot()
    assert not snapshot.is_busy
    assert snapshot.current_upload is None
    assert snapshot.completed_since_startup == 1
    assert snapshot.failed_since_startup == 0
    database.close()


def test_upload_worker_cancels_queued_upload(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), FakeUploader())

    assert worker.cancel(file_record.id, source="test") is True

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_CANCELLED
    assert not local_path.exists()
    database.close()


def test_upload_worker_cancels_running_upload_cooperatively(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    uploader = BlockingUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    async def scenario() -> None:
        worker.start()
        assert await asyncio.to_thread(uploader.started.wait, 2)
        assert worker.cancel(file_record.id, source="test") is True
        uploader.release.set()
        if worker._task is not None:  # noqa: SLF001
            await asyncio.wait_for(worker._task, timeout=2)  # noqa: SLF001

    asyncio.run(scenario())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_CANCELLED
    assert updated.google_drive_file_id is None
    assert uploader.calls == 1
    assert not local_path.exists()
    database.close()


def test_upload_worker_recovery_ignores_cancelled_upload(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    repository.cancel_upload(file_record.id)
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    summary = asyncio.run(worker.recover_interrupted_jobs())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_CANCELLED
    assert summary.uploads_recovered == 0
    assert summary.cleanup_recovered == 0
    assert uploader.calls == 0
    database.close()


def test_upload_worker_finalizes_existing_uploaded_job_without_uploading(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    file_record = _uploaded_file(repository, local_path=local_path)
    local_path.unlink()
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_COMPLETED
    assert updated.google_drive_file_id == "drive-file-id"
    assert not local_path.exists()
    assert uploader.calls == 0
    database.close()


def test_upload_worker_recovers_uploading_job_with_drive_id_after_restart_without_duplicate_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class SimulatedCrash(Exception):
        pass

    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)
    original_transition_file_state = repository.transition_file_state

    def crash_after_drive_id_persisted(
        file_id: int,
        target: JobState,
        google_drive_file_id: str | None = None,
    ) -> FileRecord | None:
        if target == JobState.UPLOADED:
            repository.update_file_status(
                file_id,
                FILE_STATUS_UPLOADING,
                google_drive_file_id=google_drive_file_id,
            )
            raise SimulatedCrash
        return original_transition_file_state(
            file_id,
            target,
            google_drive_file_id=google_drive_file_id,
        )

    monkeypatch.setattr(repository, "transition_file_state", crash_after_drive_id_persisted)

    with pytest.raises(SimulatedCrash):
        asyncio.run(worker.run_once())

    crashed = repository.get_file_record(file_record.id)
    assert crashed is not None
    assert crashed.status == FILE_STATUS_UPLOADING
    assert crashed.google_drive_file_id == "drive-file-id"
    assert local_path.exists()
    assert uploader.calls == 1

    monkeypatch.setattr(repository, "transition_file_state", original_transition_file_state)
    restarted_worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    with caplog.at_level(logging.INFO, logger="test.upload_worker"):
        asyncio.run(restarted_worker.run_once())

    recovered = repository.get_file_record(file_record.id)
    assert recovered is not None
    assert recovered.status == FILE_STATUS_COMPLETED
    assert recovered.google_drive_file_id == "drive-file-id"
    assert not local_path.exists()
    assert uploader.calls == 1
    assert any(
        getattr(record, "event", None) == "upload_recovered"
        and getattr(record, "file_record_id", None) == file_record.id
        for record in caplog.records
    )
    assert any(
        getattr(record, "event", None) == "upload_finalized"
        and getattr(record, "file_record_id", None) == file_record.id
        for record in caplog.records
    )
    database.close()


def test_upload_worker_leaves_uploaded_job_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _uploaded_file(repository, local_path=local_path)
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    def fail_remove(path: Path | str) -> None:
        if Path(path) == local_path:
            raise PermissionError("locked")
        raise FileNotFoundError

    monkeypatch.setattr("app.upload_worker.os.remove", fail_remove)

    with caplog.at_level(logging.WARNING, logger="test.upload_worker"):
        asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_UPLOADED
    assert updated.google_drive_file_id == "drive-file-id"
    assert local_path.exists()
    assert uploader.calls == 0
    assert any(
        getattr(record, "event", None) == "upload_finalize_cleanup_failed"
        and getattr(record, "file_record_id", None) == file_record.id
        for record in caplog.records
    )
    database.close()


def test_upload_worker_schedules_transient_failure_for_later_retry(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    uploader = FakeUploader(fail=True)
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_READY_FOR_UPLOAD
    assert updated.google_drive_file_id is None
    assert updated.upload_retry_count == 1
    assert updated.upload_retry_after is not None
    assert updated.upload_error_message == "temporary drive failure"
    assert local_path.exists()
    assert uploader.calls == 1

    uploader.fail = False
    asyncio.run(worker.run_once())

    still_waiting = repository.get_file_record(file_record.id)
    assert still_waiting is not None
    assert still_waiting.status == FILE_STATUS_READY_FOR_UPLOAD
    assert still_waiting.google_drive_file_id is None
    assert local_path.exists()
    assert uploader.calls == 1
    database.close()


def test_upload_worker_sends_transient_failure_notification(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    _ready_file(repository, local_path=local_path, status_chat_id=456)
    uploader = FakeUploader(fail=True)
    bot = FakeNotificationBot()
    worker = UploadWorker(
        repository,
        logging.getLogger("test.upload_worker"),
        uploader,
        notification_bot=bot,
    )

    asyncio.run(worker.run_once())

    assert len(bot.messages) == 1
    message = bot.messages[0]
    assert message["chat_id"] == 456
    assert message["parse_mode"] == "HTML"
    assert "Upload Delayed" in str(message["text"])
    assert "temporary drive failure" in str(message["text"])
    assert "Retry Scheduled" in str(message["text"])
    database.close()


def test_upload_worker_logs_retry_reason_and_next_retry_time(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    uploader = FakeUploader(fail=True)
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    with caplog.at_level(logging.WARNING, logger="test.upload_worker"):
        asyncio.run(worker.run_once())

    assert any(
        getattr(record, "event", None) == "upload_retry_scheduled"
        and getattr(record, "file_record_id", None) == file_record.id
        and getattr(record, "retry_reason", None) == "network_error"
        and getattr(record, "retry_count", None) == 1
        and getattr(record, "retry_after", None) is not None
        for record in caplog.records
    )
    database.close()


def test_upload_worker_marks_verification_mismatch_failed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    uploader = FakeUploader(uploaded_size=4)
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    with caplog.at_level(logging.WARNING, logger="test.upload_worker"):
        asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_FAILED
    assert updated.google_drive_file_id is None
    assert local_path.exists()
    assert uploader.calls == 1
    assert any(
        getattr(record, "event", None) == "upload_failed_permanent"
        and getattr(record, "file_record_id", None) == file_record.id
        and getattr(record, "expected_size", None) == 5
        and getattr(record, "actual_size", None) == 4
        for record in caplog.records
    )
    database.close()


def test_upload_worker_missing_uploader_is_permanent_failure(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"))

    asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_FAILED
    assert updated.google_drive_file_id is None
    assert local_path.exists()
    assert worker.snapshot().failed_since_startup == 1
    database.close()


def test_upload_worker_sends_permanent_failure_notification(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path, status_chat_id=789)
    bot = FakeNotificationBot()
    worker = UploadWorker(
        repository,
        logging.getLogger("test.upload_worker"),
        notification_bot=bot,
    )

    asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_FAILED
    assert len(bot.messages) == 1
    message = bot.messages[0]
    assert message["chat_id"] == 789
    assert "Upload Failed" in str(message["text"])
    assert "Google Drive uploader is not configured." in str(message["text"])
    assert "Failed" in str(message["text"])
    database.close()


def test_upload_worker_does_not_retry_missing_local_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    local_path.unlink()
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    with caplog.at_level(logging.WARNING, logger="test.upload_worker"):
        asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_FAILED
    assert updated.upload_retry_count == 0
    assert updated.upload_retry_after is None
    assert updated.google_drive_file_id is None
    assert uploader.calls == 0
    assert any(
        getattr(record, "event", None) == "upload_failed_permanent"
        and getattr(record, "file_record_id", None) == file_record.id
        and getattr(record, "retry_reason", None) == "missing_local_file"
        for record in caplog.records
    )
    database.close()


def test_upload_worker_notification_failure_does_not_block_failure_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path, status_chat_id=789)
    bot = FakeNotificationBot(fail=True)
    worker = UploadWorker(
        repository,
        logging.getLogger("test.upload_worker"),
        notification_bot=bot,
    )

    with caplog.at_level(logging.WARNING, logger="test.upload_worker"):
        asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_FAILED
    assert any(
        getattr(record, "event", None) == "upload_failure_notification_failed"
        and getattr(record, "file_record_id", None) == file_record.id
        for record in caplog.records
    )
    database.close()


def test_upload_worker_never_reuploads_when_drive_file_id_exists(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    file_record = _ready_file(repository, local_path=local_path)
    repository.update_file_status(
        file_record.id,
        FILE_STATUS_READY_FOR_UPLOAD,
        google_drive_file_id="drive-file-id",
    )
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    asyncio.run(worker.run_once())

    updated = repository.get_file_record(file_record.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_READY_FOR_UPLOAD
    assert updated.google_drive_file_id == "drive-file-id"
    assert local_path.exists()
    assert uploader.calls == 0
    database.close()


def test_upload_worker_startup_recovery_finalizes_uploading_with_drive_id(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    ready = _ready_file(repository, local_path=local_path)
    uploading = repository.transition_file_state(
        ready.id,
        JobState.UPLOADING,
        google_drive_file_id="drive-file-id",
    )
    assert uploading is not None
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    summary = asyncio.run(worker.recover_interrupted_jobs())
    second_summary = asyncio.run(worker.recover_interrupted_jobs())

    updated = repository.get_file_record(ready.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_COMPLETED
    assert updated.google_drive_file_id == "drive-file-id"
    assert summary.uploads_recovered == 1
    assert summary.cleanup_recovered == 1
    assert second_summary.uploads_recovered == 0
    assert second_summary.cleanup_recovered == 0
    assert uploader.calls == 0
    assert not local_path.exists()
    database.close()


def test_upload_worker_startup_recovery_finalizes_uploaded_job(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    uploaded = _uploaded_file(repository, local_path)
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    summary = asyncio.run(worker.recover_interrupted_jobs())
    second_summary = asyncio.run(worker.recover_interrupted_jobs())

    updated = repository.get_file_record(uploaded.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_COMPLETED
    assert summary.uploads_recovered == 0
    assert summary.cleanup_recovered == 1
    assert second_summary.uploads_recovered == 0
    assert second_summary.cleanup_recovered == 0
    assert uploader.calls == 0
    assert not local_path.exists()
    database.close()


def test_upload_worker_startup_recovery_does_not_start_ready_upload(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    local_path = tmp_path / "example-1.txt"
    local_path.write_text("hello", encoding="utf-8")
    ready = _ready_file(repository, local_path=local_path)
    uploader = FakeUploader()
    worker = UploadWorker(repository, logging.getLogger("test.upload_worker"), uploader)

    summary = asyncio.run(worker.recover_interrupted_jobs())
    second_summary = asyncio.run(worker.recover_interrupted_jobs())

    updated = repository.get_file_record(ready.id)
    assert updated is not None
    assert updated.status == FILE_STATUS_READY_FOR_UPLOAD
    assert summary.uploads_recovered == 0
    assert summary.cleanup_recovered == 0
    assert second_summary.uploads_recovered == 0
    assert second_summary.cleanup_recovered == 0
    assert uploader.calls == 0
    assert local_path.exists()
    database.close()


def _ready_file(
    repository: DatabaseRepository,
    message_id: int = 1,
    local_path: Path | None = None,
    status_chat_id: int | None = None,
    status_message_id: int | None = None,
) -> FileRecord:
    user = repository.create_user(telegram_user_id=message_id, username=None, first_name=None)
    expected_size = local_path.stat().st_size if local_path is not None else 12
    file_record = repository.create_file_record(
        user_id=user.id,
        metadata=FileMetadata(
            telegram_file_id=f"telegram-file-id-{message_id}",
            message_id=message_id,
            chat_id=789,
            forward_origin_chat_id=None,
            forward_origin_message_id=None,
            original_name=f"example-{message_id}.txt",
            mime_type="text/plain",
            size=expected_size,
            extension=".txt",
            file_type=TelegramFileType.DOCUMENT,
            created_at="2026-07-24T00:00:00+00:00",
        ),
    )
    if local_path is not None:
        repository.save_download(
            file_record.id,
            local_path=str(local_path),
            total_bytes=local_path.stat().st_size,
            status_chat_id=status_chat_id,
            status_message_id=status_message_id,
        )
    repository.set_file_destination_folder(
        file_record.id,
        Folder(
            id="folder-id",
            name="Uploads",
            parent_id="root",
            drive_id=None,
            path="My Drive/Uploads",
            is_shared=False,
            created_at="2026-07-24T00:00:00Z",
        ),
    )
    ready = repository.mark_file_ready_for_upload(file_record.id)
    if ready is None:
        raise RuntimeError("File record was not available after marking ready.")
    return ready


def _uploaded_file(
    repository: DatabaseRepository,
    local_path: Path,
    message_id: int = 1,
) -> FileRecord:
    if not local_path.exists():
        local_path.write_text("hello", encoding="utf-8")
    ready = _ready_file(repository, message_id=message_id, local_path=local_path)
    uploading = repository.transition_file_state(ready.id, JobState.UPLOADING)
    if uploading is None:
        raise RuntimeError("File record was not available after marking uploading.")
    uploaded = repository.transition_file_state(
        uploading.id,
        JobState.UPLOADED,
        google_drive_file_id="drive-file-id",
    )
    if uploaded is None:
        raise RuntimeError("File record was not available after marking uploaded.")
    return uploaded
