from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app import constants
from app.exceptions import ConfigurationError
from app.utils.filesystem import ensure_directory, ensure_parent_directory
from app.utils.validators import (
    parse_bool,
    parse_csv,
    parse_optional_int,
    require_non_empty,
    validate_bot_token,
    validate_log_level,
    validate_readable_file,
)


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    log_file: Path
    telegram_bot_token: str
    pyrogram_api_id: int | None
    pyrogram_api_hash: str | None
    pyrogram_session_name: str
    pyrogram_workdir: Path
    google_credentials_file: Path
    google_token_file: Path
    google_scopes: tuple[str, ...]
    google_auto_auth: bool
    sqlite_db_path: Path
    downloads_dir: Path
    temp_dir: Path


def load_settings(dotenv_path: Path | None = None) -> Settings:
    load_dotenv(dotenv_path=dotenv_path)

    telegram_bot_token = validate_bot_token(
        require_non_empty(os.getenv("TELEGRAM_BOT_TOKEN"), "TELEGRAM_BOT_TOKEN")
    )
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    log_level = validate_log_level(os.getenv("LOG_LEVEL", constants.DEFAULT_LOG_LEVEL))
    pyrogram_api_id = parse_optional_int(os.getenv("PYROGRAM_API_ID"), "PYROGRAM_API_ID")
    google_scopes = parse_csv(os.getenv("GOOGLE_SCOPES"), constants.DEFAULT_GOOGLE_SCOPES)

    settings = Settings(
        app_env=app_env,
        log_level=log_level,
        log_file=Path(os.getenv("LOG_FILE", constants.DEFAULT_LOG_FILE)),
        telegram_bot_token=telegram_bot_token,
        pyrogram_api_id=pyrogram_api_id,
        pyrogram_api_hash=_none_if_blank(os.getenv("PYROGRAM_API_HASH")),
        pyrogram_session_name=os.getenv("PYROGRAM_SESSION_NAME", "g_drive_bot"),
        pyrogram_workdir=Path(os.getenv("PYROGRAM_WORKDIR", constants.SESSIONS_DIR)),
        google_credentials_file=Path(
            os.getenv("GOOGLE_CREDENTIALS_FILE", constants.DEFAULT_GOOGLE_CREDENTIALS_FILE)
        ),
        google_token_file=Path(os.getenv("GOOGLE_TOKEN_FILE", constants.DEFAULT_GOOGLE_TOKEN_FILE)),
        google_scopes=google_scopes,
        google_auto_auth=parse_bool(os.getenv("GOOGLE_AUTO_AUTH"), default=False),
        sqlite_db_path=Path(os.getenv("SQLITE_DB_PATH", constants.DEFAULT_SQLITE_DB_PATH)),
        downloads_dir=Path(os.getenv("DOWNLOADS_DIR", constants.DOWNLOADS_DIR)),
        temp_dir=Path(os.getenv("TEMP_DIR", constants.TEMP_DIR)),
    )
    validate_settings(settings)
    return settings


def validate_settings(settings: Settings) -> None:
    ensure_parent_directory(settings.sqlite_db_path)
    ensure_parent_directory(settings.google_token_file)
    ensure_parent_directory(settings.log_file)
    ensure_directory(settings.pyrogram_workdir)
    ensure_directory(settings.downloads_dir)
    ensure_directory(settings.temp_dir)

    if bool(settings.pyrogram_api_id) != bool(settings.pyrogram_api_hash):
        raise ConfigurationError(
            "PYROGRAM_API_ID and PYROGRAM_API_HASH must be configured together."
        )

    if settings.google_auto_auth or settings.app_env == "production":
        validate_readable_file(settings.google_credentials_file, "Google credentials file")


def _none_if_blank(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value.strip()
