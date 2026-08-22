from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from googleapiclient.discovery import Resource
from telegram.ext import Application

from app.config import Settings
from app.database import SQLiteDatabase
from app.exceptions import StartupValidationError


class PyrogramSession(Protocol):
    async def start(self) -> None: ...


@dataclass(frozen=True)
class StartupValidationSummary:
    environment: str
    database: str
    directories: str
    telegram_bot_api: str
    pyrogram_session: str
    google_drive: str


@dataclass(frozen=True)
class _ValidationStep:
    key: str
    label: str


_STEPS = (
    _ValidationStep("environment", "Configuration"),
    _ValidationStep("database", "Database"),
    _ValidationStep("directories", "Directories"),
    _ValidationStep("telegram_bot_api", "Telegram Bot"),
    _ValidationStep("pyrogram_session", "Pyrogram"),
    _ValidationStep("google_drive", "Google Drive"),
)


async def validate_startup_configuration(
    *,
    settings: Settings,
    database: SQLiteDatabase,
    telegram_application: Application,
    pyrogram_client: PyrogramSession | None,
    drive_service: Resource | None,
    logger: logging.Logger,
) -> StartupValidationSummary:
    statuses = dict.fromkeys((step.key for step in _STEPS), "pending")
    logger.info("startup validation started", extra={"event": "startup_validation_started"})

    await _run_validation_step(
        step=_STEPS[0],
        statuses=statuses,
        logger=logger,
        validator=lambda: _run_sync_validation(lambda: _validate_required_settings(settings)),
    )
    await _run_validation_step(
        step=_STEPS[1],
        statuses=statuses,
        logger=logger,
        validator=lambda: _run_sync_validation(lambda: _validate_database(database)),
    )
    await _run_validation_step(
        step=_STEPS[2],
        statuses=statuses,
        logger=logger,
        validator=lambda: _run_sync_validation(lambda: _validate_directories(settings)),
    )
    await _run_validation_step(
        step=_STEPS[3],
        statuses=statuses,
        logger=logger,
        validator=lambda: _validate_telegram_bot_api(telegram_application),
    )
    await _run_validation_step(
        step=_STEPS[4],
        statuses=statuses,
        logger=logger,
        validator=lambda: _validate_pyrogram_session(pyrogram_client),
    )
    await _run_validation_step(
        step=_STEPS[5],
        statuses=statuses,
        logger=logger,
        validator=lambda: _run_sync_validation(lambda: _validate_google_drive(drive_service)),
    )

    summary = StartupValidationSummary(
        environment=statuses["environment"],
        database=statuses["database"],
        directories=statuses["directories"],
        telegram_bot_api=statuses["telegram_bot_api"],
        pyrogram_session=statuses["pyrogram_session"],
        google_drive=statuses["google_drive"],
    )
    _log_validation_summary(logger=logger, statuses=statuses, aborted=False)
    logger.info(
        "startup validation completed",
        extra={
            "event": "startup_validation_completed",
            "environment": summary.environment,
            "database": summary.database,
            "directories": summary.directories,
            "telegram_bot_api": summary.telegram_bot_api,
            "pyrogram_session": summary.pyrogram_session,
            "google_drive": summary.google_drive,
        },
    )
    return summary


async def _run_validation_step(
    *,
    step: _ValidationStep,
    statuses: dict[str, str],
    logger: logging.Logger,
    validator: Callable[[], Awaitable[None]],
) -> None:
    try:
        await validator()
    except StartupValidationError as exc:
        statuses[step.key] = "failed"
        logger.error(
            "startup validation check failed",
            extra={
                "event": "startup_validation_check_failed",
                "subsystem": step.label,
                "status": "failed",
                "reason": str(exc),
            },
        )
        _log_validation_summary(
            logger=logger,
            statuses=statuses,
            aborted=True,
            failed_subsystem=step.label,
            failure_reason=str(exc),
        )
        logger.error(
            "startup aborted",
            extra={
                "event": "startup_aborted",
                "failed_subsystem": step.label,
                "reason": str(exc),
            },
        )
        raise
    statuses[step.key] = "ok"
    logger.info(
        "startup validation check passed",
        extra={
            "event": "startup_validation_check_passed",
            "subsystem": step.label,
            "status": "ok",
        },
    )


