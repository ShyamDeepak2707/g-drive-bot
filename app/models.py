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
    forward_origin_chat_id: int | None
    forward_origin_message_id: int | None
    original_name: str | None
    mime_type: str | None
    size: int | None
    extension: str | None
    file_type: TelegramFileType
    created_at: str
    telegram_file_unique_id: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    file_record_id: int
    metadata: FileMetadata
    path: Path
    filename: str
    size: int | None


@dataclass(frozen=True)
class Folder:
    id: str
    name: str
    parent_id: str | None
    drive_id: str | None
    path: str
    is_shared: bool
    created_at: str | None
