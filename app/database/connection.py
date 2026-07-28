from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from app.constants import (
    DOWNLOAD_STATUS_QUEUED,
    FILE_STATUS_RECEIVED,
)
from app.exceptions import DatabaseError
from app.logging_config import get_logger
from app.utils.filesystem import ensure_parent_directory

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    telegram_file_id TEXT NOT NULL,
    message_id INTEGER,
    chat_id INTEGER,
    forward_origin_chat_id INTEGER,
    forward_origin_message_id INTEGER,
    google_drive_file_id TEXT,
    upload_retry_count INTEGER NOT NULL DEFAULT 0,
    upload_retry_after TEXT,
    upload_error_message TEXT,
    destination_folder_id TEXT,
    destination_folder_name TEXT,
    destination_folder_path TEXT,
    destination_drive_id TEXT,
    destination_is_shared INTEGER NOT NULL DEFAULT 0,
    original_name TEXT,
    mime_type TEXT,
    size INTEGER,
    extension TEXT,
    file_type TEXT,
    status TEXT NOT NULL DEFAULT '{FILE_STATUS_RECEIVED}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL UNIQUE,
    local_path TEXT,
    temp_path TEXT,
    status_chat_id INTEGER,
    status_message_id INTEGER,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER,
    progress_percent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '{DOWNLOAD_STATUS_QUEUED}',
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS folder_favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    folder_id TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    drive_id TEXT,
    path TEXT NOT NULL,
    is_shared INTEGER NOT NULL DEFAULT 0,
    folder_created_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, folder_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS recent_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    folder_id TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    drive_id TEXT,
    path TEXT NOT NULL,
    is_shared INTEGER NOT NULL DEFAULT 0,
    folder_created_at TEXT,
    used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, folder_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_folder_preferences (
    user_id INTEGER PRIMARY KEY,
    last_folder_id TEXT,
    last_folder_name TEXT,
    last_folder_parent_id TEXT,
    last_folder_drive_id TEXT,
    last_folder_path TEXT,
    last_folder_is_shared INTEGER NOT NULL DEFAULT 0,
    last_folder_created_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""

FILE_COLUMN_MIGRATIONS = {
    "message_id": "ALTER TABLE files ADD COLUMN message_id INTEGER",
    "chat_id": "ALTER TABLE files ADD COLUMN chat_id INTEGER",
    "forward_origin_chat_id": "ALTER TABLE files ADD COLUMN forward_origin_chat_id INTEGER",
    "forward_origin_message_id": ("ALTER TABLE files ADD COLUMN forward_origin_message_id INTEGER"),
    "upload_retry_count": (
        "ALTER TABLE files ADD COLUMN upload_retry_count INTEGER NOT NULL DEFAULT 0"
    ),
    "upload_retry_after": "ALTER TABLE files ADD COLUMN upload_retry_after TEXT",
    "upload_error_message": "ALTER TABLE files ADD COLUMN upload_error_message TEXT",
    "destination_folder_id": "ALTER TABLE files ADD COLUMN destination_folder_id TEXT",
    "destination_folder_name": "ALTER TABLE files ADD COLUMN destination_folder_name TEXT",
    "destination_folder_path": "ALTER TABLE files ADD COLUMN destination_folder_path TEXT",
    "destination_drive_id": "ALTER TABLE files ADD COLUMN destination_drive_id TEXT",
    "destination_is_shared": (
        "ALTER TABLE files ADD COLUMN destination_is_shared INTEGER NOT NULL DEFAULT 0"
    ),
    "size": "ALTER TABLE files ADD COLUMN size INTEGER",
    "extension": "ALTER TABLE files ADD COLUMN extension TEXT",
    "file_type": "ALTER TABLE files ADD COLUMN file_type TEXT",
}

DOWNLOAD_COLUMN_MIGRATIONS = {
    "status_chat_id": "ALTER TABLE downloads ADD COLUMN status_chat_id INTEGER",
    "status_message_id": "ALTER TABLE downloads ADD COLUMN status_message_id INTEGER",
}


class SQLiteDatabase:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._connection: sqlite3.Connection | None = None
        self._logger = get_logger(__name__)

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    def connect(self) -> None:
        if self._connection is not None:
            return
        ensure_parent_directory(self.db_path)
        try:
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            self._connection = connection
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to connect to SQLite database at '{self.db_path}'."
            ) from exc

    def initialize(self) -> None:
        self.connect()
        self.executescript(SCHEMA)
        self._apply_migrations()
        self.commit()
        self._logger.info("database initialized", extra={"event": "database_initialized"})

    def close(self) -> None:
        if self._connection is None:
            return
        self._connection.close()
        self._connection = None
        self._logger.info("database connection closed", extra={"event": "database_closed"})

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        connection = self._require_connection()
        try:
            return connection.execute(sql, parameters)
        except sqlite3.Error as exc:
            raise DatabaseError("SQLite execute failed.") from exc

    def fetch_one(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Row | None:
        return self.execute(sql, parameters).fetchone()

    def fetch_all(self, sql: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        return list(self.execute(sql, parameters).fetchall())

    def executescript(self, sql: str) -> None:
        connection = self._require_connection()
        try:
            connection.executescript(sql)
        except sqlite3.Error as exc:
            raise DatabaseError("SQLite schema initialization failed.") from exc

    def commit(self) -> None:
        connection = self._require_connection()
        try:
            connection.commit()
        except sqlite3.Error as exc:
            raise DatabaseError("SQLite commit failed.") from exc

    def rollback(self) -> None:
        connection = self._require_connection()
        try:
            connection.rollback()
        except sqlite3.Error as exc:
            raise DatabaseError("SQLite rollback failed.") from exc

    def __enter__(self) -> SQLiteDatabase:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseError("SQLite database is not connected.")
        return self._connection

    def _apply_migrations(self) -> None:
        existing_columns = {str(row["name"]) for row in self.fetch_all("PRAGMA table_info(files)")}
        for column, migration_sql in FILE_COLUMN_MIGRATIONS.items():
            if column not in existing_columns:
                self.execute(migration_sql)
        existing_download_columns = {
            str(row["name"]) for row in self.fetch_all("PRAGMA table_info(downloads)")
        }
        for column, migration_sql in DOWNLOAD_COLUMN_MIGRATIONS.items():
            if column not in existing_download_columns:
                self.execute(migration_sql)
