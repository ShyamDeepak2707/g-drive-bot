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
FILE_STATUS_RECEIVED = "received"
FILE_STATUS_QUEUED = "queued"
FILE_STATUS_DOWNLOADING = "downloading"
FILE_STATUS_DOWNLOADED = "downloaded"
FILE_STATUS_AWAITING_RENAME = "awaiting_rename"
FILE_STATUS_READY_FOR_UPLOAD = "ready_for_upload"
FILE_STATUS_SKIPPED = "skipped"
FILE_STATUS_CANCELLED = "cancelled"
FILE_STATUS_UPLOADING = "uploading"
FILE_STATUS_COMPLETED = "completed"
FILE_STATUS_FAILED = "failed"

DOWNLOAD_STATUS_QUEUED = "queued"
DOWNLOAD_STATUS_RUNNING = "running"
DOWNLOAD_STATUS_COMPLETED = "completed"
DOWNLOAD_STATUS_FAILED = "failed"
DOWNLOAD_STATUS_CANCELLED = "cancelled"

FILE_TYPE_DOCUMENT = "document"
FILE_TYPE_VIDEO = "video"
FILE_TYPE_AUDIO = "audio"
FILE_TYPE_PHOTO = "photo"
FILE_TYPE_ANIMATION = "animation"
FILE_TYPE_VOICE = "voice"

RENAME_ACTION_KEEP = "keep"
RENAME_ACTION_RENAME = "rename"
RENAME_ACTION_SKIP = "skip"
RENAME_CALLBACK_PREFIX = "rename"

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
DOWNLOAD_QUEUE_RETRY_LIMIT = 2
DOWNLOAD_PROGRESS_UPDATE_SECONDS = 3.0
DOWNLOAD_WORKER_COUNT = 1
FILENAME_MAX_LENGTH = 180