async def _run_sync_validation(validator: Callable[[], None]) -> None:
    validator()


def _log_validation_summary(
    *,
    logger: logging.Logger,
    statuses: dict[str, str],
    aborted: bool,
    failed_subsystem: str | None = None,
    failure_reason: str | None = None,
) -> None:
    lines = ["Startup validation", ""]
    for step in _STEPS:
        status = statuses[step.key]
        marker = _status_marker(status)
        lines.append(f"{marker} {step.label}")
        if status == "failed" and step.label == failed_subsystem and failure_reason:
            lines.append(f"   {failure_reason}")
    if aborted:
        lines.extend(("", "Startup aborted."))

    logger.info(
        "\n".join(lines),
        extra={
            "event": "startup_validation_summary",
            "aborted": aborted,
            "failed_subsystem": failed_subsystem,
        },
    )


def _status_marker(status: str) -> str:
    if status == "ok":
        return "[OK]"
    if status == "failed":
        return "[FAILED]"
    return "[PENDING]"


def _validate_required_settings(settings: Settings) -> None:
    missing: list[str] = []
    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if settings.pyrogram_api_id is None:
        missing.append("PYROGRAM_API_ID")
    if not settings.pyrogram_api_hash:
        missing.append("PYROGRAM_API_HASH")
    if not settings.google_scopes:
        missing.append("GOOGLE_SCOPES")

    if missing:
        names = ", ".join(missing)
        raise StartupValidationError(
            f"Missing required startup configuration: {names}. "
            "Set these environment variables before starting the bot."
        )


def _validate_database(database: SQLiteDatabase) -> None:
    try:
        database.execute("SELECT 1").fetchone()
    except Exception as exc:
        raise StartupValidationError(
            f"SQLite database is not reachable at '{database.db_path}'. "
            "Check the database path and file permissions."
        ) from exc


def _validate_directories(settings: Settings) -> None:
    directories = {
        "SQLite database directory": settings.sqlite_db_path.parent,
        "Google token directory": settings.google_token_file.parent,
        "log directory": settings.log_file.parent,
        "Pyrogram session directory": settings.pyrogram_workdir,
        "downloads directory": settings.downloads_dir,
        "temporary directory": settings.temp_dir,
    }
    for label, path in directories.items():
        _verify_writable_directory(path, label)


async def _validate_telegram_bot_api(application: Application) -> None:
    try:
        me = await application.bot.get_me()
    except Exception as exc:
        raise StartupValidationError(
            "Telegram Bot API connectivity check failed. "
            "Verify TELEGRAM_BOT_TOKEN and network connectivity."
        ) from exc
    if getattr(me, "id", None) is None:
        raise StartupValidationError(
            "Telegram Bot API getMe returned an invalid response. Verify TELEGRAM_BOT_TOKEN."
        )


async def _validate_pyrogram_session(pyrogram_client: PyrogramSession | None) -> None:
    if pyrogram_client is None:
        raise StartupValidationError(
            "Pyrogram session is required. Set PYROGRAM_API_ID and PYROGRAM_API_HASH, "
            "then configure PYROGRAM_SESSION_STRING or authorize the configured "
            "PYROGRAM_SESSION_NAME as a user session."
        )
    try:
        await pyrogram_client.start()
    except Exception as exc:
        raise StartupValidationError(
            "Pyrogram session startup failed. Re-authorize the session and verify "
            "PYROGRAM_API_ID, PYROGRAM_API_HASH, PYROGRAM_SESSION_NAME, and network access."
        ) from exc


def _validate_google_drive(drive_service: Resource | None) -> None:
    if drive_service is None:
        raise StartupValidationError(
            "Google Drive authentication is required. Create credentials.json and a valid "
            "token file, or run with GOOGLE_AUTO_AUTH=true locally to authorize."
        )
    try:
        drive_service.about().get(fields="user").execute()
    except Exception as exc:
        raise StartupValidationError(
            "Google Drive authentication check failed. Refresh or recreate the Google token "
            "and verify Drive API access."
        ) from exc


def _verify_writable_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise NotADirectoryError(path)
        probe = path / ".startup-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise StartupValidationError(
            f"{label} is not writable at '{path}'. Check directory permissions."
        ) from exc
