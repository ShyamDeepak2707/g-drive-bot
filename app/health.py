from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from googleapiclient.discovery import Resource
from telegram.ext import Application

from app.download_queue import DownloadQueue
from app.startup_recovery import StartupRecoverySummary
from app.upload_worker import UploadWorker


class DatabaseHealthStore(Protocol):
    @property
    def is_connected(self) -> bool: ...

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> Any: ...


class RepositoryHealthStore(Protocol):
    def count_pending_uploads(self) -> int: ...


class PyrogramSession(Protocol):
    def is_running(self) -> bool: ...


@dataclass(frozen=True)
class QueueHealth:
    pending_downloads: int
    active_downloads: int
    pending_uploads: int
    active_uploads: int


@dataclass(frozen=True)
class HealthSnapshot:
    startup_time: datetime
    uptime_seconds: float
    telegram_bot_connected: bool
    pyrogram_connected: bool
    google_drive_authenticated: bool
    database_connected: bool
    queues: QueueHealth
    startup_recovery: StartupRecoverySummary


class HealthService:
    def __init__(
        self,
        *,
        startup_time: datetime,
        database: DatabaseHealthStore,
        repository: RepositoryHealthStore,
        telegram_application: Application | None,
        pyrogram_client: PyrogramSession | None,
        drive_service: Resource | None,
        download_queue: DownloadQueue | None,
        upload_worker: UploadWorker | None,
        startup_recovery_summary: StartupRecoverySummary,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._startup_time = startup_time
        self._database = database
        self._repository = repository
        self._telegram_application = telegram_application
        self._pyrogram_client = pyrogram_client
        self._drive_service = drive_service
        self._download_queue = download_queue
        self._upload_worker = upload_worker
        self._startup_recovery_summary = startup_recovery_summary
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def snapshot(self) -> HealthSnapshot:
        now = self._clock()
        return HealthSnapshot(
            startup_time=self._startup_time,
            uptime_seconds=max(0.0, (now - self._startup_time).total_seconds()),
            telegram_bot_connected=self._telegram_bot_connected(),
            pyrogram_connected=self._pyrogram_connected(),
            google_drive_authenticated=self._drive_service is not None,
            database_connected=self._database_connected(),
            queues=self._queue_health(),
            startup_recovery=self._startup_recovery_summary,
        )

    def _telegram_bot_connected(self) -> bool:
        application = self._telegram_application
        if application is None:
            return False
        return bool(getattr(application, "bot", None)) and bool(
            getattr(application, "running", False)
        )

    def _pyrogram_connected(self) -> bool:
        if self._pyrogram_client is None:
            return False
        try:
            return self._pyrogram_client.is_running()
        except Exception:
            return False

    def _database_connected(self) -> bool:
        if not self._database.is_connected:
            return False
        try:
            self._database.execute("SELECT 1").fetchone()
        except Exception:
            return False
        return True

    def _queue_health(self) -> QueueHealth:
        pending_downloads = 0
        active_downloads = 0
        if self._download_queue is not None:
            download_snapshot = self._download_queue.snapshot()
            pending_downloads = download_snapshot.queue_length
            active_downloads = 1 if download_snapshot.current_download is not None else 0

        try:
            pending_uploads = self._repository.count_pending_uploads()
        except Exception:
            pending_uploads = 0
        active_uploads = 0
        if self._upload_worker is not None:
            upload_snapshot = self._upload_worker.snapshot()
            active_uploads = 1 if upload_snapshot.current_upload is not None else 0

        return QueueHealth(
            pending_downloads=pending_downloads,
            active_downloads=active_downloads,
            pending_uploads=pending_uploads,
            active_uploads=active_uploads,
        )
