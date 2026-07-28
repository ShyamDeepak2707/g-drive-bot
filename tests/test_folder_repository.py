from __future__ import annotations

from pathlib import Path

from app.constants import FILE_STATUS_READY_FOR_UPLOAD
from app.database import DatabaseRepository, SQLiteDatabase
from app.models import FileMetadata, Folder, TelegramFileType


def test_folder_repository_favorites_recent_last_and_destination(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username="sam", first_name="Sam")
    file_record = repository.create_file_record(
        user.id,
        FileMetadata(
            telegram_file_id="telegram-file-id",
            message_id=10,
            chat_id=20,
            forward_origin_chat_id=None,
            forward_origin_message_id=None,
            original_name="example.txt",
            mime_type="text/plain",
            size=100,
            extension=".txt",
            file_type=TelegramFileType.DOCUMENT,
            created_at="2026-07-24T00:00:00+00:00",
        ),
    )
    uploads = _folder("folder-1", "Uploads", "My Drive/Uploads")
    archive = _folder("folder-2", "Archive", "My Drive/Archive")
    reports = _folder("folder-3", "Reports", "My Drive/Reports")

    repository.add_favorite_folder(user.id, uploads)
    repository.add_favorite_folder(user.id, archive)
    assert [folder.name for folder in repository.list_favorite_folders(user.id)] == [
        "Archive",
        "Uploads",
    ]

    assert repository.remove_favorite_folder(user.id, archive.id)
    assert repository.list_favorite_folders(user.id) == [uploads]

    repository.record_recent_folder(user.id, uploads, limit=2)
    repository.record_recent_folder(user.id, archive, limit=2)
    repository.record_recent_folder(user.id, reports, limit=2)
    assert [folder.id for folder in repository.list_recent_folders(user.id, limit=5)] == [
        reports.id,
        archive.id,
    ]

    repository.set_last_folder(user.id, uploads)
    assert repository.get_last_folder(user.id) == uploads

    updated = repository.set_file_destination_folder(file_record.id, uploads)
    assert updated is not None
    assert updated.destination_folder_id == uploads.id
    assert updated.destination_folder_path == uploads.path

    ready = repository.mark_file_ready_for_upload(file_record.id)
    assert ready is not None
    assert ready.status == FILE_STATUS_READY_FOR_UPLOAD
    assert ready.destination_folder_id == uploads.id

    database.close()


def _folder(folder_id: str, name: str, path: str) -> Folder:
    return Folder(
        id=folder_id,
        name=name,
        parent_id="root",
        drive_id=None,
        path=path,
        is_shared=False,
        created_at="2026-07-24T00:00:00.000Z",
    )
