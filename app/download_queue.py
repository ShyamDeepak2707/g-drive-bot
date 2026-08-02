from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError as TelegramApiError

from app import constants
from app.database import DatabaseRepository, DownloadRecord, FileRecord
from app.download_manager import DownloadManager
from app.exceptions import DownloadError
from app.models import DownloadResult, FileMetadata, TelegramFileType
from app.progress import ProgressSnapshot, format_bytes, format_duration
from app.task_messages import ActiveTaskMessageRegistry, TaskMessageKey
from app.transfer_coordinator import TransferCoordinator
from app.ui import TelegramMessage, progress_card


class DownloadJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadJob:
    file_record_id: int
    metadata: FileMetadata
    chat_id: int
    status_message_id: int
    attempts: int = 0
    status: DownloadJobStatus = DownloadJobStatus.QUEUED
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_task: asyncio.Task[DownloadResult] | None = field(default=None, repr=False)


@dataclass
class _ProgressState:
    latest: ProgressSnapshot | None = None


@dataclass(frozen=True)
class CurrentDownloadSnapshot:
    file_record_id: int
    filename: str
    progress_percent: int | None
    speed_bytes_per_second: float | None


@dataclass(frozen=True)
class DownloadQueueSnapshot:
    current_download: CurrentDownloadSnapshot | None
    queue_length: int
    completed_since_startup: int
    failed_since_startup: int


