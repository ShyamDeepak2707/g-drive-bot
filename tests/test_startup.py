from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.lifecycle import shutdown, startup


class FakeBot:
    async def get_me(self) -> object:
        return type("BotUser", (), {"id": 12345})()


class FakeTelegramApplication:
    def __init__(self) -> None:
        self.bot = FakeBot()
        self.bot_data: dict[str, object] = {}
        self.handlers: list[object] = []
        self.error_handlers: list[object] = []
        self.updater = None
        self.running = False

    def add_handler(self, handler: object) -> None:
        self.handlers.append(handler)

    def add_error_handler(self, handler: object) -> None:
        self.error_handlers.append(handler)

    async def shutdown(self) -> None:
        return None


class FakePyrogramSession:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def is_running(self) -> bool:
        return self.started and not self.stopped


class FakeRequest:
    def execute(self) -> dict[str, object]:
        return {"user": {"emailAddress": "bot@example.com"}}


class FakeAboutResource:
    def get(self, **kwargs: object) -> FakeRequest:
        del kwargs
        return FakeRequest()


class FakeDriveService:
    def about(self) -> FakeAboutResource:
        return FakeAboutResource()


def test_startup_and_shutdown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("PYROGRAM_API_ID", "12345")
    monkeypatch.setenv("PYROGRAM_API_HASH", "api-hash")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("GOOGLE_AUTO_AUTH", "false")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "data" / "app.sqlite3"))
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "data" / "token.json"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "tmp"))
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    stale_part = temp_dir / "stale-download.part"
    stale_part.write_bytes(b"partial")
    pyrogram = FakePyrogramSession()
    telegram_application = FakeTelegramApplication()

    monkeypatch.setattr("app.lifecycle.create_pyrogram_client", lambda settings: pyrogram)
    monkeypatch.setattr("app.lifecycle.get_drive_service", lambda settings: FakeDriveService())
    monkeypatch.setattr("app.lifecycle.create_application", lambda settings: telegram_application)

    async def run_lifecycle() -> None:
        container = await startup()
        assert not stale_part.exists()
        assert container.database.is_connected
        assert container.telegram_application is not None
        assert container.pyrogram_client is pyrogram
        assert pyrogram.started
        assert container.download_manager is not None
        assert container.download_queue is not None
        assert container.upload_worker is not None
        assert container.startup_recovery_summary is not None
        assert container.health_service is not None
        assert container.admin_service is not None
        await shutdown(container)
        assert not container.database.is_connected
        assert pyrogram.stopped

    asyncio.run(run_lifecycle())
