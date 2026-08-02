from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.constants import (
    DOWNLOAD_STATUS_CANCELLED,
    DOWNLOAD_STATUS_COMPLETED,
    DOWNLOAD_STATUS_FAILED,
    DOWNLOAD_STATUS_QUEUED,
    DOWNLOAD_STATUS_RUNNING,
    FILE_STATUS_AWAITING_FOLDER,
    FILE_STATUS_AWAITING_RENAME,
    FILE_STATUS_CANCELLED,
    FILE_STATUS_COMPLETED,
    FILE_STATUS_DOWNLOADED,
    FILE_STATUS_DOWNLOADING,
    FILE_STATUS_FAILED,
    FILE_STATUS_QUEUED,
    FILE_STATUS_READY_FOR_UPLOAD,
    FILE_STATUS_RECEIVED,
    FILE_STATUS_SKIPPED,
    FILE_STATUS_UPLOADED,
    FILE_STATUS_UPLOADING,
)
from app.database.connection import SQLiteDatabase
from app.job_state import JobState, require_transition
from app.logging_config import get_logger
from app.models import FileMetadata, Folder


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
    telegram_file_unique_id: str | None
    source_url: str | None
    message_id: int | None
    chat_id: int | None
    forward_origin_chat_id: int | None
    forward_origin_message_id: int | None
    google_drive_file_id: str | None
    upload_retry_count: int
    upload_retry_after: str | None
    upload_error_message: str | None
    destination_folder_id: str | None
    destination_folder_name: str | None
    destination_folder_path: str | None
    destination_drive_id: str | None
    destination_is_shared: bool
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
    status_chat_id: int | None
    status_message_id: int | None
    bytes_downloaded: int
    total_bytes: int | None
    progress_percent: int
    status: str
    error_message: str | None
    retry_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class InterruptedDownloadRecord:
    file: FileRecord
    download: DownloadRecord


@dataclass(frozen=True)
class RepositoryStatistics:
    total_tracked_files: int
    completed_downloads: int
    completed_uploads: int
    failed_downloads: int
    failed_uploads: int
    download_retry_attempts: int
    upload_retry_attempts: int
    scheduled_upload_retries: int


class DatabaseRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._logger = get_logger(__name__)

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
        self._logger.info(
            "creating media file record",
            extra={
                "event": "media_metadata_repository_create_trace",
                "user_id": user_id,
                "metadata_message_id": metadata.message_id,
                "metadata_chat_id": metadata.chat_id,
                "forward_origin_chat_id": metadata.forward_origin_chat_id,
                "forward_origin_message_id": metadata.forward_origin_message_id,
                "file_type": metadata.file_type.value,
                "original_name": metadata.original_name,
                "size": metadata.size,
            },
        )
        cursor = self._database.execute(
            """
            INSERT INTO files (
                user_id, telegram_file_id, telegram_file_unique_id, source_url, message_id, chat_id,
                forward_origin_chat_id, forward_origin_message_id, original_name,
                mime_type, size, extension, file_type, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                metadata.telegram_file_id,
                metadata.telegram_file_unique_id,
                metadata.source_url,
                metadata.message_id,
                metadata.chat_id,
                metadata.forward_origin_chat_id,
                metadata.forward_origin_message_id,
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
        self._logger.info(
            "created media file record",
            extra={
                "event": "media_metadata_repository_created_trace",
                "file_record_id": file_record.id,
                "metadata_message_id": metadata.message_id,
                "persisted_message_id": file_record.message_id,
                "metadata_chat_id": metadata.chat_id,
                "persisted_chat_id": file_record.chat_id,
                "file_type": file_record.file_type,
                "original_name": file_record.original_name,
                "size": file_record.size,
            },
        )
        return file_record

    def get_file_record(self, file_id: int) -> FileRecord | None:
        row = self._database.fetch_one(
            _FILE_SELECT_SQL + " WHERE id = ?",
            (file_id,),
        )
        if row is None:
            return None
        return _file_record_from_row(row)

    def get_next_file_by_status(self, status: str) -> FileRecord | None:
        row = self._database.fetch_one(
            _FILE_SELECT_SQL + " WHERE status = ? ORDER BY updated_at ASC, id ASC LIMIT 1",
            (status,),
        )
        if row is None:
            return None
        return _file_record_from_row(row)

    def get_oldest_incomplete_file_before(self, file_id: int) -> FileRecord | None:
        row = self._database.fetch_one(
            _FILE_SELECT_SQL + """
            WHERE id < ?
              AND status NOT IN (?, ?, ?, ?)
            ORDER BY id ASC
            LIMIT 1
            """,
            (
                file_id,
                FILE_STATUS_COMPLETED,
                FILE_STATUS_FAILED,
                FILE_STATUS_CANCELLED,
                FILE_STATUS_SKIPPED,
            ),
        )
        if row is None:
            return None
        return _file_record_from_row(row)

    def get_next_ready_for_upload(
        self,
        excluded_file_ids: set[int] | None = None,
    ) -> FileRecord | None:
        excluded_clause, excluded_parameters = _excluded_file_ids_clause(excluded_file_ids)
        row = self._database.fetch_one(
            _FILE_SELECT_SQL + f"""
            WHERE status = ?
              AND google_drive_file_id IS NULL
              AND (upload_retry_after IS NULL OR upload_retry_after <= CURRENT_TIMESTAMP)
              {excluded_clause}
            ORDER BY upload_retry_after ASC, updated_at ASC, id ASC
            LIMIT 1
            """,
            (FILE_STATUS_READY_FOR_UPLOAD, *excluded_parameters),
        )
        if row is None:
            return None
        return _file_record_from_row(row)

    def get_next_uploaded(self, excluded_file_ids: set[int] | None = None) -> FileRecord | None:
        excluded_clause, excluded_parameters = _excluded_file_ids_clause(excluded_file_ids)
        row = self._database.fetch_one(
            _FILE_SELECT_SQL + f"""
            WHERE status = ?{excluded_clause}
            ORDER BY updated_at ASC, id ASC
            LIMIT 1
            """,
            (FILE_STATUS_UPLOADED, *excluded_parameters),
        )
        if row is None:
            return None
        return _file_record_from_row(row)

    def get_next_uploading_with_drive_file_id(
        self,
        excluded_file_ids: set[int] | None = None,
    ) -> FileRecord | None:
        excluded_clause, excluded_parameters = _excluded_file_ids_clause(excluded_file_ids)
        row = self._database.fetch_one(
            _FILE_SELECT_SQL + f"""
            WHERE status = ? AND google_drive_file_id IS NOT NULL{excluded_clause}
            ORDER BY updated_at ASC, id ASC
            LIMIT 1
            """,
            (FILE_STATUS_UPLOADING, *excluded_parameters),
        )
        if row is None:
            return None
        return _file_record_from_row(row)

    def count_pending_uploads(self) -> int:
        row = self._database.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM files
            WHERE status = ?
              AND google_drive_file_id IS NULL
              AND (upload_retry_after IS NULL OR upload_retry_after <= CURRENT_TIMESTAMP)
            """,
            (FILE_STATUS_READY_FOR_UPLOAD,),
        )
        if row is None:
            return 0
        return int(row["count"])

    def list_failed_file_records(self, limit: int = 20) -> tuple[FileRecord, ...]:
        row_limit = max(0, limit)
        rows = self._database.fetch_all(
            _FILE_SELECT_SQL + """
            WHERE status = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (FILE_STATUS_FAILED, row_limit),
        )
        return tuple(_file_record_from_row(row) for row in rows)

    def runtime_statistics(self) -> RepositoryStatistics:
        row = self._database.fetch_one(
            """
            SELECT
                (SELECT COUNT(*) FROM files) AS total_tracked_files,
                (
                    SELECT COUNT(*)
                    FROM downloads
                    WHERE status = ?
                ) AS completed_downloads,
                (
                    SELECT COUNT(*)
                    FROM files
                    WHERE status IN (?, ?)
                      AND google_drive_file_id IS NOT NULL
                ) AS completed_uploads,
                (
                    SELECT COUNT(*)
                    FROM downloads
                    WHERE status = ?
                ) AS failed_downloads,
                (
                    SELECT COUNT(*)
                    FROM files AS failed_files
                    WHERE failed_files.status = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM downloads AS failed_downloads
                          WHERE failed_downloads.file_id = failed_files.id
                            AND failed_downloads.status = ?
                      )
                ) AS failed_uploads,
                (
                    SELECT COALESCE(SUM(retry_count), 0)
                    FROM downloads
                ) AS download_retry_attempts,
                (
                    SELECT COALESCE(SUM(upload_retry_count), 0)
                    FROM files
                ) AS upload_retry_attempts,
                (
                    SELECT COUNT(*)
                    FROM files
                    WHERE status = ?
                      AND upload_retry_after IS NOT NULL
                ) AS scheduled_upload_retries
            """,
            (
                DOWNLOAD_STATUS_COMPLETED,
                FILE_STATUS_UPLOADED,
                FILE_STATUS_COMPLETED,
                DOWNLOAD_STATUS_FAILED,
                FILE_STATUS_FAILED,
                DOWNLOAD_STATUS_FAILED,
                FILE_STATUS_READY_FOR_UPLOAD,
            ),
        )
        if row is None:
            return RepositoryStatistics(
                total_tracked_files=0,
                completed_downloads=0,
                completed_uploads=0,
                failed_downloads=0,
                failed_uploads=0,
                download_retry_attempts=0,
                upload_retry_attempts=0,
                scheduled_upload_retries=0,
            )
        return RepositoryStatistics(
            total_tracked_files=int(row["total_tracked_files"]),
            completed_downloads=int(row["completed_downloads"]),
            completed_uploads=int(row["completed_uploads"]),
            failed_downloads=int(row["failed_downloads"]),
            failed_uploads=int(row["failed_uploads"]),
            download_retry_attempts=int(row["download_retry_attempts"]),
            upload_retry_attempts=int(row["upload_retry_attempts"]),
            scheduled_upload_retries=int(row["scheduled_upload_retries"]),
        )

    def list_download_records_with_paths(self) -> tuple[DownloadRecord, ...]:
        rows = self._database.fetch_all("""
            SELECT id, file_id, local_path, temp_path, status_chat_id, status_message_id,
                   bytes_downloaded, total_bytes, progress_percent, status, error_message,
                   retry_count, created_at, updated_at
            FROM downloads
            WHERE local_path IS NOT NULL OR temp_path IS NOT NULL
            ORDER BY updated_at DESC, id DESC
            """)
        return tuple(_download_record_from_row(row) for row in rows)

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

    def mark_file_awaiting_folder(self, file_id: int) -> FileRecord | None:
        return self.update_file_status(file_id, FILE_STATUS_AWAITING_FOLDER)

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

    def mark_file_uploading(self, file_id: int) -> FileRecord | None:
        return self.update_file_status(file_id, FILE_STATUS_UPLOADING)

    def mark_file_uploaded(self, file_id: int, google_drive_file_id: str) -> FileRecord | None:
        return self.update_file_status(
            file_id,
            FILE_STATUS_UPLOADED,
            google_drive_file_id=google_drive_file_id,
        )

    def mark_file_completed(self, file_id: int, google_drive_file_id: str) -> FileRecord | None:
        return self.update_file_status(
            file_id,
            FILE_STATUS_COMPLETED,
            google_drive_file_id=google_drive_file_id,
        )

    def transition_file_state(
        self,
        file_id: int,
        target: JobState,
        google_drive_file_id: str | None = None,
    ) -> FileRecord | None:
        file_record = self.get_file_record(file_id)
        if file_record is None:
            return None
        current = JobState(file_record.status)
        require_transition(current, target)
        return self.update_file_status(
            file_id,
            target.value,
            google_drive_file_id=google_drive_file_id,
        )

    def schedule_upload_retry(
        self,
        file_id: int,
        retry_count: int,
        retry_after: str,
        error_message: str,
    ) -> FileRecord | None:
        file_record = self.get_file_record(file_id)
        if file_record is None:
            return None
        require_transition(JobState(file_record.status), JobState.READY_FOR_UPLOAD)
        self._database.execute(
            """
            UPDATE files
            SET status = ?,
                upload_retry_count = ?,
                upload_retry_after = ?,
                upload_error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                FILE_STATUS_READY_FOR_UPLOAD,
                retry_count,
                retry_after,
                error_message,
                file_id,
            ),
        )
        self._database.commit()
        return self.get_file_record(file_id)

    def clear_upload_retry_schedule(self, file_id: int) -> FileRecord | None:
        self._database.execute(
            """
            UPDATE files
            SET upload_retry_after = NULL,
                upload_error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (file_id,),
        )
        self._database.commit()
        return self.get_file_record(file_id)

    def mark_upload_failed_permanently(
        self,
        file_id: int,
        error_message: str,
    ) -> FileRecord | None:
        file_record = self.get_file_record(file_id)
        if file_record is None:
            return None
        require_transition(JobState(file_record.status), JobState.FAILED)
        self._database.execute(
            """
            UPDATE files
            SET status = ?,
                upload_error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (FILE_STATUS_FAILED, error_message, file_id),
        )
        self._database.commit()
        return self.get_file_record(file_id)

    def update_file_original_name(self, file_id: int, original_name: str) -> FileRecord | None:
        self._database.execute(
            """
            UPDATE files
            SET original_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (original_name, file_id),
        )
        self._database.commit()
        return self.get_file_record(file_id)

    def set_file_destination_folder(self, file_id: int, folder: Folder) -> FileRecord | None:
        self._database.execute(
            """
            UPDATE files
            SET destination_folder_id = ?,
                destination_folder_name = ?,
                destination_folder_path = ?,
                destination_drive_id = ?,
                destination_is_shared = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                folder.id,
                folder.name,
                folder.path,
                folder.drive_id,
                int(folder.is_shared),
                file_id,
            ),
        )
        self._database.commit()
        return self.get_file_record(file_id)

    def mark_file_skipped(self, file_id: int) -> FileRecord | None:
        return self.update_file_status(file_id, FILE_STATUS_SKIPPED)

    def save_download(
        self,
        file_id: int,
        local_path: str | None = None,
        temp_path: str | None = None,
        total_bytes: int | None = None,
        status_chat_id: int | None = None,
        status_message_id: int | None = None,
    ) -> DownloadRecord:
        self._database.execute(
            """
            INSERT INTO downloads (
                file_id, local_path, temp_path, status_chat_id, status_message_id,
                total_bytes, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                local_path = excluded.local_path,
                temp_path = excluded.temp_path,
                status_chat_id = COALESCE(excluded.status_chat_id, downloads.status_chat_id),
                status_message_id = COALESCE(excluded.status_message_id, downloads.status_message_id),
                bytes_downloaded = 0,
                total_bytes = excluded.total_bytes,
                progress_percent = 0,
                status = excluded.status,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                file_id,
                local_path,
                temp_path,
                status_chat_id,
                status_message_id,
                total_bytes,
                DOWNLOAD_STATUS_QUEUED,
            ),
        )
        self._database.commit()
        download = self.get_download(file_id)
        if download is None:
            raise RuntimeError("Download was not available after upsert.")
        return download

    def get_download(self, file_id: int) -> DownloadRecord | None:
        row = self._database.fetch_one(
            """
            SELECT id, file_id, local_path, temp_path, status_chat_id, status_message_id,
                   bytes_downloaded, total_bytes, progress_percent, status, error_message,
                   retry_count, created_at, updated_at
            FROM downloads
            WHERE file_id = ?
            """,
            (file_id,),
        )
        if row is None:
            return None
        return _download_record_from_row(row)

    def update_download_status_message(
        self,
        file_id: int,
        *,
        status_chat_id: int,
        status_message_id: int,
    ) -> DownloadRecord | None:
        self._database.execute(
            """
            UPDATE downloads
            SET status_chat_id = ?, status_message_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE file_id = ?
            """,
            (status_chat_id, status_message_id, file_id),
        )
        self._database.commit()
        return self.get_download(file_id)

    def list_interrupted_downloads(self) -> list[InterruptedDownloadRecord]:
        rows = self._database.fetch_all(
            """
            SELECT file_id
            FROM downloads
            WHERE status IN (?, ?)
              AND status_chat_id IS NOT NULL
              AND status_message_id IS NOT NULL
            ORDER BY updated_at ASC, id ASC
            """,
            (DOWNLOAD_STATUS_QUEUED, DOWNLOAD_STATUS_RUNNING),
        )
        records: list[InterruptedDownloadRecord] = []
        for row in rows:
            file_record = self.get_file_record(int(row["file_id"]))
            download = self.get_download(int(row["file_id"]))
            if file_record is None or download is None:
                continue
            records.append(InterruptedDownloadRecord(file=file_record, download=download))
        return records

    def requeue_interrupted_download(self, file_id: int) -> DownloadRecord | None:
        self._database.execute(
            """
            UPDATE downloads
            SET bytes_downloaded = 0,
                progress_percent = 0,
                status = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE file_id = ?
            """,
            (DOWNLOAD_STATUS_QUEUED, "Recovered after process restart.", file_id),
        )
        self._database.commit()
        self.update_file_status(file_id, FILE_STATUS_QUEUED)
        return self.get_download(file_id)

    def requeue_failed_download(self, file_id: int) -> DownloadRecord | None:
        self._database.execute(
            """
            UPDATE downloads
            SET bytes_downloaded = 0,
                progress_percent = 0,
                status = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE file_id = ?
            """,
            (DOWNLOAD_STATUS_QUEUED, "Retried by admin command.", file_id),
        )
        self._database.commit()
        self.update_file_status(file_id, FILE_STATUS_QUEUED)
        return self.get_download(file_id)

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

    def cancel_download(self, file_id: int) -> DownloadRecord | None:
        download = self.get_download(file_id)
        if download is None:
            return None
        if download.status in {
            DOWNLOAD_STATUS_COMPLETED,
            DOWNLOAD_STATUS_FAILED,
            DOWNLOAD_STATUS_CANCELLED,
        }:
            return download
        return self.mark_download_cancelled(file_id, download.retry_count)

    def cancel_upload(self, file_id: int) -> FileRecord | None:
        file_record = self.get_file_record(file_id)
        if file_record is None:
            return None
        if file_record.status in {
            FILE_STATUS_COMPLETED,
            FILE_STATUS_UPLOADED,
            FILE_STATUS_FAILED,
            FILE_STATUS_CANCELLED,
            FILE_STATUS_SKIPPED,
        }:
            return file_record
        require_transition(JobState(file_record.status), JobState.CANCELLED)
        self._database.execute(
            """
            UPDATE files
            SET status = ?,
                upload_retry_after = NULL,
                upload_error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (FILE_STATUS_CANCELLED, "Upload cancelled.", file_id),
        )
        self._database.commit()
        return self.get_file_record(file_id)

    def add_favorite_folder(self, user_id: int, folder: Folder) -> Folder:
        self._database.execute(
            """
            INSERT INTO folder_favorites (
                user_id, folder_id, name, parent_id, drive_id, path,
                is_shared, folder_created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, folder_id) DO UPDATE SET
                name = excluded.name,
                parent_id = excluded.parent_id,
                drive_id = excluded.drive_id,
                path = excluded.path,
                is_shared = excluded.is_shared,
                folder_created_at = excluded.folder_created_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            _folder_parameters(user_id, folder),
        )
        self._database.commit()
        return folder

    def remove_favorite_folder(self, user_id: int, folder_id: str) -> bool:
        cursor = self._database.execute(
            """
            DELETE FROM folder_favorites
            WHERE user_id = ? AND folder_id = ?
            """,
            (user_id, folder_id),
        )
        self._database.commit()
        return cursor.rowcount > 0

    def list_favorite_folders(self, user_id: int) -> list[Folder]:
        rows = self._database.fetch_all(
            """
            SELECT folder_id, name, parent_id, drive_id, path, is_shared, folder_created_at
            FROM folder_favorites
            WHERE user_id = ?
            ORDER BY name COLLATE NOCASE ASC
            """,
            (user_id,),
        )
        return [_folder_from_row(row) for row in rows]

    def is_favorite_folder(self, user_id: int, folder_id: str) -> bool:
        row = self._database.fetch_one(
            """
            SELECT 1
            FROM folder_favorites
            WHERE user_id = ? AND folder_id = ?
            """,
            (user_id, folder_id),
        )
        return row is not None

    def record_recent_folder(self, user_id: int, folder: Folder, limit: int) -> Folder:
        self._database.execute(
            """
            INSERT INTO recent_folders (
                user_id, folder_id, name, parent_id, drive_id, path,
                is_shared, folder_created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, folder_id) DO UPDATE SET
                name = excluded.name,
                parent_id = excluded.parent_id,
                drive_id = excluded.drive_id,
                path = excluded.path,
                is_shared = excluded.is_shared,
                folder_created_at = excluded.folder_created_at,
                used_at = CURRENT_TIMESTAMP
            """,
            _folder_parameters(user_id, folder),
        )
        self._database.execute(
            """
            DELETE FROM recent_folders
            WHERE user_id = ?
              AND id NOT IN (
                  SELECT id
                  FROM recent_folders
                  WHERE user_id = ?
                  ORDER BY used_at DESC, id DESC
                  LIMIT ?
              )
            """,
            (user_id, user_id, limit),
        )
        self._database.commit()
        return folder

    def list_recent_folders(self, user_id: int, limit: int) -> list[Folder]:
        rows = self._database.fetch_all(
            """
            SELECT folder_id, name, parent_id, drive_id, path, is_shared, folder_created_at
            FROM recent_folders
            WHERE user_id = ?
            ORDER BY used_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return [_folder_from_row(row) for row in rows]

    def set_last_folder(self, user_id: int, folder: Folder) -> Folder:
        self._database.execute(
            """
            INSERT INTO user_folder_preferences (
                user_id, last_folder_id, last_folder_name, last_folder_parent_id,
                last_folder_drive_id, last_folder_path, last_folder_is_shared,
                last_folder_created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_folder_id = excluded.last_folder_id,
                last_folder_name = excluded.last_folder_name,
                last_folder_parent_id = excluded.last_folder_parent_id,
                last_folder_drive_id = excluded.last_folder_drive_id,
                last_folder_path = excluded.last_folder_path,
                last_folder_is_shared = excluded.last_folder_is_shared,
                last_folder_created_at = excluded.last_folder_created_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                folder.id,
                folder.name,
                folder.parent_id,
                folder.drive_id,
                folder.path,
                int(folder.is_shared),
                folder.created_at,
            ),
        )
        self._database.commit()
        return folder

    def get_last_folder(self, user_id: int) -> Folder | None:
        row = self._database.fetch_one(
            """
            SELECT last_folder_id, last_folder_name, last_folder_parent_id,
                   last_folder_drive_id, last_folder_path, last_folder_is_shared,
                   last_folder_created_at
            FROM user_folder_preferences
            WHERE user_id = ? AND last_folder_id IS NOT NULL
            """,
            (user_id,),
        )
        if row is None:
            return None
        return Folder(
            id=row["last_folder_id"],
            name=row["last_folder_name"],
            parent_id=row["last_folder_parent_id"],
            drive_id=row["last_folder_drive_id"],
            path=row["last_folder_path"],
            is_shared=bool(row["last_folder_is_shared"]),
            created_at=row["last_folder_created_at"],
        )


_FILE_SELECT_SQL = """
SELECT id, user_id, telegram_file_id, telegram_file_unique_id, source_url, message_id, chat_id,
       forward_origin_chat_id, forward_origin_message_id, google_drive_file_id,
       upload_retry_count, upload_retry_after, upload_error_message,
       destination_folder_id, destination_folder_name, destination_folder_path,
       destination_drive_id, destination_is_shared, original_name, mime_type, size,
       extension, file_type, status, created_at, updated_at
FROM files
"""


def _file_record_from_row(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        telegram_file_id=row["telegram_file_id"],
        telegram_file_unique_id=row["telegram_file_unique_id"],
        source_url=row["source_url"],
        message_id=row["message_id"],
        chat_id=row["chat_id"],
        forward_origin_chat_id=row["forward_origin_chat_id"],
        forward_origin_message_id=row["forward_origin_message_id"],
        google_drive_file_id=row["google_drive_file_id"],
        upload_retry_count=int(row["upload_retry_count"]),
        upload_retry_after=row["upload_retry_after"],
        upload_error_message=row["upload_error_message"],
        destination_folder_id=row["destination_folder_id"],
        destination_folder_name=row["destination_folder_name"],
        destination_folder_path=row["destination_folder_path"],
        destination_drive_id=row["destination_drive_id"],
        destination_is_shared=bool(row["destination_is_shared"]),
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
        status_chat_id=row["status_chat_id"],
        status_message_id=row["status_message_id"],
        bytes_downloaded=int(row["bytes_downloaded"]),
        total_bytes=row["total_bytes"],
        progress_percent=int(row["progress_percent"]),
        status=row["status"],
        error_message=row["error_message"],
        retry_count=int(row["retry_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _folder_parameters(user_id: int, folder: Folder) -> tuple[object, ...]:
    return (
        user_id,
        folder.id,
        folder.name,
        folder.parent_id,
        folder.drive_id,
        folder.path,
        int(folder.is_shared),
        folder.created_at,
    )


def _folder_from_row(row: sqlite3.Row) -> Folder:
    return Folder(
        id=row["folder_id"],
        name=row["name"],
        parent_id=row["parent_id"],
        drive_id=row["drive_id"],
        path=row["path"],
        is_shared=bool(row["is_shared"]),
        created_at=row["folder_created_at"],
    )


def _excluded_file_ids_clause(excluded_file_ids: set[int] | None) -> tuple[str, tuple[int, ...]]:
    if not excluded_file_ids:
        return "", ()
    ordered_ids = tuple(sorted(excluded_file_ids))
    placeholders = ", ".join("?" for _ in ordered_ids)
    return f" AND id NOT IN ({placeholders})", ordered_ids
