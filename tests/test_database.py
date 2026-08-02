from __future__ import annotations

from pathlib import Path

from app.constants import (
    FILE_STATUS_AWAITING_FOLDER,
    FILE_STATUS_COMPLETED,
    FILE_STATUS_QUEUED,
    FILE_STATUS_RECEIVED,
    FILE_STATUS_UPLOADED,
    FILE_STATUS_UPLOADING,
)
from app.database import DatabaseRepository, SQLiteDatabase
from app.job_state import JobState
from app.models import FileMetadata, TelegramFileType


def test_database_initialization_and_repository(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)

    user = repository.create_user(
        telegram_user_id=123,
        username="samuel",
        first_name="Samuel",
    )
    assert repository.get_user(123) == user

    file_record = repository.create_file_record(
        user_id=user.id,
        metadata=FileMetadata(
            telegram_file_id="telegram-file-id",
            message_id=456,
            chat_id=789,
            forward_origin_chat_id=-100123,
            forward_origin_message_id=99,
            original_name="example.txt",
            mime_type="text/plain",
            size=12,
            extension=".txt",
            file_type=TelegramFileType.DOCUMENT,
            created_at="2026-07-24T00:00:00+00:00",
            telegram_file_unique_id="unique-file-id",
        ),
    )
    assert file_record.status == FILE_STATUS_RECEIVED
    assert file_record.telegram_file_unique_id == "unique-file-id"
    assert file_record.source_url is None
    assert file_record.message_id == 456
    assert file_record.forward_origin_chat_id == -100123
    assert file_record.forward_origin_message_id == 99
    assert file_record.file_type == TelegramFileType.DOCUMENT.value

    download = repository.save_download(
        file_record.id,
        total_bytes=12,
        status_chat_id=789,
        status_message_id=55,
    )
    assert download.file_id == file_record.id
    assert download.status_chat_id == 789
    assert download.status_message_id == 55

    interrupted = repository.list_interrupted_downloads()
    assert len(interrupted) == 1
    assert interrupted[0].file.id == file_record.id
    recovered = repository.requeue_interrupted_download(file_record.id)
    assert recovered is not None
    assert recovered.status == "queued"
    queued_file = repository.get_file_record(file_record.id)
    assert queued_file is not None
    assert queued_file.status == FILE_STATUS_QUEUED

    progress = repository.update_download_progress(
        file_record.id, bytes_downloaded=6, total_bytes=12
    )
    assert progress is not None
    assert progress.progress_percent == 50

    completed = repository.mark_download_complete(file_record.id, "downloads/example.txt", 12)
    assert completed is not None
    assert completed.progress_percent == 100

    updated = repository.update_file_status(file_record.id, FILE_STATUS_COMPLETED)
    assert updated is not None
    assert updated.status == FILE_STATUS_COMPLETED

    completed_upload = repository.mark_file_completed(file_record.id, "drive-file-id")
    assert completed_upload is not None
    assert completed_upload.google_drive_file_id == "drive-file-id"
    assert completed_upload.status == FILE_STATUS_COMPLETED

    database.close()


def test_repository_upload_state_transitions_are_persisted(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=123, username=None, first_name=None)
    file_record = repository.create_file_record(
        user_id=user.id,
        metadata=FileMetadata(
            telegram_file_id="telegram-file-id",
            message_id=456,
            chat_id=789,
            forward_origin_chat_id=None,
            forward_origin_message_id=None,
            original_name="example.txt",
            mime_type="text/plain",
            size=12,
            extension=".txt",
            file_type=TelegramFileType.DOCUMENT,
            created_at="2026-07-24T00:00:00+00:00",
        ),
    )

    repository.transition_file_state(file_record.id, JobState.QUEUED)
    repository.transition_file_state(file_record.id, JobState.DOWNLOADING)
    repository.transition_file_state(file_record.id, JobState.DOWNLOADED)
    repository.transition_file_state(file_record.id, JobState.READY_FOR_UPLOAD)

    uploading = repository.mark_file_uploading(file_record.id)
    assert uploading is not None
    assert uploading.status == FILE_STATUS_UPLOADING

    uploaded = repository.mark_file_uploaded(file_record.id, "drive-file-id")
    assert uploaded is not None
    assert uploaded.status == FILE_STATUS_UPLOADED
    assert uploaded.google_drive_file_id == "drive-file-id"

    completed = repository.mark_file_completed(file_record.id, "drive-file-id")
    assert completed is not None
    assert completed.status == FILE_STATUS_COMPLETED
    assert completed.google_drive_file_id == "drive-file-id"

    database.close()


def test_repository_finds_older_incomplete_file(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=123, username=None, first_name=None)
    older = repository.create_file_record(
        user_id=user.id,
        metadata=_metadata(message_id=1),
    )
    newer = repository.create_file_record(
        user_id=user.id,
        metadata=_metadata(message_id=2),
    )
    repository.update_file_status(older.id, FILE_STATUS_AWAITING_FOLDER)

    blocking = repository.get_oldest_incomplete_file_before(newer.id)

    assert blocking is not None
    assert blocking.id == older.id

    repository.mark_file_completed(older.id, "drive-file-id")

    assert repository.get_oldest_incomplete_file_before(newer.id) is None
    database.close()


def _metadata(message_id: int) -> FileMetadata:
    return FileMetadata(
        telegram_file_id=f"telegram-file-id-{message_id}",
        message_id=message_id,
        chat_id=789,
        forward_origin_chat_id=None,
        forward_origin_message_id=None,
        original_name=f"example-{message_id}.txt",
        mime_type="text/plain",
        size=12,
        extension=".txt",
        file_type=TelegramFileType.DOCUMENT,
        created_at="2026-07-24T00:00:00+00:00",
    )
