from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError as TelegramApiError

from app import constants
from app.database import DatabaseRepository
from app.download_manager import DownloadManager
from app.exceptions import DownloadError
from app.models import FileMetadata
from app.progress import ProgressSnapshot, format_progress


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


class DownloadQueue:
    def __init__(
        self,
        repository: DatabaseRepository,
        download_manager: DownloadManager,
        bot: Bot,
        logger: logging.Logger,
        retry_limit: int = constants.DOWNLOAD_QUEUE_RETRY_LIMIT,
        progress_interval_seconds: float = constants.DOWNLOAD_PROGRESS_UPDATE_SECONDS,
    ) -> None:
        self._repository = repository
        self._download_manager = download_manager
        self._bot = bot
        self._logger = logger
        self._retry_limit = retry_limit
        self._progress_interval_seconds = progress_interval_seconds
        self._queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._jobs: dict[int, DownloadJob] = {}
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
        )
        await self._queue.put(job)
        self._logger.info("download job queued", extra={"event": "download_queued"})

    def cancel(self, file_record_id: int) -> bool:
        job = self._jobs.get(file_record_id)
        if job is None:
            return False
        job.cancel_event.set()
        job.status = DownloadJobStatus.CANCELLED
        return True

    def status(self, file_record_id: int) -> DownloadJobStatus | None:
        job = self._jobs.get(file_record_id)
        return job.status if job else None

    async def _run_worker(self) -> None:
        while not self._stopping:
            job = await self._queue.get()
            try:
                await self._process_job(job)
            finally:
                self._queue.task_done()

    async def _process_job(self, job: DownloadJob) -> None:
        while job.attempts <= self._retry_limit:
            if job.cancel_event.is_set():
                job.status = DownloadJobStatus.CANCELLED
                self._repository.mark_download_cancelled(
                    job.file_record_id,
                    job.attempts,
                )
                return

            job.attempts += 1
            job.status = DownloadJobStatus.RUNNING
            self._repository.mark_file_downloading(job.file_record_id)
            last_progress_update = 0.0

            async def progress(snapshot: ProgressSnapshot) -> None:
                nonlocal last_progress_update
                if job.cancel_event.is_set():
                    raise asyncio.CancelledError
                self._repository.update_download_progress(
                    file_id=job.file_record_id,
                    bytes_downloaded=snapshot.current,
                    total_bytes=snapshot.total,
                )
                now = time.monotonic()
                if now - last_progress_update < self._progress_interval_seconds:
                    return
                last_progress_update = now
                await self._safe_edit_message(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text=format_progress(snapshot),
                )

            try:
                await self._safe_edit_message(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text="Downloading\n\nQueued download started.",
                )
                result = await self._download_manager.download(
                    file_record_id=job.file_record_id,
                    metadata=job.metadata,
                    progress_callback=progress,
                )
                self._repository.mark_download_complete(
                    file_id=job.file_record_id,
                    local_path=str(result.path),
                    total_bytes=result.size,
                )
                self._repository.mark_file_awaiting_rename(job.file_record_id)
                job.status = DownloadJobStatus.COMPLETED
                await self._send_rename_prompt(job, result.filename)
                return
            except asyncio.CancelledError:
                if self._stopping:
                    raise
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
                return
            except DownloadError as exc:
                self._logger.warning("download attempt failed", extra={"event": "download_failed"})
                if job.attempts > self._retry_limit:
                    job.status = DownloadJobStatus.FAILED
                    self._repository.mark_download_failed(
                        job.file_record_id,
                        str(exc),
                        job.attempts,
                    )
                    await self._safe_edit_message(
                        chat_id=job.chat_id,
                        message_id=job.status_message_id,
                        text=f"Download failed: {exc}",
                    )
                    return
                await asyncio.sleep(min(job.attempts, 5))

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
            text=f"Downloaded {filename}.\nChoose how to name it before upload later.",
            reply_markup=keyboard,
        )

    async def _safe_edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        except TelegramApiError:
            self._logger.warning(
                "failed to edit progress message", extra={"event": "progress_edit_failed"}
            )


def _rename_callback(action: str, file_record_id: int) -> str:
    return f"{constants.RENAME_CALLBACK_PREFIX}:{action}:{file_record_id}"
