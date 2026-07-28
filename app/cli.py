from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from app import constants
from app.health import HealthService, HealthSnapshot
from app.startup_recovery import StartupRecoverySummary

CRITICAL_SUBSYSTEMS = (
    ("Application", "application_running"),
    ("Telegram Bot", "telegram_bot_connected"),
    ("Pyrogram", "pyrogram_connected"),
    ("Google Drive", "google_drive_authenticated"),
    ("Database", "database_connected"),
)


@dataclass(frozen=True)
class HealthCheckResult:
    snapshot: HealthSnapshot
    application_running: bool
    failures: tuple[str, ...]

    @property
    def is_healthy(self) -> bool:
        return not self.failures


class _ReadOnlyDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    def connect(self) -> None:
        if self._connection is not None:
            return
        if not self.path.exists():
            return
        uri = f"file:{self.path.as_posix()}?mode=ro"
        try:
            self._connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            self._connection = None

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if self._connection is None:
            raise sqlite3.OperationalError("SQLite database is not connected.")
        return self._connection.execute(sql, parameters)

    def close(self) -> None:
        if self._connection is None:
            return
        self._connection.close()
        self._connection = None


class _ReadOnlyRepository:
    def __init__(self, database: _ReadOnlyDatabase) -> None:
        self._database = database

    def count_pending_uploads(self) -> int:
        row = self._database.execute(
            """
            SELECT COUNT(*) AS count
            FROM files
            WHERE status = ?
              AND google_drive_file_id IS NULL
              AND (upload_retry_after IS NULL OR upload_retry_after <= CURRENT_TIMESTAMP)
            """,
            (constants.FILE_STATUS_READY_FOR_UPLOAD,),
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])


@dataclass(frozen=True)
class _ApplicationProbe:
    running: bool
    bot: object | None


@dataclass(frozen=True)
class _PyrogramProbe:
    running: bool

    def is_running(self) -> bool:
        return self.running


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Run a lightweight operational health check.")
    args = parser.parse_args(argv)

    if args.command == "health":
        return health_command()
    parser.error(f"Unsupported command: {args.command}")
    return 2


def health_command(
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    result = run_health_check(env=env or os.environ)
    if result.is_healthy:
        print(
            "healthy: all critical subsystems are available "
            f"(uptime {int(result.snapshot.uptime_seconds)}s)",
            file=output,
        )
        return 0

    print("unhealthy:", file=output)
    for failure in result.failures:
        print(f"- {failure}", file=output)
    return 1


def run_health_check(env: Mapping[str, str]) -> HealthCheckResult:
    now = datetime.now(tz=UTC)
    startup_time = _container_startup_time(now)
    database = _ReadOnlyDatabase(
        _path_from_env(env, "SQLITE_DB_PATH", constants.DEFAULT_SQLITE_DB_PATH)
    )
    database.connect()
    try:
        application_running = _application_process_running()
        telegram_token_present = bool(env.get("TELEGRAM_BOT_TOKEN", "").strip())
        pyrogram_session_present = _pyrogram_session_exists(env)
        google_drive_files_present = _google_drive_files_exist(env)
        service = HealthService(
            startup_time=startup_time,
            database=database,
            repository=_ReadOnlyRepository(database),
            telegram_application=_ApplicationProbe(
                running=application_running and telegram_token_present,
                bot=object() if telegram_token_present else None,
            ),  # type: ignore[arg-type]
            pyrogram_client=_PyrogramProbe(running=pyrogram_session_present),
            drive_service=object() if google_drive_files_present else None,  # type: ignore[arg-type]
            download_queue=None,
            upload_worker=None,
            startup_recovery_summary=StartupRecoverySummary(0, 0, 0),
            clock=lambda: now,
        )
        snapshot = service.snapshot()
    finally:
        database.close()

    failures = _critical_failures(snapshot, application_running)
    return HealthCheckResult(
        snapshot=snapshot,
        application_running=application_running,
        failures=failures,
    )


def _critical_failures(snapshot: HealthSnapshot, application_running: bool) -> tuple[str, ...]:
    values = {
        "application_running": application_running,
        "telegram_bot_connected": snapshot.telegram_bot_connected,
        "pyrogram_connected": snapshot.pyrogram_connected,
        "google_drive_authenticated": snapshot.google_drive_authenticated,
        "database_connected": snapshot.database_connected,
    }
    return tuple(label for label, key in CRITICAL_SUBSYSTEMS if not values[key])


def _application_process_running() -> bool:
    cmdline = Path("/proc/1/cmdline")
    if not cmdline.exists():
        return True
    try:
        value = cmdline.read_text(encoding="utf-8", errors="ignore").replace("\x00", " ")
    except OSError:
        return False
    return "main.py" in value or "app.cli" not in value


def _container_startup_time(now: datetime) -> datetime:
    stat_path = Path("/proc/1/stat")
    uptime_path = Path("/proc/uptime")
    try:
        start_ticks = int(stat_path.read_text(encoding="utf-8").split()[21])
        uptime_seconds = float(uptime_path.read_text(encoding="utf-8").split()[0])
        sysconf = getattr(os, "sysconf", None)
        sysconf_names = getattr(os, "sysconf_names", {})
        if sysconf is None or "SC_CLK_TCK" not in sysconf_names:
            return now
        ticks_per_second = sysconf(sysconf_names["SC_CLK_TCK"])
    except (OSError, IndexError, ValueError):
        return now
    process_uptime_seconds = max(0.0, uptime_seconds - (start_ticks / ticks_per_second))
    return now - timedelta(seconds=process_uptime_seconds)


def _pyrogram_session_exists(env: Mapping[str, str]) -> bool:
    workdir = _path_from_env(env, "PYROGRAM_WORKDIR", constants.SESSIONS_DIR)
    session_name = env.get("PYROGRAM_SESSION_NAME", "g_drive_bot").strip() or "g_drive_bot"
    return (workdir / f"{session_name}.session").exists()


def _google_drive_files_exist(env: Mapping[str, str]) -> bool:
    credentials = _path_from_env(
        env,
        "GOOGLE_CREDENTIALS_FILE",
        constants.DEFAULT_GOOGLE_CREDENTIALS_FILE,
    )
    token = _path_from_env(env, "GOOGLE_TOKEN_FILE", constants.DEFAULT_GOOGLE_TOKEN_FILE)
    return credentials.exists() and token.exists()


def _path_from_env(env: Mapping[str, str], name: str, default: str) -> Path:
    return Path(env.get(name, default)).expanduser()


if __name__ == "__main__":
    raise SystemExit(main())
