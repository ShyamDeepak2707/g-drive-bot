from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.constants import (
    DOWNLOAD_STATUS_CANCELLED,
    DOWNLOAD_STATUS_COMPLETED,
    DOWNLOAD_STATUS_FAILED,
    DOWNLOAD_STATUS_QUEUED,
    DOWNLOAD_STATUS_RUNNING,
    FILE_STATUS_AWAITING_RENAME,
    FILE_STATUS_CANCELLED,
    FILE_STATUS_DOWNLOADED,
    FILE_STATUS_DOWNLOADING,
    FILE_STATUS_FAILED,
    FILE_STATUS_QUEUED,
    FILE_STATUS_READY_FOR_UPLOAD,
    FILE_STATUS_RECEIVED,
    FILE_STATUS_SKIPPED,
)
from app.database.connection import SQLiteDatabase
from app.models import FileMetadata


@dataclass(frozen=True)
class UserRecord:
    id: int
    telegram_user_id: int
    username: str | None
    first_name: str | None
    created_at: str


@dataclass(frozen=True)
class FileRecord:
    id: int
    user_id: int
    telegram_file_id: str
    message_id: int | None
    chat_id: int | None
    google_drive_file_id: str | None
    original_name: str | None
    mime_type: str | None
    size: int | None
    extension: str | None
    file_type: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DownloadRecord:
    id: int
    file_id: int
    local_path: str | None
    temp_path: str | None
    bytes_downloaded: int
    total_bytes: int | None
    progress_percent: int
    status: str
    error_message: str | None
    retry_count: int
    created_at: str
    updated_at: str


class DatabaseRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def create_user(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
    ) -> UserRecord:
        self._database.execute(
            """
            INSERT INTO users (telegram_user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (telegram_user_id, username, first_name),
        )
        self._database.commit()
        user = self.get_user(telegram_user_id)
        if user is None:
            raise RuntimeError("User was not available after upsert.")
        return user

    def get_user(self, telegram_user_id: int) -> UserRecord | None:
        row = self._database.fetch_one(
            """
            SELECT id, telegram_user_id, username, first_name, created_at
            FROM users
            WHERE telegram_user_id = ?
            """,
            (telegram_user_id,),
        )
        if row is None:
            return None
        return UserRecord(
            id=int(row["id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            username=row["username"],
            first_name=row["first_name"],
            created_at=row["created_at"],
        )

    def create_file_record(
        self,
        user_id: int,
        metadata: FileMetadata,
    ) -> FileRecord:
        cursor = self._database.execute(
            """
            INSERT INTO files (
                user_id, telegram_file_id, message_id, chat_id, original_name,
                mime_type, size, extension, file_type, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                metadata.telegram_file_id,
                metadata.message_id,
                metadata.chat_id,
                metadata.original_name,
                metadata.mime_type,
                metadata.size,
                metadata.extension,
                metadata.file_type.value,
                FILE_STATUS_RECEIVED,
            ),
        )
        self._database.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a file record id.")
        file_record = self.get_file_record(int(cursor.lastrowid))
        if file_record is None:
            raise RuntimeError("File record was not available after insert.")
        return file_record

    def get_file_record(self, file_id: int) -> FileRecord | None:
        row = self._database.fetch_one(
            _FILE_SELECT_SQL + " WHERE id = ?",
            (file_id,),
        )
        if row is None:
            return None
        return _file_record_from_row(row)

    def update_file_status(
        self,
        file_id: int,
        status: str,
        google_drive_file_id: str | None = None,
    ) -> FileRecord | None:
        self._database.execute(
            """
            UPDATE files
            SET status = ?,
                google_drive_file_id = COALESCE(?, google_drive_file_id),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, google_drive_file_id, file_id),
        )
        self._database.commit()
        row = self._database.fetch_one(
            _FILE_SELECT_SQL + " WHERE id = ?",
            (file_id,),
        )
        if row is None:
            return None
        return _file_record_from_row(row)

    def mark_file_queued(self, file_id: int) -> FileRecord | None:
        return self.update_file_status(file_id, FILE_STATUS_QUEUED)

    def mark_file_downloading(self, file_id: int) -> FileRecord | None:
        return self.update_file_status(file_id, FILE_STATUS_DOWNLOADING)

    def mark_file_awaiting_rename(self, file_id: int) -> FileRecord | None:
        return self.update_file_status(file_id, FILE_STATUS_AWAITING_RENAME)

    def mark_file_ready_for_upload(
        self,
        file_id: int,
        original_name: str | None = None,
    ) -> FileRecord | None:
        if original_name is not None:
            self._database.execute(
                """
                UPDATE files
                SET original_name = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (original_name, FILE_STATUS_READY_FOR_UPLOAD, file_id),
            )
            self._database.commit()
            return self.get_file_record(file_id)
        return self.update_file_status(file_id, FILE_STATUS_READY_FOR_UPLOAD)

    def mark_file_skipped(self, file_id: int) -> FileRecord | None:
        return self.update_file_status(file_id, FILE_STATUS_SKIPPED)

    def save_download(
        self,
        file_id: int,
        local_path: str | None = None,
        temp_path: str | None = None,
        total_bytes: int | None = None,
    ) -> DownloadRecord:
        self._database.execute(
            """
            INSERT INTO downloads (file_id, local_path, temp_path, total_bytes, status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                local_path = excluded.local_path,
                temp_path = excluded.temp_path,
                total_bytes = excluded.total_bytes,
                status = excluded.status,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (file_id, local_path, temp_path, total_bytes, DOWNLOAD_STATUS_QUEUED),
        )
        self._database.commit()
        download = self.get_download(file_id)
        if download is None:
            raise RuntimeError("Download was not available after upsert.")
        return download

    def get_download(self, file_id: int) -> DownloadRecord | None:
        row = self._database.fetch_one(
            """
            SELECT id, file_id, local_path, temp_path, bytes_downloaded, total_bytes,
                   progress_percent, status, error_message, retry_count, created_at, updated_at
            FROM downloads
            WHERE file_id = ?
            """,
            (file_id,),
        )
        if row is None:
            return None
        return _download_record_from_row(row)

    def update_download_progress(
        self,
        file_id: int,
        bytes_downloaded: int,
        total_bytes: int | None,
    ) -> DownloadRecord | None:
        progress_percent = int((bytes_downloaded / total_bytes) * 100) if total_bytes else 0
        self._database.execute(
            """
            UPDATE downloads
            SET bytes_downloaded = ?,
                total_bytes = COALESCE(?, total_bytes),
                progress_percent = ?,
                status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE file_id = ?
            """,
            (
                bytes_downloaded,
                total_bytes,
                min(100, progress_percent),
                DOWNLOAD_STATUS_RUNNING,
                file_id,
            ),
        )
        self._database.commit()
        return self.get_download(file_id)

    def mark_download_complete(
        self,
        file_id: int,
        local_path: str,
        total_bytes: int | None,
    ) -> DownloadRecord | None:
        self._database.execute(
            """
            UPDATE downloads
            SET local_path = ?,
                temp_path = NULL,
                bytes_downloaded = COALESCE(?, bytes_downloaded),
                total_bytes = COALESCE(?, total_bytes),
                progress_percent = 100,
                status = ?,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE file_id = ?
            """,
            (local_path, total_bytes, total_bytes, DOWNLOAD_STATUS_COMPLETED, file_id),
        )
        self._database.commit()
        self.update_file_status(file_id, FILE_STATUS_DOWNLOADED)
        return self.get_download(file_id)

    def update_download_path(self, file_id: int, local_path: str) -> DownloadRecord | None:
        self._database.execute(
            """
            UPDATE downloads
            SET local_path = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE file_id = ?
            """,
            (local_path, file_id),
        )
        self._database.commit()
        return self.get_download(file_id)

    def mark_download_failed(
        self,
        file_id: int,
        error_message: str,
        retry_count: int,
    ) -> DownloadRecord | None:
        self._database.execute(
            """
            UPDATE downloads
            SET status = ?,
                error_message = ?,
                retry_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE file_id = ?
            """,
            (DOWNLOAD_STATUS_FAILED, error_message, retry_count, file_id),
        )
        self._database.commit()
        self.update_file_status(file_id, FILE_STATUS_FAILED)
        return self.get_download(file_id)

    def mark_download_cancelled(
        self,
        file_id: int,
        retry_count: int,
    ) -> DownloadRecord | None:
        self._database.execute(
            """
            UPDATE downloads
            SET status = ?,
                error_message = ?,
                retry_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE file_id = ?
            """,
            (DOWNLOAD_STATUS_CANCELLED, "Download cancelled.", retry_count, file_id),
        )
        self._database.commit()
        self.update_file_status(file_id, FILE_STATUS_CANCELLED)
        return self.get_download(file_id)


_FILE_SELECT_SQL = """
SELECT id, user_id, telegram_file_id, message_id, chat_id, google_drive_file_id,
       original_name, mime_type, size, extension, file_type, status, created_at, updated_at
FROM files
"""


def _file_record_from_row(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        telegram_file_id=row["telegram_file_id"],
        message_id=row["message_id"],
        chat_id=row["chat_id"],
        google_drive_file_id=row["google_drive_file_id"],
        original_name=row["original_name"],
        mime_type=row["mime_type"],
        size=row["size"],
        extension=row["extension"],
        file_type=row["file_type"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _download_record_from_row(row: sqlite3.Row) -> DownloadRecord:
    return DownloadRecord(
        id=int(row["id"]),
        file_id=int(row["file_id"]),
        local_path=row["local_path"],
        temp_path=row["temp_path"],
        bytes_downloaded=int(row["bytes_downloaded"]),
        total_bytes=row["total_bytes"],
        progress_percent=int(row["progress_percent"]),
        status=row["status"],
        error_message=row["error_message"],
        retry_count=int(row["retry_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
