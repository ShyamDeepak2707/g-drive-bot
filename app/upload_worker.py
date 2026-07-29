from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from googleapiclient.errors import HttpError

from app import constants
from app.database import DatabaseRepository, FileRecord
from app.job_state import InvalidJobStateTransition, JobState, should_start_upload
from app.ui.messages import TelegramMessage, error_card, warning_card


class UploadError(Exception):
    """Raised when a file cannot be uploaded to Google Drive."""


class UploadVerificationError(UploadError):
    def __init__(
        self,
        drive_file_id: str,
        expected_size: int | None,
        actual_size: int | None,
    ) -> None:
        super().__init__(
            "Uploaded Google Drive file size mismatch: "
            f"expected {expected_size} bytes, got {actual_size} bytes."
        )
        self.drive_file_id = drive_file_id
        self.expected_size = expected_size
        self.actual_size = actual_size


@dataclass(frozen=True)
class UploadedFile:
    drive_file_id: str
    size: int | None


@dataclass(frozen=True)
class _UploadFailureClassification:
    is_transient: bool
    reason: str


@dataclass(frozen=True)
class _ProcessedUploadJob:
    file_record_id: int
    defer_for_run: bool = False


@dataclass(frozen=True)
class CurrentUploadSnapshot:
    file_record_id: int
    filename: str
    progress_percent: int | None
    status: str


@dataclass(frozen=True)
class UploadWorkerSnapshot:
    current_upload: CurrentUploadSnapshot | None
    completed_since_startup: int
    failed_since_startup: int
    is_busy: bool


@dataclass(frozen=True)
class UploadRecoverySummary:
    uploads_recovered: int
    cleanup_recovered: int


class DriveUploader(Protocol):
    def upload_file(
        self,
        local_path: Path,
        filename: str,
        mime_type: str | None,
        folder_id: str,
    ) -> UploadedFile: ...