class DownloadQueue:
    def __init__(
        self,
        repository: DatabaseRepository,
        download_manager: DownloadManager,
        bot: Bot,
        logger: logging.Logger,
        retry_limit: int = constants.DOWNLOAD_QUEUE_RETRY_LIMIT,
        progress_interval_seconds: float = constants.DOWNLOAD_PROGRESS_UPDATE_SECONDS,
        retry_backoff_base_seconds: float = constants.DOWNLOAD_RETRY_BASE_DELAY_SECONDS,
        retry_backoff_max_seconds: float = constants.DOWNLOAD_RETRY_MAX_DELAY_SECONDS,
        task_message_registry: ActiveTaskMessageRegistry | None = None,
        transfer_coordinator: TransferCoordinator | None = None,
    ) -> None:
        self._repository = repository
        self._download_manager = download_manager
        self._bot = bot
        self._logger = logger
        self._retry_limit = retry_limit
        self._progress_interval_seconds = progress_interval_seconds
        self._retry_backoff_base_seconds = retry_backoff_base_seconds
        self._retry_backoff_max_seconds = retry_backoff_max_seconds
        self._task_message_registry = task_message_registry
        self._transfer_coordinator = transfer_coordinator
        self._queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._jobs: dict[int, DownloadJob] = {}
        self._current_job: DownloadJob | None = None
        self._current_progress: ProgressSnapshot | None = None
        self._completed_since_startup = 0
        self._failed_since_startup = 0
        self._stopping = False

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stopping = False
            self._worker = asyncio.create_task(self._run_worker(), name="download-worker-1")
            self._logger.info("download queue started", extra={"event": "download_queue_started"})

    async def stop(self) -> None:
        self._stopping = True
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._logger.info("download queue stopped", extra={"event": "download_queue_stopped"})

    async def enqueue(self, job: DownloadJob) -> None:
        self._jobs[job.file_record_id] = job
        self._repository.mark_file_queued(job.file_record_id)
        self._repository.save_download(
            file_id=job.file_record_id,
            total_bytes=job.metadata.size,
            status_chat_id=job.chat_id,
            status_message_id=job.status_message_id,
        )
        await self._queue.put(job)
        self._logger.info("download job queued", extra={"event": "download_queued"})

    def recover_interrupted_jobs(self) -> int:
        recovered = 0
        for record in self._repository.list_interrupted_downloads():
            if record.file.id in self._jobs:
                self._logger.info(
                    "interrupted download already recovered",
                    extra={
                        "event": "download_recovery_duplicate_skipped",
                        "file_record_id": record.file.id,
                    },
                )
                continue
            metadata = _metadata_from_file_record(record.file)
            if metadata is None:
                self._logger.warning(
                    "interrupted download cannot be recovered because metadata is incomplete",
                    extra={
                        "event": "download_recovery_skipped",
                        "file_record_id": record.file.id,
                    },
                )
                continue
            if record.download.status_chat_id is None or record.download.status_message_id is None:
                continue
            self._repository.requeue_interrupted_download(record.file.id)
            job = DownloadJob(
                file_record_id=record.file.id,
                metadata=metadata,
                chat_id=record.download.status_chat_id,
                status_message_id=record.download.status_message_id,
                attempts=record.download.retry_count,
            )
            self._jobs[job.file_record_id] = job
            self._queue.put_nowait(job)
            recovered += 1
            self._logger.info(
                "interrupted download recovered",
                extra={
                    "event": "download_recovered",
                    "file_record_id": job.file_record_id,
                    "retry_count": job.attempts,
                },
            )
        return recovered

    def retry_failed_download(self, file_record: FileRecord, download: DownloadRecord) -> bool:
        if download.status != constants.DOWNLOAD_STATUS_FAILED:
            return False
        existing_job = self._jobs.get(file_record.id)
        if existing_job is not None and existing_job.status in {
            DownloadJobStatus.QUEUED,
            DownloadJobStatus.RUNNING,
        }:
            return False
        if _is_permanent_download_failure(download.error_message):
            return False
        metadata = _metadata_from_file_record(file_record)
        if metadata is None:
            return False
        if download.status_chat_id is None or download.status_message_id is None:
            return False

        self._repository.requeue_failed_download(file_record.id)
        job = DownloadJob(
            file_record_id=file_record.id,
            metadata=metadata,
            chat_id=download.status_chat_id,
            status_message_id=download.status_message_id,
        )
        self._jobs[job.file_record_id] = job
        self._queue.put_nowait(job)
        self._logger.info(
            "failed download requeued by admin command",
            extra={
                "event": "download_retry_failed_requeued",
                "file_record_id": file_record.id,
            },
        )
        return True

    def cancel(self, file_record_id: int, source: str = "manual") -> bool:
        job = self._jobs.get(file_record_id)
        if job is None:
            return False
        if job.status == DownloadJobStatus.CANCELLED:
            return True
        job.cancel_event.set()
        job.status = DownloadJobStatus.CANCELLED
        self._repository.cancel_download(file_record_id)
        self._logger.info(
            "download cancellation requested",
            extra={
                "event": "download_cancellation_requested",
                "file_record_id": file_record_id,
                "source": source,
                "active": job.active_task is not None and not job.active_task.done(),
            },
        )
        return True

    def cancel_chat(self, chat_id: int, source: str = "telegram_command") -> bool:
        cancellable_jobs = [
            job
            for job in self._jobs.values()
            if job.chat_id == chat_id
            and job.status in {DownloadJobStatus.QUEUED, DownloadJobStatus.RUNNING}
        ]
        if not cancellable_jobs:
            return False
        job = cancellable_jobs[-1]
        return self.cancel(job.file_record_id, source=source)

    def status(self, file_record_id: int) -> DownloadJobStatus | None:
        job = self._jobs.get(file_record_id)
        return job.status if job else None

    def snapshot(self) -> DownloadQueueSnapshot:
        current_download: CurrentDownloadSnapshot | None = None
        if self._current_job is not None:
            current_download = CurrentDownloadSnapshot(
                file_record_id=self._current_job.file_record_id,
                filename=_job_filename(self._current_job),
                progress_percent=_progress_percent(self._current_progress),
                speed_bytes_per_second=(
                    self._current_progress.speed_bytes_per_second
                    if self._current_progress is not None
                    else None
                ),
            )
        return DownloadQueueSnapshot(
            current_download=current_download,
            queue_length=self._queue.qsize(),
            completed_since_startup=self._completed_since_startup,
            failed_since_startup=self._failed_since_startup,
        )

    async def _run_worker(self) -> None:
        while not self._stopping:
            job = await self._queue.get()
            self._current_job = job
            self._current_progress = None
            try:
                await self._process_job(job)
            finally:
                if self._current_job is job:
                    self._current_job = None
                    self._current_progress = None
                self._queue.task_done()

    async def _process_job(self, job: DownloadJob) -> None:
        while job.attempts <= self._retry_limit:
            if job.cancel_event.is_set():
                job.status = DownloadJobStatus.CANCELLED
                self._repository.mark_download_cancelled(
                    job.file_record_id,
                    job.attempts,
                )
                self._unregister_task_message(job)
                await self._safe_edit_message(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text="Download cancelled.",
                )
                await self._promote_latest_active_message(job.chat_id)
                return

            await self._wait_for_older_files(job)
            if job.cancel_event.is_set():
                job.status = DownloadJobStatus.CANCELLED
                self._repository.mark_download_cancelled(
                    job.file_record_id,
                    job.attempts,
                )
                await self._safe_edit_message(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text="Download cancelled.",
                )
                await self._promote_latest_active_message(job.chat_id)
                return

            job.attempts += 1
            job.status = DownloadJobStatus.RUNNING
            self._repository.mark_file_downloading(job.file_record_id)
            progress_state = _ProgressState()

            async def progress(
                snapshot: ProgressSnapshot,
                state: _ProgressState = progress_state,
            ) -> None:
                if job.cancel_event.is_set():
                    raise asyncio.CancelledError
                state.latest = snapshot
                self._current_progress = snapshot

            async def flush_progress(state: _ProgressState = progress_state) -> None:
                last_flushed: ProgressSnapshot | None = None
                interval = max(0.001, self._progress_interval_seconds)
                while True:
                    await asyncio.sleep(interval)
                    snapshot = state.latest
                    if snapshot is None or snapshot == last_flushed:
                        continue
                    self._repository.update_download_progress(
                        file_id=job.file_record_id,
                        bytes_downloaded=snapshot.current,
                        total_bytes=snapshot.total,
                    )
                    message = _download_progress_message(job, snapshot)
                    await self._safe_edit_message(
                        chat_id=job.chat_id,
                        message_id=job.status_message_id,
                        message=message,
                    )
                    self._update_task_message(job, message)
                    last_flushed = snapshot

            progress_task: asyncio.Task[None] | None = None
            try:
                async with _transfer_slot(
                    self._transfer_coordinator,
                    transfer_type="download",
                    file_record_id=job.file_record_id,
                    cancellation_check=job.cancel_event.is_set,
                ):
                    self._logger.info(
                        "download started",
                        extra={
                            "event": "download_job_started",
                            "file_record_id": job.file_record_id,
                            "attempt": job.attempts,
                        },
                    )
                    started_message = _download_started_message(job)
                    await self._safe_edit_message(
                        chat_id=job.chat_id,
                        message_id=job.status_message_id,
                        message=started_message,
                    )
                    self._register_task_message(job, started_message)
                    progress_task = asyncio.create_task(
                        flush_progress(),
                        name=f"download-progress-{job.file_record_id}",
                    )
                    job.active_task = asyncio.create_task(
                        self._download_manager.download(
                            file_record_id=job.file_record_id,
                            metadata=job.metadata,
                            progress_callback=progress,
                        ),
                        name=f"download-{job.file_record_id}",
                    )
                    result = await job.active_task
                job.active_task = None
                await _stop_progress_task(progress_task)
                progress_task = None
                self._repository.mark_download_complete(
                    file_id=job.file_record_id,
                    local_path=str(result.path),
                    total_bytes=result.size,
                )
                self._repository.mark_file_awaiting_rename(job.file_record_id)
                job.status = DownloadJobStatus.COMPLETED
                self._completed_since_startup += 1
                self._logger.info(
                    "download completed",
                    extra={
                        "event": "download_job_completed",
                        "file_record_id": job.file_record_id,
                        "attempt": job.attempts,
                    },
                )
                self._unregister_task_message(job)
                await self._safe_delete_message(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                )
                await self._send_rename_prompt(job, result.filename)
                await self._promote_latest_active_message(job.chat_id)
                return
            except asyncio.CancelledError:
                job.active_task = None
                await _stop_progress_task(progress_task)
                if self._stopping:
                    raise
                job.status = DownloadJobStatus.CANCELLED
                self._repository.mark_download_cancelled(
                    job.file_record_id,
                    job.attempts,
                )
                self._unregister_task_message(job)
                await self._safe_edit_message(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text="Download cancelled.",
                )
                await self._promote_latest_active_message(job.chat_id)
                self._logger.info(
                    "download cancelled",
                    extra={
                        "event": "download_job_cancelled",
                        "file_record_id": job.file_record_id,
                        "attempt": job.attempts,
                    },
                )
                return
            except DownloadError as exc:
                job.active_task = None
                await _stop_progress_task(progress_task)
                if job.cancel_event.is_set():
                    job.status = DownloadJobStatus.CANCELLED
                    self._repository.mark_download_cancelled(
                        job.file_record_id,
                        job.attempts,
                    )
                    self._unregister_task_message(job)
                    await self._safe_edit_message(
                        chat_id=job.chat_id,
                        message_id=job.status_message_id,
                        text="Download cancelled.",
                    )
                    await self._promote_latest_active_message(job.chat_id)
                    self._logger.info(
                        "download cancelled",
                        extra={
                            "event": "download_job_cancelled",
                            "file_record_id": job.file_record_id,
                            "attempt": job.attempts,
                        },
                    )
                    return
                self._logger.warning(
                    "download attempt failed",
                    extra={
                        "event": "download_failed",
                        "file_record_id": job.file_record_id,
                        "attempt": job.attempts,
                        "error": str(exc),
                    },
                )
                if job.attempts > self._retry_limit:
                    job.status = DownloadJobStatus.FAILED
                    self._repository.mark_download_failed(
                        job.file_record_id,
                        str(exc),
                        job.attempts,
                    )
                    self._failed_since_startup += 1
                    self._unregister_task_message(job)
                    await self._safe_edit_message(
                        chat_id=job.chat_id,
                        message_id=job.status_message_id,
                        text=_download_failed_text(exc),
                    )
                    await self._promote_latest_active_message(job.chat_id)
                    return
                self._logger.info(
                    "retrying download",
                    extra={
                        "event": "download_retry",
                        "file_record_id": job.file_record_id,
                        "attempt": job.attempts,
                        "next_attempt": job.attempts + 1,
                        "delay_seconds": _retry_delay(
                            job.attempts,
                            self._retry_backoff_base_seconds,
                            self._retry_backoff_max_seconds,
                        ),
                        "error": str(exc),
                    },
                )
                await _sleep_or_cancel(
                    job.cancel_event,
                    _retry_delay(
                        job.attempts,
                        self._retry_backoff_base_seconds,
                        self._retry_backoff_max_seconds,
                    ),
                )
            except Exception as exc:
                job.active_task = None
                await _stop_progress_task(progress_task)
                if job.cancel_event.is_set():
                    job.status = DownloadJobStatus.CANCELLED
                    self._repository.mark_download_cancelled(
                        job.file_record_id,
                        job.attempts,
                    )
                    self._unregister_task_message(job)
                    await self._safe_edit_message(
                        chat_id=job.chat_id,
                        message_id=job.status_message_id,
                        text="Download cancelled.",
                    )
                    await self._promote_latest_active_message(job.chat_id)
                    self._logger.info(
                        "download cancelled",
                        extra={
                            "event": "download_job_cancelled",
                            "file_record_id": job.file_record_id,
                            "attempt": job.attempts,
                        },
                    )
                    return
                self._logger.exception(
                    "download attempt failed unexpectedly",
                    extra={
                        "event": "download_unexpected_failed",
                        "file_record_id": job.file_record_id,
                        "attempt": job.attempts,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
                if job.attempts > self._retry_limit:
                    job.status = DownloadJobStatus.FAILED
                    self._repository.mark_download_failed(
                        job.file_record_id,
                        str(exc),
                        job.attempts,
                    )
                    self._failed_since_startup += 1
                    self._unregister_task_message(job)
                    await self._safe_edit_message(
                        chat_id=job.chat_id,
                        message_id=job.status_message_id,
                        text=_download_failed_text(exc),
                    )
                    await self._promote_latest_active_message(job.chat_id)
                    return
                self._logger.info(
                    "retrying download after unexpected failure",
                    extra={
                        "event": "download_unexpected_retry",
                        "file_record_id": job.file_record_id,
                        "attempt": job.attempts,
                        "next_attempt": job.attempts + 1,
                        "delay_seconds": _retry_delay(
                            job.attempts,
                            self._retry_backoff_base_seconds,
                            self._retry_backoff_max_seconds,
                        ),
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
                await _sleep_or_cancel(
                    job.cancel_event,
                    _retry_delay(
                        job.attempts,
                        self._retry_backoff_base_seconds,
                        self._retry_backoff_max_seconds,
                    ),
                )

    async def _send_rename_prompt(self, job: DownloadJob, filename: str) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Keep original filename",
                        callback_data=_rename_callback(
                            constants.RENAME_ACTION_KEEP, job.file_record_id
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Rename",
                        callback_data=_rename_callback(
                            constants.RENAME_ACTION_RENAME, job.file_record_id
                        ),
                    ),
                    InlineKeyboardButton(
                        "Skip",
                        callback_data=_rename_callback(
                            constants.RENAME_ACTION_SKIP, job.file_record_id
                        ),
                    ),
                ],
            ]
        )
        await self._bot.send_message(
            chat_id=job.chat_id,
            text=f"Downloaded {filename}.\nRename it, then choose a destination folder.",
            reply_markup=keyboard,
        )

    async def _wait_for_older_files(self, job: DownloadJob) -> None:
        waiting_message_sent = False
        while not job.cancel_event.is_set():
            blocking_file = self._repository.get_oldest_incomplete_file_before(job.file_record_id)
            if blocking_file is None:
                if waiting_message_sent:
                    self._logger.info(
                        "download no longer blocked by older jobs",
                        extra={
                            "event": "download_fifo_wait_finished",
                            "file_record_id": job.file_record_id,
                        },
                    )
                return
            if not waiting_message_sent:
                waiting_message_sent = True
                self._logger.info(
                    "download waiting for older file lifecycle to finish",
                    extra={
                        "event": "download_fifo_waiting",
                        "file_record_id": job.file_record_id,
                        "blocking_file_record_id": blocking_file.id,
                        "blocking_status": blocking_file.status,
                    },
                )
                await self._safe_edit_message(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text=(
                        "Waiting for the previous file to finish uploading before "
                        "this download starts."
                    ),
                )
            await _sleep_or_cancel(job.cancel_event, 2.0)

    async def _safe_edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str | None = None,
        message: TelegramMessage | None = None,
    ) -> None:
        if message is None and text is None:
            raise ValueError("Either text or message must be provided.")
        text_value = message.text if message is not None else str(text)
        try:
            if message is None:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text_value,
                )
            else:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text_value,
                    parse_mode=message.parse_mode,
                    disable_web_page_preview=message.disable_web_page_preview,
                )
        except TelegramApiError:
            self._logger.warning(
                "failed to edit progress message", extra={"event": "progress_edit_failed"}
            )

    async def _safe_delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramApiError:
            self._logger.warning(
                "failed to delete progress message",
                extra={"event": "progress_delete_failed"},
            )

    def _register_task_message(self, job: DownloadJob, message: TelegramMessage) -> None:
        if self._task_message_registry is None:
            return

        def replace_message_id(message_id: int) -> None:
            job.status_message_id = message_id
            self._repository.update_download_status_message(
                job.file_record_id,
                status_chat_id=job.chat_id,
                status_message_id=message_id,
            )

        self._task_message_registry.register(
            key=_download_task_message_key(job.file_record_id),
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            message=message,
            replace_message_id=replace_message_id,
        )

    def _update_task_message(self, job: DownloadJob, message: TelegramMessage) -> None:
        if self._task_message_registry is None:
            return
        self._task_message_registry.update(_download_task_message_key(job.file_record_id), message)

    def _unregister_task_message(self, job: DownloadJob) -> None:
        if self._task_message_registry is None:
            return
        self._task_message_registry.unregister(_download_task_message_key(job.file_record_id))

    async def _promote_latest_active_message(self, chat_id: int) -> None:
        if self._task_message_registry is None:
            return
        await self._task_message_registry.promote_latest_active(
            chat_id=chat_id,
            bot=self._bot,
            logger=self._logger,
        )


