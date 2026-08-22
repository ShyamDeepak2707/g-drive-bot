from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from app.cli import health_command, run_health_check
from app.database import SQLiteDatabase


def test_health_command_exits_zero_when_critical_subsystems_are_healthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _healthy_env(tmp_path)
    _create_database(Path(env["SQLITE_DB_PATH"]))
    _create_runtime_files(env)
    monkeypatch.setattr("app.cli._application_process_running", lambda: True)
    monkeypatch.setattr(
        "app.cli._container_startup_time",
        lambda now: datetime(2026, 7, 28, 10, 0, 0, tzinfo=UTC),
    )
    output = StringIO()

    exit_code = health_command(env=env, stdout=output)

    assert exit_code == 0
    assert "healthy: all critical subsystems are available" in output.getvalue()


def test_health_command_exits_nonzero_and_prints_failing_subsystems(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _healthy_env(tmp_path)
    monkeypatch.setattr("app.cli._application_process_running", lambda: False)
    output = StringIO()

    exit_code = health_command(env=env, stdout=output)

    assert exit_code == 1
    text = output.getvalue()
    assert "unhealthy:" in text
    assert "- Application" in text
    assert "- Telegram Bot" in text
    assert "- Pyrogram" in text
    assert "- Google Drive" in text
    assert "- Database" in text


def test_run_health_check_uses_read_only_database_without_creating_missing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _healthy_env(tmp_path)
    missing_database = Path(env["SQLITE_DB_PATH"])
    monkeypatch.setattr("app.cli._application_process_running", lambda: True)

    result = run_health_check(env)

    assert not result.is_healthy
    assert "Database" in result.failures
    assert not missing_database.exists()


def test_health_check_accepts_pyrogram_session_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _healthy_env(tmp_path)
    env["PYROGRAM_SESSION_STRING"] = "session-string"
    _create_database(Path(env["SQLITE_DB_PATH"]))
    _create_runtime_files(env, create_pyrogram_session=False)
    monkeypatch.setattr("app.cli._application_process_running", lambda: True)

    result = run_health_check(env)

    assert result.snapshot.pyrogram_connected is True
    assert "Pyrogram" not in result.failures


def _healthy_env(tmp_path: Path) -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyzABCDE",
        "SQLITE_DB_PATH": str(tmp_path / "data" / "app.sqlite3"),
        "GOOGLE_CREDENTIALS_FILE": str(tmp_path / "data" / "credentials.json"),
        "GOOGLE_TOKEN_FILE": str(tmp_path / "data" / "token.json"),
        "PYROGRAM_WORKDIR": str(tmp_path / "data" / "sessions"),
        "PYROGRAM_SESSION_NAME": "g_drive_bot",
    }


def _create_database(path: Path) -> None:
    database = SQLiteDatabase(path)
    database.initialize()
    database.close()


def _create_runtime_files(env: dict[str, str], *, create_pyrogram_session: bool = True) -> None:
    credentials = Path(env["GOOGLE_CREDENTIALS_FILE"])
    token = Path(env["GOOGLE_TOKEN_FILE"])
    session = Path(env["PYROGRAM_WORKDIR"]) / f"{env['PYROGRAM_SESSION_NAME']}.session"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("{}", encoding="utf-8")
    token.write_text("{}", encoding="utf-8")
    if create_pyrogram_session:
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_bytes(b"session")
