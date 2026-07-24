from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TypeVar, cast

from googleapiclient.discovery import Resource
from telegram.ext import Application

from app.config import Settings
from app.database import DatabaseRepository, SQLiteDatabase
from app.download_manager import DownloadManager
from app.download_queue import DownloadQueue
from app.pyrogram_client import PyrogramSessionManager
from app.task_manager import AsyncTaskManager

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
    download_manager: DownloadManager | None = None
    download_queue: DownloadQueue | None = None
    drive_service: Resource | None = None
    telegram_application: Application | None = None
    registry: ServiceRegistry = field(default_factory=ServiceRegistry)

    def register_singletons(self) -> None:
        self.registry.set("settings", self.settings)
        self.registry.set("logger", self.logger)
        self.registry.set("database", self.database)
        self.registry.set("repository", self.repository)
        self.registry.set("task_manager", self.task_manager)
        if self.pyrogram_client is not None:
            self.registry.set("pyrogram_client", self.pyrogram_client)
        if self.download_manager is not None:
            self.registry.set("download_manager", self.download_manager)
        if self.download_queue is not None:
            self.registry.set("download_queue", self.download_queue)
        if self.drive_service is not None:
            self.registry.set("drive_service", self.drive_service)
        if self.telegram_application is not None:
            self.registry.set("telegram_application", self.telegram_application)
