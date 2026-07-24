from __future__ import annotations

APP_NAME = "telegram-drive-manager"

DATA_DIR = "data"
DOWNLOADS_DIR = "downloads"
LOGS_DIR = "logs"
SESSIONS_DIR = "sessions"
TEMP_DIR = "tmp"

DEFAULT_SQLITE_DB_PATH = f"{DATA_DIR}/app.sqlite3"
DEFAULT_GOOGLE_CREDENTIALS_FILE = "credentials.json"
DEFAULT_GOOGLE_TOKEN_FILE = f"{DATA_DIR}/token.json"
DEFAULT_GOOGLE_SCOPES = ("https://www.googleapis.com/auth/drive.file",)
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FILE = f"{LOGS_DIR}/app.log"

START_COMMAND = "start"

FILE_STATUS_PENDING = "pending"
FILE_STATUS_UPLOADING = "uploading"
FILE_STATUS_COMPLETED = "completed"
FILE_STATUS_FAILED = "failed"

SUPPORTED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "text/plain",
        "video/mp4",
    }
)

MAX_UPLOAD_SIZE_BYTES = 2 * 1024 * 1024 * 1024
TASK_SHUTDOWN_TIMEOUT_SECONDS = 10.0
