from __future__ import annotations


class AppError(Exception):
    """Base class for expected application errors."""


class ConfigurationError(AppError):
    """Raised when runtime configuration is invalid."""


class DatabaseError(AppError):
    """Raised when SQLite operations fail."""


class GoogleDriveError(AppError):
    """Raised when Google Drive authentication or service creation fails."""


class TelegramError(AppError):
    """Raised when Telegram bot initialization or runtime fails."""