def _rename_callback(action: str, file_record_id: int) -> str:
    return f"{constants.RENAME_CALLBACK_PREFIX}:{action}:{file_record_id}"


def _download_task_message_key(file_record_id: int) -> TaskMessageKey:
    return ("download", file_record_id)


def _download_failed_text(exc: Exception) -> str:
    return f"Download failed: {exc}\nUse /retry_failed to retry eligible failed jobs."


def _job_filename(job: DownloadJob) -> str:
    return job.metadata.original_name or f"{job.metadata.file_type.value}-{job.metadata.message_id}"


def _progress_percent(snapshot: ProgressSnapshot | None) -> int | None:
    if snapshot is None or not snapshot.total:
        return None
    return min(100, max(0, int((snapshot.current / snapshot.total) * 100)))


def _download_started_message(job: DownloadJob) -> TelegramMessage:
    return progress_card(
        "📥 Downloading",
        filename=_job_filename(job),
        percent=0,
        transferred_size=format_bytes(0),
        total_size=format_bytes(job.metadata.size),
        speed="Calculating",
        status_text="Queued download started.",
    )


def _download_progress_message(job: DownloadJob, snapshot: ProgressSnapshot) -> TelegramMessage:
    return progress_card(
        "📥 Downloading",
        filename=_job_filename(job),
        percent=_progress_percent(snapshot),
        transferred_size=format_bytes(snapshot.current),
        total_size=format_bytes(snapshot.total),
        speed=_download_speed(snapshot),
        eta=_download_eta(snapshot),
    )


