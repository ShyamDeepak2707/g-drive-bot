from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import UTC, datetime

from telegram.ext import Application

from app.admin_service import AdminService
from app.config import load_settings
from app.database import DatabaseRepository, SQLiteDatabase
from app.download_manager import DownloadManager
from app.download_queue import DownloadQueue
from app.drive.auth import get_drive_service
from app.drive.browser import DriveFolderBrowser
from app.exceptions import TelegramError
from app.health import HealthService
from app.logging_config import configure_logging, get_logger
from app.pyrogram_client import PyrogramSessionManager, create_pyrogram_client
from app.services import ApplicationContainer
from app.startup_recovery import run_startup_recovery
from app.startup_validation import validate_startup_configuration
from app.task_manager import AsyncTaskManager
from app.telegram_bot import configure_bot_commands, create_application, register_handlers
from app.upload_worker import GoogleDriveUploader, UploadWorker
from app.utils.filesystem import cleanup_runtime_directory


async def startup() -> ApplicationContainer:
    startup_time = datetime.now(tz=UTC)
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_file)
    logger = get_logger(__name__)
    logger.info("startup started", extra={"event": "startup_started"})
    cleanup_runtime_directory(settings.temp_dir, logger)

    database = SQLiteDatabase(settings.sqlite_db_path)
    database.initialize()
    repository = DatabaseRepository(database)
    task_manager = AsyncTaskManager(get_logger("app.task_manager"))

    pyrogram_client = None
    try:
        pyrogram_client = create_pyrogram_client(settings)
        drive_service = get_drive_service(settings)
        telegram_application = create_application(settings)
        await validate_startup_configuration(
            settings=settings,
            database=database,
            telegram_application=telegram_application,
            pyrogram_client=pyrogram_client,
            drive_service=drive_service,
            logger=logger,
        )
    except Exception:
        logger.exception("startup validation failed", extra={"event": "startup_validation_failed"})
        await _stop_pyrogram(pyrogram_client)
        database.close()
        raise

    assert drive_service is not None
    folder_browser = DriveFolderBrowser(
        service=drive_service,
        page_size=settings.folder_browser_page_size,
        cache_ttl_seconds=settings.folder_browser_cache_ttl_seconds,
    )

    download_manager = DownloadManager(
        bot=telegram_application.bot,
        pyrogram_session=pyrogram_client,
        download_dir=settings.downloads_dir,
        temp_dir=settings.temp_dir,
        logger=get_logger("app.download_manager"),
    )
    download_queue = DownloadQueue(
        repository=repository,
        download_manager=download_manager,
        bot=telegram_application.bot,
        logger=get_logger("app.download_queue"),
    )
    upload_worker = UploadWorker(
        repository=repository,
        logger=get_logger("app.upload_worker"),
        uploader=GoogleDriveUploader(drive_service) if drive_service is not None else None,
    )
    startup_recovery_summary = await run_startup_recovery(
        download_queue=download_queue,
        upload_worker=upload_worker,
        logger=logger,
    )
    health_service = HealthService(
        startup_time=startup_time,
        database=database,
        repository=repository,
        telegram_application=telegram_application,
        pyrogram_client=pyrogram_client,
        drive_service=drive_service,
        download_queue=download_queue,
        upload_worker=upload_worker,
        startup_recovery_summary=startup_recovery_summary,
    )
    admin_service = AdminService(
        health_service=health_service,
        repository=repository,
        download_queue=download_queue,
        upload_worker=upload_worker,
        temp_dir=settings.temp_dir,
        downloads_dir=settings.downloads_dir,
    )
    register_handlers(
        application=telegram_application,
        repository=repository,
        download_queue=download_queue,
        upload_worker=upload_worker,
        folder_browser=folder_browser,
        folder_recent_limit=settings.folder_recent_limit,
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
        upload_worker=upload_worker,
        drive_service=drive_service,
        folder_browser=folder_browser,
        startup_recovery_summary=startup_recovery_summary,
        health_service=health_service,
        admin_service=admin_service,
        telegram_application=telegram_application,
    )
    container.register_singletons()
    logger.info("startup completed", extra={"event": "startup_completed"})
    return container


async def start_telegram(application: Application) -> None:
    try:
        await application.initialize()
        await configure_bot_commands(application)
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
    await _stop_upload_worker(container.upload_worker)
    await _stop_telegram(container.telegram_application)
    await _stop_pyrogram(container.pyrogram_client)
    await container.task_manager.shutdown()
    container.database.close()
    cleanup_runtime_directory(container.settings.temp_dir, logger)

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
        if container.upload_worker is not None:
            container.upload_worker.start()
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


async def _stop_upload_worker(worker: UploadWorker | None) -> None:
    if worker is None:
        return
    await worker.stop()


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
