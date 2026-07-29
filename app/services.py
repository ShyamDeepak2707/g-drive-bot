from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar, cast

from googleapiclient.discovery import Resource
from telegram.ext import Application

from app.admin_commands import AdminCommandService
from app.admin_service import AdminService
from app.config import Settings
from app.database import DatabaseRepository, SQLiteDatabase
from app.download_manager import DownloadManager
from app.download_queue import DownloadQueue
from app.drive.browser import DriveFolderBrowser
from app.health import HealthService
from app.shutdown_control import ShutdownController
from app.startup_recovery import StartupRecoverySummary
from app.task_manager import AsyncTaskManager
from app.upload_worker import UploadWorker

if TYPE_CHECKING:
    from app.pyrogram_client import PyrogramBotSessionManager, PyrogramSessionManager
    from app.render_health import RenderHealthServer

T = TypeVar("T")


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, object] = {}

    def set(self, key: str, service: object) -> None:
        self._services[key] = service

    def get(self, key: str, expected_type: type[T]) -> T:
        service = self._services[key]
        if not isinstance(service, expected_type):
            raise TypeError(f"Service '{key}' is not a {expected_type.__name__}.")
        return cast(T, service)


@dataclass
class ApplicationContainer:
    settings: Settings
    logger: logging.Logger
    database: SQLiteDatabase
    repository: DatabaseRepository
    task_manager: AsyncTaskManager
    pyrogram_client: PyrogramSessionManager | None = None
    pyrogram_bot_client: PyrogramBotSessionManager | None = None
    download_manager: DownloadManager | None = None
    download_queue: DownloadQueue | None = None
    upload_worker: UploadWorker | None = None
    drive_service: Resource | None = None
    folder_browser: DriveFolderBrowser | None = None
    startup_recovery_summary: StartupRecoverySummary | None = None
    health_service: HealthService | None = None
    admin_service: AdminService | None = None
    admin_command_service: AdminCommandService | None = None
    shutdown_controller: ShutdownController | None = None
    telegram_application: Application | None = None
    render_health_server: RenderHealthServer | None = None
    registry: ServiceRegistry = field(default_factory=ServiceRegistry)

    def register_singletons(self) -> None:
        self.registry.set("settings", self.settings)
        self.registry.set("logger", self.logger)
        self.registry.set("database", self.database)
        self.registry.set("repository", self.repository)
        self.registry.set("task_manager", self.task_manager)
        if self.pyrogram_client is not None:
            self.registry.set("pyrogram_client", self.pyrogram_client)
        if self.pyrogram_bot_client is not None:
            self.registry.set("pyrogram_bot_client", self.pyrogram_bot_client)
        if self.download_manager is not None:
            self.registry.set("download_manager", self.download_manager)
        if self.download_queue is not None:
            self.registry.set("download_queue", self.download_queue)
        if self.upload_worker is not None:
            self.registry.set("upload_worker", self.upload_worker)
        if self.drive_service is not None:
            self.registry.set("drive_service", self.drive_service)
        if self.folder_browser is not None:
            self.registry.set("folder_browser", self.folder_browser)
        if self.startup_recovery_summary is not None:
            self.registry.set("startup_recovery_summary", self.startup_recovery_summary)
        if self.health_service is not None:
            self.registry.set("health_service", self.health_service)
        if self.admin_service is not None:
            self.registry.set("admin_service", self.admin_service)
        if self.admin_command_service is not None:
            self.registry.set("admin_command_service", self.admin_command_service)
        if self.shutdown_controller is not None:
            self.registry.set("shutdown_controller", self.shutdown_controller)
        if self.telegram_application is not None:
            self.registry.set("telegram_application", self.telegram_application)
        if self.render_health_server is not None:
            self.registry.set("render_health_server", self.render_health_server)
