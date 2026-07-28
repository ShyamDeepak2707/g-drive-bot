from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app import constants
from app.cloud_credentials import materialize_cloud_credentials
from app.config import Settings, load_settings
from app.exceptions import StartupValidationError


def test_materialize_cloud_credentials_writes_valid_base64(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    env = {
        "GOOGLE_CREDENTIALS_BASE64": _encode(b'{"installed": {}}'),
        "PYROGRAM_SESSION_BASE64": _encode(b"session-bytes"),
    }

    summary = materialize_cloud_credentials(settings, env=env)

    assert summary.google_credentials_written is True
    assert summary.pyrogram_session_written is True
    assert settings.google_credentials_file.read_bytes() == b'{"installed": {}}'
    assert (
        settings.pyrogram_workdir / f"{settings.pyrogram_session_name}.session"
    ).read_bytes() == b"session-bytes"


def test_materialize_cloud_credentials_rejects_invalid_base64(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(StartupValidationError, match="GOOGLE_CREDENTIALS_BASE64"):
        materialize_cloud_credentials(settings, env={"GOOGLE_CREDENTIALS_BASE64": "not base64"})

    assert not settings.google_credentials_file.exists()


def test_materialize_cloud_credentials_reports_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)

    def fail_write_bytes(self: Path, data: bytes) -> int:
        del self, data
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "write_bytes", fail_write_bytes)

    with pytest.raises(StartupValidationError, match="could not be written"):
        materialize_cloud_credentials(
            settings,
            env={"PYROGRAM_SESSION_BASE64": _encode(b"session-bytes")},
        )


def test_materialize_cloud_credentials_falls_back_to_local_files_when_env_absent(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    summary = materialize_cloud_credentials(settings, env={})

    assert summary.google_credentials_written is False
    assert summary.pyrogram_session_written is False
    assert not settings.google_credentials_file.exists()
    assert not (settings.pyrogram_workdir / f"{settings.pyrogram_session_name}.session").exists()


def test_load_settings_uses_cloud_paths_when_base64_env_is_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_minimum_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_BASE64", _encode(b"credentials"))
    monkeypatch.setenv("PYROGRAM_SESSION_BASE64", _encode(b"session"))

    settings = load_settings(dotenv_path=tmp_path / ".env.missing")

    assert settings.google_credentials_file == Path(constants.CLOUD_GOOGLE_CREDENTIALS_FILE)
    assert settings.pyrogram_workdir == Path(constants.CLOUD_PYROGRAM_WORKDIR)
    assert settings.pyrogram_session_name == constants.CLOUD_PYROGRAM_SESSION_NAME


def test_load_settings_keeps_existing_local_file_behavior_when_base64_env_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")
    _set_minimum_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("PYROGRAM_SESSION_NAME", "local-session")
    monkeypatch.setenv("PYROGRAM_WORKDIR", str(tmp_path / "local-sessions"))
    monkeypatch.delenv("GOOGLE_CREDENTIALS_BASE64", raising=False)
    monkeypatch.delenv("PYROGRAM_SESSION_BASE64", raising=False)

    settings = load_settings(dotenv_path=tmp_path / ".env.missing")

    assert settings.google_credentials_file == credentials
    assert settings.pyrogram_workdir == tmp_path / "local-sessions"
    assert settings.pyrogram_session_name == "local-session"


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _set_minimum_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("PYROGRAM_API_ID", "12345")
    monkeypatch.setenv("PYROGRAM_API_HASH", "api-hash")
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "data" / "token.json"))
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "data" / "app.sqlite3"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "tmp"))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="development",
        log_level="INFO",
        log_file=tmp_path / "logs" / "app.log",
        telegram_bot_token="123456789:abcdefghijklmnopqrstuvwxyzABCDE",
        telegram_admin_user_ids=(123,),
        pyrogram_api_id=12345,
        pyrogram_api_hash="api-hash",
        pyrogram_session_name="g_drive_bot",
        pyrogram_workdir=tmp_path / "sessions",
        pyrogram_max_concurrent_transmissions=4,
        google_credentials_file=tmp_path / "credentials.json",
        google_token_file=tmp_path / "tokens" / "token.json",
        google_scopes=("https://www.googleapis.com/auth/drive.file",),
        google_auto_auth=False,
        sqlite_db_path=tmp_path / "data" / "app.sqlite3",
        downloads_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        folder_browser_page_size=8,
        folder_browser_cache_ttl_seconds=300,
        folder_recent_limit=5,
    )
