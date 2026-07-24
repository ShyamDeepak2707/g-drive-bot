from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class TelegramFileType(StrEnum):
    DOCUMENT = "document"
    VIDEO = "video"
    AUDIO = "audio"
    PHOTO = "photo"
    ANIMATION = "animation"
    VOICE = "voice"


@dataclass(frozen=True)
class FileMetadata:
    telegram_file_id: str
    message_id: int
    chat_id: int
    original_name: str | None
    mime_type: str | None
    size: int | None
    extension: str | None
    file_type: TelegramFileType
    created_at: str


@dataclass(frozen=True)
class DownloadResult:
    file_record_id: int
    metadata: FileMetadata
    path: Path
    filename: str
    size: int | None
