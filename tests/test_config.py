from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_settings
from app.exceptions import ConfigurationError


def test_load_settings_creates_runtime_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.delenv("PYROGRAM_API_ID", raising=False)
    monkeypatch.delenv("PYROGRAM_API_HASH", raising=False)
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "data" / "app.sqlite3"))
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "data" / "token.json"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "123, 456")

    settings = load_settings(dotenv_path=tmp_path / ".env.missing")

    assert settings.sqlite_db_path.parent.exists()
    assert settings.pyrogram_workdir.exists()
    assert settings.downloads_dir.exists()
    assert settings.temp_dir.exists()
    assert settings.telegram_admin_user_ids == (123, 456)


def test_load_settings_requires_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(ConfigurationError):
        load_settings(dotenv_path=tmp_path / ".env.missing")


def test_load_settings_rejects_invalid_admin_user_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "123,not-an-id")

    with pytest.raises(ConfigurationError, match="TELEGRAM_ADMIN_USER_IDS"):
        load_settings(dotenv_path=tmp_path / ".env.missing")
