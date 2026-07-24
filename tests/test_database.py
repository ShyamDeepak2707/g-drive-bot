from __future__ import annotations

from pathlib import Path

from app.constants import FILE_STATUS_COMPLETED, FILE_STATUS_RECEIVED
from app.database import DatabaseRepository, SQLiteDatabase
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
            original_name="example.txt",
            mime_type="text/plain",
            size=12,
            extension=".txt",
            file_type=TelegramFileType.DOCUMENT,
            created_at="2026-07-24T00:00:00+00:00",
        ),
    )
    assert file_record.status == FILE_STATUS_RECEIVED
    assert file_record.message_id == 456
    assert file_record.file_type == TelegramFileType.DOCUMENT.value

    download = repository.save_download(file_record.id, total_bytes=12)
    assert download.file_id == file_record.id

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

    database.close()
