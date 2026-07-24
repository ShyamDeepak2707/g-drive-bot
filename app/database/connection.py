from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from app.constants import FILE_STATUS_PENDING
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
    google_drive_file_id TEXT,
    original_name TEXT,
    mime_type TEXT,
    status TEXT NOT NULL DEFAULT '{FILE_STATUS_PENDING}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


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
