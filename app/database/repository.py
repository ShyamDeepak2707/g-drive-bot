from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.constants import FILE_STATUS_PENDING
from app.database.connection import SQLiteDatabase


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
    google_drive_file_id: str | None
    original_name: str | None
    mime_type: str | None
    status: str
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
        telegram_file_id: str,
        original_name: str | None = None,
        mime_type: str | None = None,
    ) -> FileRecord:
        cursor = self._database.execute(
            """
            INSERT INTO files (user_id, telegram_file_id, original_name, mime_type, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, telegram_file_id, original_name, mime_type, FILE_STATUS_PENDING),
        )
        self._database.commit()
        row = self._database.fetch_one(
            """
            SELECT id, user_id, telegram_file_id, google_drive_file_id, original_name,
                   mime_type, status, created_at, updated_at
            FROM files
            WHERE id = ?
            """,
            (cursor.lastrowid,),
        )
        if row is None:
            raise RuntimeError("File record was not available after insert.")
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
            """
            SELECT id, user_id, telegram_file_id, google_drive_file_id, original_name,
                   mime_type, status, created_at, updated_at
            FROM files
            WHERE id = ?
            """,
            (file_id,),
        )
        if row is None:
            return None
        return _file_record_from_row(row)


def _file_record_from_row(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        telegram_file_id=row["telegram_file_id"],
        google_drive_file_id=row["google_drive_file_id"],
        original_name=row["original_name"],
        mime_type=row["mime_type"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
