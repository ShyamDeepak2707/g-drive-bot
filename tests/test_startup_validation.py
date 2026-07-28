from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from app.config import Settings
from app.database import SQLiteDatabase
from app.exceptions import StartupValidationError
from app.startup_validation import validate_startup_configuration


class FakeBot:
    def __init__(self, *, fail_get_me: bool = False) -> None:
        self.fail_get_me = fail_get_me

    async def get_me(self) -> object:
        if self.fail_get_me:
            raise RuntimeError("telegram unavailable")
        return type("BotUser", (), {"id": 12345})()


class FakeApplication:
    def __init__(self, *, fail_get_me: bool = False) -> None:
        self.bot = FakeBot(fail_get_me=fail_get_me)


class FakePyrogramSession:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("pyrogram unavailable")


class FakeRequest:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def execute(self) -> dict[str, object]:
        if self.fail:
            raise RuntimeError("drive unavailable")
        return {"user": {"emailAddress": "bot@example.com"}}


class FakeAboutResource:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def get(self, **kwargs: object) -> FakeRequest:
        del kwargs
        return FakeRequest(fail=self.fail)


class FakeDriveService:
    def __init__(self, *, fail_about: bool = False) -> None:
        self.fail_about = fail_about

    def about(self) -> FakeAboutResource:
        return FakeAboutResource(fail=self.fail_about)


def test_validate_startup_configuration_succeeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = SQLiteDatabase(tmp_path / "data" / "app.sqlite3")
    database.initialize()
    pyrogram = FakePyrogramSession()

    async def run() -> None:
        with caplog.at_level(logging.INFO):
            summary = await validate_startup_configuration(
                settings=_settings(tmp_path),
                database=database,
                telegram_application=FakeApplication(),  # type: ignore[arg-type]
                pyrogram_client=pyrogram,
                drive_service=FakeDriveService(),  # type: ignore[arg-type]
                logger=logging.getLogger("test.startup_validation"),
            )
        assert summary.google_drive == "ok"

    try:
        asyncio.run(run())
    finally:
        database.close()

    assert pyrogram.start_calls == 1
    assert any(
        getattr(record, "event", None) == "startup_validation_completed"
        for record in caplog.records
    )
    summary = _log_message(caplog, "startup_validation_summary")
    assert "[OK] Configuration" in summary
    assert "[OK] Database" in summary
    assert "[OK] Directories" in summary
    assert "[OK] Telegram Bot" in summary
    assert "[OK] Pyrogram" in summary
    assert "[OK] Google Drive" in summary


def test_validate_startup_configuration_requires_pyrogram(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "data" / "app.sqlite3")
    database.initialize()

    async def run() -> None:
        with pytest.raises(StartupValidationError, match="Missing required startup configuration"):
            await validate_startup_configuration(
                settings=_settings(tmp_path, pyrogram_api_id=None),
                database=database,
                telegram_application=FakeApplication(),  # type: ignore[arg-type]
                pyrogram_client=None,
                drive_service=FakeDriveService(),  # type: ignore[arg-type]
                logger=logging.getLogger("test.startup_validation"),
            )

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_validate_startup_configuration_requires_pyrogram_session(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "data" / "app.sqlite3")
    database.initialize()

    async def run() -> None:
        with pytest.raises(StartupValidationError, match="Pyrogram session is required"):
            await validate_startup_configuration(
                settings=_settings(tmp_path),
                database=database,
                telegram_application=FakeApplication(),  # type: ignore[arg-type]
                pyrogram_client=None,
                drive_service=FakeDriveService(),  # type: ignore[arg-type]
                logger=logging.getLogger("test.startup_validation"),
            )

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_validate_startup_configuration_requires_google_drive(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "data" / "app.sqlite3")
    database.initialize()

    async def run() -> None:
        with pytest.raises(StartupValidationError, match="Google Drive authentication is required"):
            await validate_startup_configuration(
                settings=_settings(tmp_path),
                database=database,
                telegram_application=FakeApplication(),  # type: ignore[arg-type]
                pyrogram_client=FakePyrogramSession(),
                drive_service=None,
                logger=logging.getLogger("test.startup_validation"),
            )

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_validate_startup_configuration_reports_telegram_failure(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "data" / "app.sqlite3")
    database.initialize()

    async def run() -> None:
        with pytest.raises(StartupValidationError, match="Telegram Bot API connectivity"):
            await validate_startup_configuration(
                settings=_settings(tmp_path),
                database=database,
                telegram_application=FakeApplication(fail_get_me=True),  # type: ignore[arg-type]
                pyrogram_client=FakePyrogramSession(),
                drive_service=FakeDriveService(),  # type: ignore[arg-type]
                logger=logging.getLogger("test.startup_validation"),
            )

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_validate_startup_configuration_reports_drive_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = SQLiteDatabase(tmp_path / "data" / "app.sqlite3")
    database.initialize()

    async def run() -> None:
        with caplog.at_level(logging.INFO):
            with pytest.raises(
                StartupValidationError, match="Google Drive authentication check failed"
            ):
                await validate_startup_configuration(
                    settings=_settings(tmp_path),
                    database=database,
                    telegram_application=FakeApplication(),  # type: ignore[arg-type]
                    pyrogram_client=FakePyrogramSession(),
                    drive_service=FakeDriveService(fail_about=True),  # type: ignore[arg-type]
                    logger=logging.getLogger("test.startup_validation"),
                )

    try:
        asyncio.run(run())
    finally:
        database.close()

    summary = _log_message(caplog, "startup_validation_summary")
    assert "[OK] Configuration" in summary
    assert "[OK] Database" in summary
    assert "[OK] Directories" in summary
    assert "[OK] Telegram Bot" in summary
    assert "[OK] Pyrogram" in summary
    assert "[FAILED] Google Drive" in summary
    assert "Google Drive authentication check failed" in summary
    assert "Startup aborted." in summary
    assert any(getattr(record, "event", None) == "startup_aborted" for record in caplog.records)


def _log_message(caplog: pytest.LogCaptureFixture, event: str) -> str:
    for record in caplog.records:
        if getattr(record, "event", None) == event:
            return record.getMessage()
    raise AssertionError(f"Missing log event: {event}")


def _settings(
    tmp_path: Path,
    *,
    pyrogram_api_id: int | None = 12345,
    pyrogram_api_hash: str | None = "api-hash",
    google_scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/drive.file",),
) -> Settings:
    return Settings(
        app_env="development",
        log_level="INFO",
        log_file=tmp_path / "logs" / "app.log",
        telegram_bot_token="123456789:abcdefghijklmnopqrstuvwxyzABCDE",
        pyrogram_api_id=pyrogram_api_id,
        pyrogram_api_hash=pyrogram_api_hash,
        pyrogram_session_name="test-session",
        pyrogram_workdir=tmp_path / "sessions",
        pyrogram_max_concurrent_transmissions=4,
        google_credentials_file=tmp_path / "credentials.json",
        google_token_file=tmp_path / "tokens" / "token.json",
        google_scopes=google_scopes,
        google_auto_auth=False,
        sqlite_db_path=tmp_path / "data" / "app.sqlite3",
        downloads_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        folder_browser_page_size=10,
        folder_browser_cache_ttl_seconds=60,
        folder_recent_limit=5,
    )
