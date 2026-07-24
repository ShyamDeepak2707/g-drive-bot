from __future__ import annotations

import re
from pathlib import Path

from app.constants import FILENAME_MAX_LENGTH
from app.exceptions import ConfigurationError, RenameValidationError

BOT_TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


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


def validate_filename(filename: str) -> str:
    normalized = filename.strip()
    if normalized == "":
        raise RenameValidationError("Filename cannot be empty.")
    if normalized in {".", ".."}:
        raise RenameValidationError("Filename cannot be a relative path.")
    if INVALID_FILENAME_CHARS.search(normalized):
        raise RenameValidationError(
            'Filename cannot contain < > : " / \\ | ? * or control characters.'
        )
    if normalized.endswith(".") or normalized.endswith(" "):
        raise RenameValidationError("Filename cannot end with a space or dot.")
    if len(normalized) > FILENAME_MAX_LENGTH:
        raise RenameValidationError(f"Filename must be {FILENAME_MAX_LENGTH} characters or fewer.")
    stem = normalized.split(".", maxsplit=1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        raise RenameValidationError(f"'{normalized}' is reserved by Windows.")
    return normalized