class UploadNotificationBot(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> object: ...


class GoogleDriveUploader:
    def __init__(self, service: Any) -> None:
        self._service = service

    def upload_file(
        self,
        local_path: Path,
        filename: str,
        mime_type: str | None,
        folder_id: str,
    ) -> UploadedFile:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(
            str(local_path),
            mimetype=mime_type,
            resumable=True,
        )
        request = self._service.files().create(
            body={
                "name": filename,
                "parents": [folder_id],
            },
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        response = request.execute()
        drive_file_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(drive_file_id, str) or not drive_file_id:
            raise UploadError("Google Drive upload did not return a file id.")
        metadata_request = self._service.files().get(
            fileId=drive_file_id,
            fields="id,size",
            supportsAllDrives=True,
        )
        metadata = metadata_request.execute()
        size = metadata.get("size") if isinstance(metadata, dict) else None
        return UploadedFile(
            drive_file_id=drive_file_id,
            size=_parse_drive_size(size),
        )


@dataclass(frozen=True)
class _UploadInput:
    local_path: Path
    filename: str
    mime_type: str | None
    folder_id: str
    expected_size: int | None


class UploadWorker:
    def __init__(
        self,
        repository: DatabaseRepository,
        logger: logging.Logger,
        uploader: DriveUploader | None = None,
        notification_bot: UploadNotificationBot | None = None,
        retry_limit: int = constants.UPLOAD_RETRY_LIMIT,
        retry_backoff_base_seconds: float = constants.UPLOAD_RETRY_BASE_DELAY_SECONDS,
        retry_backoff_max_seconds: float = constants.UPLOAD_RETRY_MAX_DELAY_SECONDS,
    ) -> None:
        self._repository = repository
        self._logger = logger
        self._uploader = uploader
        self._notification_bot = notification_bot
        self._retry_limit = retry_limit
        self._retry_backoff_base_seconds = retry_backoff_base_seconds
        self._retry_backoff_max_seconds = retry_backoff_max_seconds
        self._current_file_record: FileRecord | None = None
        self._cancelled_file_ids: set[int] = set()
        self._completed_since_startup = 0
        self._failed_since_startup = 0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run_until_idle(), name="upload-worker")

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        self._logger.info(
            "upload worker waiting for active job before shutdown",
            extra={"event": "upload_worker_shutdown_waiting"},
        )
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            self._logger.warning(
                "upload worker shutdown wait was cancelled",
                extra={"event": "upload_worker_shutdown_cancelled"},
            )
            raise

    def snapshot(self) -> UploadWorkerSnapshot:
        current_upload: CurrentUploadSnapshot | None = None
        if self._current_file_record is not None:
            current_upload = CurrentUploadSnapshot(
                file_record_id=self._current_file_record.id,
                filename=_file_record_filename(self._current_file_record),
                progress_percent=None,
                status=self._current_file_record.status,
            )
        return UploadWorkerSnapshot(
            current_upload=current_upload,
            completed_since_startup=self._completed_since_startup,
            failed_since_startup=self._failed_since_startup,
            is_busy=current_upload is not None
            or (self._task is not None and not self._task.done()),
        )

    def cancel(self, file_record_id: int, source: str = "manual") -> bool:
        file_record = self._repository.get_file_record(file_record_id)
        if file_record is None:
            return False
        if file_record.status == constants.FILE_STATUS_CANCELLED:
            return True
        if file_record.status not in {
            constants.FILE_STATUS_READY_FOR_UPLOAD,
            constants.FILE_STATUS_UPLOADING,
        }:
            return False
        self._cancelled_file_ids.add(file_record_id)
        cancelled = self._repository.cancel_upload(file_record_id)
        active = (
            self._current_file_record is not None and self._current_file_record.id == file_record_id
        )
        if not active:
            with contextlib.suppress(OSError):
                self._cleanup_uploaded_files(file_record_id)
        self._logger.info(
            "upload cancellation requested",
            extra={
                "event": "upload_cancellation_requested",
                "file_record_id": file_record_id,
                "source": source,
                "active": active,
                "status": cancelled.status if cancelled else None,
            },
        )
        return cancelled is not None and cancelled.status == constants.FILE_STATUS_CANCELLED

    async def run_once(self) -> None:
        processed = await self._process_next_eligible_job(deferred_file_ids=set())
        if processed is None:
            self._logger.info(
                "upload worker found no ready jobs",
                extra={"event": "upload_worker_idle"},
            )

    async def run_until_idle(self) -> None:
        processed_count = 0
        deferred_file_ids: set[int] = set()
        while True:
            processed = await self._process_next_eligible_job(
                deferred_file_ids=deferred_file_ids,
            )
            if processed is None:
                self._logger.info(
                    "upload worker drained eligible jobs",
                    extra={
                        "event": "upload_worker_drained",
                        "processed_count": processed_count,
                    },
                )
                return
            processed_count += 1
            if processed.defer_for_run:
                deferred_file_ids.add(processed.file_record_id)

    async def recover_interrupted_jobs(self) -> UploadRecoverySummary:
        uploads_recovered = 0
        cleanup_recovered = 0
        deferred_file_ids: set[int] = set()

        while True:
            recoverable_uploaded_record = self._repository.get_next_uploading_with_drive_file_id(
                excluded_file_ids=deferred_file_ids,
            )
            if recoverable_uploaded_record is not None:
                self._current_file_record = recoverable_uploaded_record
                try:
                    finalized = self._recover_uploaded_after_restart(recoverable_uploaded_record)
                finally:
                    self._current_file_record = None
                uploads_recovered += 1
                if finalized:
                    cleanup_recovered += 1
                else:
                    deferred_file_ids.add(recoverable_uploaded_record.id)
                continue

            uploaded_record = self._repository.get_next_uploaded(
                excluded_file_ids=deferred_file_ids,
            )
            if uploaded_record is None:
                break
            self._current_file_record = uploaded_record
            try:
                finalized = self._finalize_uploaded(uploaded_record)
            finally:
                self._current_file_record = None
            if finalized:
                cleanup_recovered += 1
            else:
                deferred_file_ids.add(uploaded_record.id)

        return UploadRecoverySummary(
            uploads_recovered=uploads_recovered,
            cleanup_recovered=cleanup_recovered,
        )

    async def _process_next_eligible_job(
        self,
        deferred_file_ids: set[int],
    ) -> _ProcessedUploadJob | None:
        recoverable_uploaded_record = self._repository.get_next_uploading_with_drive_file_id(
            excluded_file_ids=deferred_file_ids,
        )
        if recoverable_uploaded_record is not None:
            self._current_file_record = recoverable_uploaded_record
            try:
                finalized = self._recover_uploaded_after_restart(recoverable_uploaded_record)
            finally:
                self._current_file_record = None
            return _ProcessedUploadJob(
                recoverable_uploaded_record.id,
                defer_for_run=not finalized,
            )

        uploaded_record = self._repository.get_next_uploaded(
            excluded_file_ids=deferred_file_ids,
        )
        if uploaded_record is not None:
            self._current_file_record = uploaded_record
            try:
                finalized = self._finalize_uploaded(uploaded_record)
            finally:
                self._current_file_record = None
            return _ProcessedUploadJob(uploaded_record.id, defer_for_run=not finalized)

        file_record = self._repository.get_next_ready_for_upload(
            excluded_file_ids=deferred_file_ids,
        )
        if file_record is None:
            return None
        self._current_file_record = file_record
        try:
            await self._claim_and_upload(file_record)
        finally:
            self._current_file_record = None
        return _ProcessedUploadJob(file_record.id)

    async def _claim_and_upload(self, file_record: FileRecord) -> None:
        if not should_start_upload(
            JobState(file_record.status),
            google_drive_file_id=file_record.google_drive_file_id,
        ):
            self._logger.info(
                "upload worker skipped job because upload is already represented",
                extra={
                    "event": "upload_worker_skipped",
                    "file_record_id": file_record.id,
                    "status": file_record.status,
                    "google_drive_file_id_present": file_record.google_drive_file_id is not None,
                },
            )
            return

        try:
            updated = self._repository.transition_file_state(
                file_record.id,
                JobState.UPLOADING,
            )
        except InvalidJobStateTransition as exc:
            self._logger.warning(
                "upload worker could not claim job",
                extra={
                    "event": "upload_worker_claim_failed",
                    "file_record_id": file_record.id,
                    "current_state": exc.current.value,
                    "target_state": exc.target.value,
                },
            )
            return
        if updated is None:
            self._logger.warning(
                "upload worker ready job disappeared before claim",
                extra={"event": "upload_worker_claim_missing", "file_record_id": file_record.id},
            )
            return
        updated = self._repository.clear_upload_retry_schedule(updated.id) or updated
        self._current_file_record = updated
        if self._is_cancel_requested(updated.id):
            self._mark_upload_cancelled(updated.id, source="pre_upload")
            return

        self._logger.info(
            "upload started",
            extra={
                "event": "upload_started",
                "file_record_id": updated.id,
                "status": updated.status,
                "local_name": updated.original_name,
                "destination_folder_id": updated.destination_folder_id,
                "destination_folder_path": updated.destination_folder_path,
                "size": updated.size,
            },
        )
        try:
            upload_input = self._resolve_upload_input(updated)
            uploaded_file = await asyncio.to_thread(self._upload, upload_input)
            if self._is_cancel_requested(updated.id):
                self._mark_upload_cancelled(updated.id, source="post_upload")
                return
            self._verify_upload(uploaded_file, upload_input.expected_size, updated.id)
        except Exception as exc:
            if self._is_cancel_requested(updated.id):
                self._mark_upload_cancelled(updated.id, source="upload_exception")
                return
            await self._handle_upload_failure(updated, exc)
            return

        uploaded = self._repository.transition_file_state(
            updated.id,
            JobState.UPLOADED,
            google_drive_file_id=uploaded_file.drive_file_id,
        )
        self._logger.info(
            "upload completed",
            extra={
                "event": "upload_uploaded",
                "file_record_id": updated.id,
                "status": uploaded.status if uploaded else None,
                "google_drive_file_id": uploaded_file.drive_file_id,
                "verified_size": uploaded_file.size,
                "expected_size": upload_input.expected_size,
            },
        )
        if uploaded is not None:
            self._finalize_uploaded(uploaded)

    def _recover_uploaded_after_restart(self, file_record: FileRecord) -> bool:
        if file_record.google_drive_file_id is None:
            self._logger.warning(
                "upload recovery skipped because drive file id is missing",
                extra={
                    "event": "upload_recovery_skipped",
                    "file_record_id": file_record.id,
                    "status": file_record.status,
                },
            )
            return False

        self._logger.info(
            "upload recovery detected existing drive file id",
            extra={
                "event": "upload_recovered",
                "file_record_id": file_record.id,
                "status": file_record.status,
                "google_drive_file_id": file_record.google_drive_file_id,
            },
        )
        uploaded = self._repository.transition_file_state(
            file_record.id,
            JobState.UPLOADED,
            google_drive_file_id=file_record.google_drive_file_id,
        )
        if uploaded is not None:
            return self._finalize_uploaded(uploaded)
        return False

    def _resolve_upload_input(self, file_record: FileRecord) -> _UploadInput:
        download = self._repository.get_download(file_record.id)
        if download is None or download.local_path is None:
            raise UploadError("Downloaded local file path is missing.")

        local_path = Path(download.local_path)
        if not local_path.exists() or not local_path.is_file():
            raise UploadError(f"Downloaded local file does not exist at '{local_path}'.")
        if file_record.destination_folder_id is None:
            raise UploadError("Destination Google Drive folder is missing.")

        filename = file_record.original_name or local_path.name
        return _UploadInput(
            local_path=local_path,
            filename=filename,
            mime_type=file_record.mime_type,
            folder_id=file_record.destination_folder_id,
            expected_size=file_record.size,
        )

    def _upload(self, upload_input: _UploadInput) -> UploadedFile:
        if self._uploader is None:
            raise UploadError("Google Drive uploader is not configured.")
        return self._uploader.upload_file(
            local_path=upload_input.local_path,
            filename=upload_input.filename,
            mime_type=upload_input.mime_type,
            folder_id=upload_input.folder_id,
        )

    def _verify_upload(
        self,
        uploaded_file: UploadedFile,
        expected_size: int | None,
        file_record_id: int,
    ) -> None:
        if expected_size is None:
            self._logger.info(
                "upload verification skipped because expected size is unknown",
                extra={
                    "event": "upload_verification_skipped",
                    "file_record_id": file_record_id,
                    "google_drive_file_id": uploaded_file.drive_file_id,
                    "actual_size": uploaded_file.size,
                },
            )
            return
        if uploaded_file.size == expected_size:
            self._logger.info(
                "upload verified",
                extra={
                    "event": "upload_verified",
                    "file_record_id": file_record_id,
                    "google_drive_file_id": uploaded_file.drive_file_id,
                    "expected_size": expected_size,
                    "actual_size": uploaded_file.size,
                },
            )
            return
        raise UploadVerificationError(
            drive_file_id=uploaded_file.drive_file_id,
            expected_size=expected_size,
            actual_size=uploaded_file.size,
        )

    async def _handle_upload_failure(self, file_record: FileRecord, exc: Exception) -> None:
        classification = _classify_upload_failure(exc)
        extra: dict[str, object] = {
            "file_record_id": file_record.id,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "retry_reason": classification.reason,
            "transient": classification.is_transient,
        }
        if isinstance(exc, UploadVerificationError):
            extra.update(
                {
                    "google_drive_file_id": exc.drive_file_id,
                    "expected_size": exc.expected_size,
                    "actual_size": exc.actual_size,
                }
            )

        if not classification.is_transient:
            failed = self._repository.mark_upload_failed_permanently(
                file_record.id,
                str(exc),
            )
            self._logger.warning(
                "upload failed permanently",
                extra={
                    **extra,
                    "event": "upload_failed_permanent",
                    "status": failed.status if failed else None,
                },
            )
            self._failed_since_startup += 1
            await self._notify_upload_failure(
                file_record=file_record,
                exc=exc,
                classification=classification,
                terminal=True,
            )
            return

        next_retry_count = file_record.upload_retry_count + 1
        if next_retry_count > self._retry_limit:
            failed = self._repository.mark_upload_failed_permanently(
                file_record.id,
                str(exc),
            )
            self._logger.warning(
                "upload retry limit reached",
                extra={
                    **extra,
                    "event": "upload_retry_exhausted",
                    "retry_count": file_record.upload_retry_count,
                    "retry_limit": self._retry_limit,
                    "status": failed.status if failed else None,
                },
            )
            self._failed_since_startup += 1
            await self._notify_upload_failure(
                file_record=file_record,
                exc=exc,
                classification=classification,
                terminal=True,
            )
            return

        delay_seconds = _retry_delay(
            attempt=next_retry_count,
            base_seconds=self._retry_backoff_base_seconds,
            max_seconds=self._retry_backoff_max_seconds,
        )
        retry_after = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        retry_after_value = _sqlite_timestamp(retry_after)
        scheduled = self._repository.schedule_upload_retry(
            file_record.id,
            retry_count=next_retry_count,
            retry_after=retry_after_value,
            error_message=str(exc),
        )
        self._logger.warning(
            "upload failed and retry was scheduled",
            extra={
                **extra,
                "event": "upload_retry_scheduled",
                "status": scheduled.status if scheduled else None,
                "retry_count": next_retry_count,
                "retry_after": retry_after.isoformat(),
                "retry_delay_seconds": delay_seconds,
            },
        )
        await self._notify_upload_failure(
            file_record=file_record,
            exc=exc,
            classification=classification,
            terminal=False,
            retry_after=retry_after,
            retry_count=next_retry_count,
        )

    def _finalize_uploaded(self, file_record: FileRecord) -> bool:
        if file_record.google_drive_file_id is None:
            self._logger.warning(
                "uploaded job cannot be finalized without a drive file id",
                extra={
                    "event": "upload_finalize_skipped",
                    "file_record_id": file_record.id,
                    "status": file_record.status,
                },
            )
            return False

        self._logger.info(
            "upload finalization started",
            extra={
                "event": "upload_finalize_started",
                "file_record_id": file_record.id,
                "google_drive_file_id": file_record.google_drive_file_id,
            },
        )
        try:
            self._cleanup_uploaded_files(file_record.id)
        except OSError as exc:
            self._logger.warning(
                "upload finalization cleanup failed",
                extra={
                    "event": "upload_finalize_cleanup_failed",
                    "file_record_id": file_record.id,
                    "google_drive_file_id": file_record.google_drive_file_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return False

        completed = self._repository.transition_file_state(
            file_record.id,
            JobState.COMPLETED,
            google_drive_file_id=file_record.google_drive_file_id,
        )
        self._logger.info(
            "upload finalized",
            extra={
                "event": "upload_finalized",
                "file_record_id": file_record.id,
                "status": completed.status if completed else None,
                "google_drive_file_id": file_record.google_drive_file_id,
            },
        )
        if completed is not None:
            self._completed_since_startup += 1
            return True
        return False

    def _cleanup_uploaded_files(self, file_record_id: int) -> None:
        download = self._repository.get_download(file_record_id)
        if download is None:
            return

        paths: list[Path] = []
        if download.local_path is not None:
            local_path = Path(download.local_path)
            paths.extend(
                [
                    local_path,
                    Path(f"{local_path}.part"),
                    Path(f"{local_path}.part.temp"),
                    Path(f"{local_path}.temp"),
                ]
            )
        if download.temp_path is not None:
            temp_path = Path(download.temp_path)
            paths.extend(
                [
                    temp_path,
                    Path(f"{temp_path}.temp"),
                ]
            )

        seen: set[Path] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            _delete_file_if_present(path)

    def _is_cancel_requested(self, file_record_id: int) -> bool:
        file_record = self._repository.get_file_record(file_record_id)
        return file_record_id in self._cancelled_file_ids or (
            file_record is not None and file_record.status == constants.FILE_STATUS_CANCELLED
        )

    def _mark_upload_cancelled(self, file_record_id: int, *, source: str) -> None:
        self._cancelled_file_ids.add(file_record_id)
        cancelled = self._repository.cancel_upload(file_record_id)
        self._cleanup_uploaded_files(file_record_id)
        self._logger.info(
            "upload cancelled",
            extra={
                "event": "upload_cancelled",
                "file_record_id": file_record_id,
                "source": source,
                "status": cancelled.status if cancelled else None,
            },
        )

    async def _notify_upload_failure(
        self,
        *,
        file_record: FileRecord,
        exc: Exception,
        classification: _UploadFailureClassification,
        terminal: bool,
        retry_after: datetime | None = None,
        retry_count: int | None = None,
    ) -> None:
        if self._notification_bot is None:
            return

        download = self._repository.get_download(file_record.id)
        if download is None or download.status_chat_id is None:
            self._logger.info(
                "upload failure notification skipped because no Telegram chat is stored",
                extra={
                    "event": "upload_failure_notification_skipped",
                    "file_record_id": file_record.id,
                    "download_record_present": download is not None,
                },
            )
            return

        message = _upload_failure_message(
            file_record=file_record,
            exc=exc,
            classification=classification,
            terminal=terminal,
            retry_after=retry_after,
            retry_count=retry_count,
        )
        try:
            await self._notification_bot.send_message(
                chat_id=download.status_chat_id,
                text=message.text,
                parse_mode=message.parse_mode,
                disable_web_page_preview=message.disable_web_page_preview,
            )
        except Exception as notify_exc:
            self._logger.warning(
                "upload failure notification failed",
                extra={
                    "event": "upload_failure_notification_failed",
                    "file_record_id": file_record.id,
                    "status_chat_id": download.status_chat_id,
                    "error": str(notify_exc),
                    "error_type": type(notify_exc).__name__,
                },
            )
            return

        self._logger.info(
            "upload failure notification sent",
            extra={
                "event": "upload_failure_notification_sent",
                "file_record_id": file_record.id,
                "status_chat_id": download.status_chat_id,
                "terminal": terminal,
                "retry_reason": classification.reason,
            },
        )


def _parse_drive_size(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _file_record_filename(file_record: FileRecord) -> str:
    return file_record.original_name or f"{file_record.file_type or 'file'}-{file_record.id}"


def _upload_failure_message(
    *,
    file_record: FileRecord,
    exc: Exception,
    classification: _UploadFailureClassification,
    terminal: bool,
    retry_after: datetime | None,
    retry_count: int | None,
) -> TelegramMessage:
    reason = _truncate_text(str(exc), 240)
    fields: dict[str, object] = {
        "File": _file_record_filename(file_record),
        "Reason": reason,
    }
    if terminal:
        fields["Status"] = "Failed"
        return error_card(
            "Upload Failed",
            fields=fields,
            footer="Use /failed for the saved failure summary.",
        )

    fields["Status"] = "Retry Scheduled"
    if retry_count is not None:
        fields["Retry attempt"] = retry_count
    if retry_after is not None:
        fields["Next retry"] = retry_after.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    fields["Retry reason"] = classification.reason
    return warning_card(
        "Upload Delayed",
        fields=fields,
        footer="The bot will retry automatically.",
    )


def _truncate_text(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 3:
        return value[:max_length]
    return f"{value[: max_length - 3]}..."


def _delete_file_if_present(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def _classify_upload_failure(exc: Exception) -> _UploadFailureClassification:
    if isinstance(exc, UploadVerificationError):
        return _UploadFailureClassification(False, "verification_mismatch")
    if isinstance(exc, UploadError):
        return _UploadFailureClassification(False, _upload_error_reason(exc))
    if isinstance(exc, HttpError):
        return _classify_http_error(exc)
    if isinstance(exc, TimeoutError):
        return _UploadFailureClassification(True, "timeout")
    if isinstance(exc, ConnectionError):
        return _UploadFailureClassification(True, "connection_error")
    error_type = type(exc).__name__.lower()
    if "timeout" in error_type:
        return _UploadFailureClassification(True, "timeout")
    if "connection" in error_type or "temporar" in str(exc).lower():
        return _UploadFailureClassification(True, "network_error")
    return _UploadFailureClassification(False, "unexpected_error")


def _classify_http_error(exc: HttpError) -> _UploadFailureClassification:
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = _http_error_reason(exc)
    if status in {408, 429, 500, 502, 503, 504}:
        return _UploadFailureClassification(True, f"http_{status}")
    if status == 403 and reason in {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "sharingRateLimitExceeded",
    }:
        return _UploadFailureClassification(True, reason)
    return _UploadFailureClassification(False, f"http_{status or 'unknown'}")


def _http_error_reason(exc: HttpError) -> str | None:
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="ignore")
    elif isinstance(content, str):
        text = content
    else:
        return None
    for reason in (
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "sharingRateLimitExceeded",
    ):
        if reason in text:
            return reason
    return None


def _upload_error_reason(exc: UploadError) -> str:
    message = str(exc).lower()
    if "local file path is missing" in message or "does not exist" in message:
        return "missing_local_file"
    if "destination google drive folder is missing" in message:
        return "missing_destination_folder"
    if "uploader is not configured" in message:
        return "uploader_not_configured"
    if "did not return a file id" in message:
        return "invalid_drive_upload_response"
    return "upload_error"


def _retry_delay(attempt: int, base_seconds: float, max_seconds: float) -> float:
    base = max(0.0, base_seconds)
    maximum = max(base, max_seconds)
    return min(maximum, base * (2 ** max(0, attempt - 1)))


def _sqlite_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
