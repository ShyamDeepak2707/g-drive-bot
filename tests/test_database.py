from __future__ import annotations

from pathlib import Path

from app.constants import FILE_STATUS_COMPLETED, FILE_STATUS_PENDING
from app.database import DatabaseRepository, SQLiteDatabase


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
        telegram_file_id="telegram-file-id",
        original_name="example.txt",
        mime_type="text/plain",
    )
    assert file_record.status == FILE_STATUS_PENDING

    updated = repository.update_file_status(file_record.id, FILE_STATUS_COMPLETED)
    assert updated is not None
    assert updated.status == FILE_STATUS_COMPLETED

    database.close()
