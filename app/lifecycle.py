from __future__ import annotations

import asyncio
import contextlib
import signal

from telegram.ext import Application

from app.config import load_settings
from app.database import DatabaseRepository, SQLiteDatabase
from app.download_manager import DownloadManager
from app.download_queue import DownloadQueue
from app.drive.auth import get_drive_service
from app.exceptions import TelegramError
from app.logging_config import configure_logging, get_logger
from app.pyrogram_client import PyrogramSessionManager, create_pyrogram_client
from app.services import ApplicationContainer
from app.task_manager import AsyncTaskManager
from app.telegram_bot import create_application, register_handlers
from app.utils.filesystem import cleanup_runtime_directory


async def startup() -> ApplicationContainer:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_file)
    logger = get_logger(__name__)
    logger.info("startup started", extra={"event": "startup_started"})

    database = SQLiteDatabase(settings.sqlite_db_path)
    database.initialize()
    repository = DatabaseRepository(database)
    task_manager = AsyncTaskManager(get_logger("app.task_manager"))

    pyrogram_client = create_pyrogram_client(settings)
    drive_service = get_drive_service(settings)
    if drive_service is None:
        logger.info("google drive authentication pending", extra={"event": "google_pending"})

    telegram_application = create_application(settings)
    download_manager: DownloadManager | None = None
    download_queue: DownloadQueue | None = None
    if pyrogram_client is not None:
        download_manager = DownloadManager(
            session=pyrogram_client,
            download_dir=settings.downloads_dir,
            temp_dir=settings.temp_dir,
        )
        download_queue = DownloadQueue(
            repository=repository,
            download_manager=download_manager,
            bot=telegram_application.bot,
            logger=get_logger("app.download_queue"),
        )
    register_handlers(
        application=telegram_application,
        repository=repository,
        download_queue=download_queue,
        logger=get_logger("app.telegram_bot"),
    )

    container = ApplicationContainer(
        settings=settings,
        logger=logger,
        database=database,
        repository=repository,
        task_manager=task_manager,
        pyrogram_client=pyrogram_client,
        download_manager=download_manager,
        download_queue=download_queue,
        drive_service=drive_service,
        telegram_application=telegram_application,
    )
    container.register_singletons()
    logger.info("startup completed", extra={"event": "startup_completed"})
    return container


async def start_telegram(application: Application) -> None:
    try:
        await application.initialize()
        await application.start()
        if application.updater is None:
            raise TelegramError("Telegram application has no updater configured for polling.")
        await application.updater.start_polling()
    except Exception as exc:
        raise TelegramError("Failed to start Telegram polling.") from exc


async def shutdown(container: ApplicationContainer | None) -> None:
    if container is None:
        return

    logger = container.logger
    logger.info("shutdown started", extra={"event": "shutdown_started"})

    await _stop_download_queue(container.download_queue)
    await _stop_telegram(container.telegram_application)
    await _stop_pyrogram(container.pyrogram_client)
    await container.task_manager.shutdown()
    container.database.close()
    cleanup_runtime_directory(container.settings.temp_dir)

    logger.info("shutdown completed", extra={"event": "shutdown_completed"})


async def run() -> None:
    container: ApplicationContainer | None = None
    try:
        container = await startup()
        if container.telegram_application is None:
            raise TelegramError("Telegram application was not created.")
        await start_telegram(container.telegram_application)
        if container.download_queue is not None:
            container.download_queue.start()
        container.logger.info("telegram started", extra={"event": "telegram_started"})
        await _wait_for_shutdown_signal()
    finally:
        await shutdown(container)


async def _stop_telegram(application: Application | None) -> None:
    if application is None:
        return

    if application.updater is not None and application.updater.running:
        await application.updater.stop()
    if application.running:
        await application.stop()
    with contextlib.suppress(RuntimeError):
        await application.shutdown()


async def _stop_download_queue(queue: DownloadQueue | None) -> None:
    if queue is None:
        return
    await queue.stop()


async def _stop_pyrogram(client: PyrogramSessionManager | None) -> None:
    if client is None:
        return
    if client.is_running():
        await client.stop()


async def _wait_for_shutdown_signal() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for signame in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signame, None)
        if signal_value is None:
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal_value, stop_event.set)

    await stop_event.wait()