def _download_speed(snapshot: ProgressSnapshot) -> str:
    if snapshot.speed_bytes_per_second is None:
        return "Calculating"
    return f"{format_bytes(int(snapshot.speed_bytes_per_second))}/s"


def _download_eta(snapshot: ProgressSnapshot) -> str | None:
    if snapshot.eta_seconds is None:
        return None
    return format_duration(snapshot.eta_seconds)


async def _stop_progress_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _retry_delay(attempt: int, base_seconds: float, max_seconds: float) -> float:
    base = max(0.0, base_seconds)
    maximum = max(base, max_seconds)
    return min(maximum, base * (2 ** max(0, attempt - 1)))


async def _sleep_or_cancel(cancel_event: asyncio.Event, delay_seconds: float) -> None:
    if delay_seconds <= 0:
        return
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay_seconds)
    except TimeoutError:
        return


@contextlib.asynccontextmanager
async def _transfer_slot(
    transfer_coordinator: TransferCoordinator | None,
    *,
    transfer_type: str,
    file_record_id: int,
    cancellation_check: Callable[[], bool] | None = None,
) -> AsyncIterator[None]:
    if transfer_coordinator is None:
        yield
        return
    async with transfer_coordinator.acquire(
        transfer_type=transfer_type,
        file_record_id=file_record_id,
        cancellation_check=cancellation_check,
    ):
        yield


def _metadata_from_file_record(file_record: FileRecord) -> FileMetadata | None:
    if (
        file_record.message_id is None
        or file_record.chat_id is None
        or file_record.file_type is None
    ):
        return None
    try:
        file_type = TelegramFileType(file_record.file_type)
    except ValueError:
        return None
    return FileMetadata(
        telegram_file_id=file_record.telegram_file_id,
        message_id=file_record.message_id,
        chat_id=file_record.chat_id,
        forward_origin_chat_id=file_record.forward_origin_chat_id,
        forward_origin_message_id=file_record.forward_origin_message_id,
        original_name=file_record.original_name,
        mime_type=file_record.mime_type,
        size=file_record.size,
        extension=file_record.extension,
        file_type=file_type,
        created_at=file_record.created_at,
        telegram_file_unique_id=file_record.telegram_file_unique_id,
        source_url=file_record.source_url,
    )


def _is_permanent_download_failure(error_message: str | None) -> bool:
    if error_message is None:
        return False
    normalized = error_message.casefold()
    return "file is too big" in normalized or "doesn't contain any downloadable media" in normalized
