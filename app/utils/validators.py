from __future__ import annotations

import re
from pathlib import Path

from app.exceptions import ConfigurationError

BOT_TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def require_non_empty(value: str | None, name: str) -> str:
    if value is None or value.strip() == "":
        raise ConfigurationError(f"{name} is required. Set {name} in your .env or environment.")
    return value.strip()


def parse_optional_int(value: str | None, name: str) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    parsed = tuple(item.strip() for item in value.split(",") if item.strip())
    if not parsed:
        raise ConfigurationError("GOOGLE_SCOPES must contain at least one OAuth scope.")
    return parsed


def validate_bot_token(token: str) -> str:
    if not BOT_TOKEN_PATTERN.match(token):
        raise ConfigurationError(
            "TELEGRAM_BOT_TOKEN has an invalid format. Expected '<bot_id>:<secret>'."
        )
    return token


def validate_log_level(level: str) -> str:
    normalized = level.upper()
    if normalized not in VALID_LOG_LEVELS:
        raise ConfigurationError(
            f"LOG_LEVEL must be one of: {', '.join(sorted(VALID_LOG_LEVELS))}."
        )
    return normalized


def validate_readable_file(path: Path, label: str) -> Path:
    if not path.exists():
        raise ConfigurationError(f"{label} was not found at '{path}'. Create or mount the file.")
    if not path.is_file():
        raise ConfigurationError(f"{label} must point to a file, got '{path}'.")
    return path
