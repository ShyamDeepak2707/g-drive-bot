from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.lifecycle import shutdown, startup


def test_startup_and_shutdown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("PYROGRAM_API_ID", "12345")
    monkeypatch.setenv("PYROGRAM_API_HASH", "hash")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("GOOGLE_AUTO_AUTH", "false")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "data" / "app.sqlite3"))
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "data" / "token.json"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setenv("PYROGRAM_WORKDIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "tmp"))

    async def run_lifecycle() -> None:
        container = await startup()
        assert container.database.is_connected
        assert container.telegram_application is not None
        assert container.pyrogram_client is not None
        await shutdown(container)
        assert not container.database.is_connected

    asyncio.run(run_lifecycle())
