from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from telegram.error import TelegramError as TelegramApiError

from app import constants
from app.exceptions import DownloadError
from app.models import DownloadResult, FileMetadata
from app.progress import ProgressSnapshot
from app.utils.filesystem import ensure_directory, unique_path

ProgressCallback = Callable[[ProgressSnapshot], Awaitable[None]]
HTTP_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
HTTP_DOWNLOAD_TIMEOUT_SECONDS = 30


class BotApiFile(Protocol):
    @property
    def file_id(self) -> str: ...

    @property
    def file_unique_id(self) -> str | None: ...

    @property
    def file_size(self) -> int | None: ...

    @property
    def file_path(self) -> str | None: ...

    async def download_to_drive(self, custom_path: str | Path) -> Path: ...


class BotApiDownloadClient(Protocol):
    async def get_file(self, file_id: str) -> BotApiFile: ...

    async def get_me(self) -> object: ...


class PyrogramDownloadClient(Protocol):
    def get_dialogs(self) -> AsyncIterator[object]: ...

    async def get_me(self) -> object: ...

    async def get_messages(self, chat_id: int | str, message_ids: int) -> object: ...

    async def download_media(
        self,
        message: object,
        file_name: str,
        progress: Callable[[int, int], object] | None = None,
    ) -> str | None: ...


class PyrogramSession(Protocol):
    @property
    def client(self) -> object: ...

    async def start(self) -> None: ...


class DownloadManager:
    def __init__(
        self,
        bot: BotApiDownloadClient,
        pyrogram_session: PyrogramSession | None,
        download_dir: Path,
        temp_dir: Path,
        logger: logging.Logger,
        pyrogram_bot_session: PyrogramSession | None = None,
    ) -> None:
        self._bot = bot
        self._pyrogram_session = pyrogram_session
        self._pyrogram_bot_session = pyrogram_bot_session
        self._download_dir = ensure_directory(download_dir)
        self._temp_dir = ensure_directory(temp_dir)
        self._logger = logger

    async def download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadResult:
        final_path = self._unique_final_path(metadata)
        temp_path = self._temp_path(final_path)
        pyrogram_temp_path = _pyrogram_temp_path(temp_path)
        started_at = time.monotonic()
        self._logger.info(
            "download source selected",
            extra={
                "event": "download_source_selected",
                "file_record_id": file_record_id,
                "detected_download_source": self._selected_download_source(metadata),
                "bot_api_fallback_reason": self._bot_api_fallback_reason(metadata),
                "metadata_chat_id": metadata.chat_id,
                "metadata_message_id": metadata.message_id,
                "forward_origin_chat_id": metadata.forward_origin_chat_id,
                "forward_origin_message_id": metadata.forward_origin_message_id,
                "original_name": metadata.original_name,
                "size": metadata.size,
                "telegram_file_unique_id_present": metadata.telegram_file_unique_id is not None,
            },
        )

        try:
            if metadata.source_url is not None:
                result = await self._download_via_direct_url(
                    file_record_id=file_record_id,
                    metadata=metadata,
                    final_path=final_path,
                    temp_path=temp_path,
                    started_at=started_at,
                    progress_callback=progress_callback,
                )
            elif metadata.forward_origin_chat_id is not None:
                if metadata.forward_origin_message_id is None:
                    raise DownloadError("Forward-origin chat id was stored without a message id.")
                result = await self._download_via_forward_origin(
                    file_record_id=file_record_id,
                    metadata=metadata,
                    final_path=final_path,
                    temp_path=temp_path,
                    started_at=started_at,
                    progress_callback=progress_callback,
                )
            elif self._pyrogram_bot_session is not None:
                try:
                    result = await self._download_via_pyrogram_bot_message(
                        file_record_id=file_record_id,
                        metadata=metadata,
                        final_path=final_path,
                        temp_path=temp_path,
                        started_at=started_at,
                        progress_callback=progress_callback,
                    )
                except DownloadError as exc:
                    self._logger.warning(
                        "pyrogram bot message download unavailable; falling back to Bot API",
                        extra={
                            "event": "pyrogram_bot_message_fallback",
                            "file_record_id": file_record_id,
                            "metadata_chat_id": metadata.chat_id,
                            "metadata_message_id": metadata.message_id,
                            "file_type": metadata.file_type.value,
                            "error": str(exc),
                        },
                    )
                    result = await self._download_via_bot_api_file_id(
                        file_record_id=file_record_id,
                        metadata=metadata,
                        final_path=final_path,
                        temp_path=temp_path,
                        started_at=started_at,
                        progress_callback=progress_callback,
                    )
            else:
                result = await self._download_via_bot_api_file_id(
                    file_record_id=file_record_id,
                    metadata=metadata,
                    final_path=final_path,
                    temp_path=temp_path,
                    started_at=started_at,
                    progress_callback=progress_callback,
                )
            return result
        except asyncio.CancelledError:
            _cleanup_file(temp_path, self._logger)
            _cleanup_file(pyrogram_temp_path, self._logger)
            self._logger.info(
                "download cancelled",
                extra={
                    "event": "download_cancelled",
                    "file_record_id": file_record_id,
                    "download_source": self._selected_download_source(metadata),
                    "source": "asyncio_task",
                },
            )
            raise
        except OSError as exc:
            _cleanup_file(temp_path, self._logger)
            _cleanup_file(pyrogram_temp_path, self._logger)
            raise DownloadError(f"Download failed due to a filesystem error: {exc}") from exc
        except TelegramApiError as exc:
            _cleanup_file(temp_path, self._logger)
            _cleanup_file(pyrogram_temp_path, self._logger)
            raise DownloadError(f"Telegram Bot API download failed: {exc}") from exc
        except Exception as exc:
            _cleanup_file(temp_path, self._logger)
            _cleanup_file(pyrogram_temp_path, self._logger)
            if isinstance(exc, DownloadError):
                raise
            raise DownloadError(f"Download failed: {exc}") from exc

    async def _download_via_forward_origin(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        final_path: Path,
        temp_path: Path,
        started_at: float,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        if self._pyrogram_session is None:
            raise DownloadError(
                "This forwarded channel message requires Pyrogram, but Pyrogram credentials "
                "are not configured."
            )
        origin_chat_id = metadata.forward_origin_chat_id
        origin_message_id = metadata.forward_origin_message_id
        if origin_chat_id is None or origin_message_id is None:
            raise DownloadError("Forward-origin metadata is incomplete.")

        await self._pyrogram_session.start()
        client = cast(PyrogramDownloadClient, self._pyrogram_session.client)
        await self._log_pyrogram_account(
            client=client,
            file_record_id=file_record_id,
            download_source="pyrogram_forward_origin",
        )
        self._logger.info(
            "download started",
            extra={
                "event": "download_started",
                "download_source": "pyrogram_forward_origin",
                "file_record_id": file_record_id,
                "forward_origin_chat_id": origin_chat_id,
                "forward_origin_message_id": origin_message_id,
                "bot_api_chat_id": metadata.chat_id,
                "bot_api_message_id": metadata.message_id,
                "file_type": metadata.file_type.value,
            },
        )
        message = await self._get_forward_origin_message(
            client=client,
            chat_id=origin_chat_id,
            message_id=origin_message_id,
            file_record_id=file_record_id,
        )
        detected_media_type = _detect_media_type(message)
        self._logger.info(
            "forward-origin source message fetched",
            extra={
                "event": "forward_origin_message_fetched",
                "file_record_id": file_record_id,
                "requested_chat_id": origin_chat_id,
                "requested_message_id": origin_message_id,
                "resolved_message_id": getattr(message, "id", None),
                "resolved_chat_id": getattr(getattr(message, "chat", None), "id", None),
                "resolved_chat_type": getattr(getattr(message, "chat", None), "type", None),
                "detected_media_type": detected_media_type,
                "document": bool(getattr(message, "document", None)),
                "video": bool(getattr(message, "video", None)),
                "audio": bool(getattr(message, "audio", None)),
                "photo": bool(getattr(message, "photo", None)),
                "animation": bool(getattr(message, "animation", None)),
                "voice": bool(getattr(message, "voice", None)),
                "text": getattr(message, "text", None),
                "caption": getattr(message, "caption", None),
            },
        )
        resolved_chat_id = getattr(getattr(message, "chat", None), "id", None)
        if resolved_chat_id != origin_chat_id:
            raise DownloadError(
                "Forward-origin Telegram message resolved to a different chat "
                f"({resolved_chat_id}) than the stored origin chat ({origin_chat_id})."
            )
        if getattr(message, metadata.file_type.value, None) is None:
            raise DownloadError(
                "Forward-origin Telegram message does not contain the expected "
                f"{metadata.file_type.value} media."
            )

        async def progress(current: int, total: int) -> None:
            if progress_callback is None:
                return
            await progress_callback(_progress_snapshot(current, total, metadata.size, started_at))

        media = getattr(message, metadata.file_type.value)
        expected_size = getattr(media, "file_size", None)
        _verify_remote_media_size(
            expected_size=metadata.size,
            resolved_size=expected_size,
            file_record_id=file_record_id,
            download_source="pyrogram_forward_origin",
            logger=self._logger,
        )
        self._logger.info(
            "download_begin",
            extra={
                "event": "download_begin",
                "download_source": "pyrogram_forward_origin",
                "file_record_id": file_record_id,
                "file_name": getattr(media, "file_name", None),
                "file_size": expected_size,
                "mime_type": getattr(media, "mime_type", None),
                "resolved_file_unique_id_present": _message_media_unique_id(message, metadata)
                is not None,
                "telegram_file_unique_id_present": metadata.telegram_file_unique_id is not None,
            },
        )
        download_started_at = time.perf_counter()
        downloaded_path = await client.download_media(
            message,
            file_name=str(temp_path),
            progress=progress,
        )
        elapsed = time.perf_counter() - download_started_at
        if downloaded_path is None:
            raise DownloadError("Pyrogram did not return a downloaded file path.")
        source_path = Path(downloaded_path)
        actual_size = source_path.stat().st_size if source_path.exists() else None
        self._logger.info(
            "download_performance",
            extra={
                "event": "download_performance",
                "download_source": "pyrogram_forward_origin",
                "file_record_id": file_record_id,
                "seconds": elapsed,
                "size": actual_size,
                "average_mbps": _average_mbps(actual_size, elapsed),
            },
        )
        self._logger.info(
            "download_complete",
            extra={
                "event": "download_complete",
                "download_source": "pyrogram_forward_origin",
                "file_record_id": file_record_id,
                "expected_size": expected_size,
                "actual_size": actual_size,
                "match": actual_size == expected_size,
            },
        )
        _verify_download_size(
            source_path=source_path,
            expected_size=expected_size if expected_size is not None else metadata.size,
            file_record_id=file_record_id,
            download_source="pyrogram_forward_origin",
            logger=self._logger,
        )
        result = self._finalize_download(file_record_id, metadata, source_path, final_path)
        self._logger.info(
            "download completed",
            extra={
                "event": "download_completed",
                "download_source": "pyrogram_forward_origin",
                "file_record_id": file_record_id,
                "path": str(final_path),
                "size": result.size,
            },
        )
        return result

    async def _download_via_pyrogram_bot_message(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        final_path: Path,
        temp_path: Path,
        started_at: float,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        if self._pyrogram_bot_session is None:
            raise DownloadError("Pyrogram bot session is not configured.")

        await self._pyrogram_bot_session.start()
        client = cast(PyrogramDownloadClient, self._pyrogram_bot_session.client)
        await self._log_pyrogram_account(
            client=client,
            file_record_id=file_record_id,
            download_source="pyrogram_bot_message",
        )
        self._logger.info(
            "download started",
            extra={
                "event": "download_started",
                "download_source": "pyrogram_bot_message",
                "file_record_id": file_record_id,
                "bot_api_chat_id": metadata.chat_id,
                "bot_api_message_id": metadata.message_id,
                "file_type": metadata.file_type.value,
            },
        )
        try:
            message = await client.get_messages(
                chat_id=metadata.chat_id,
                message_ids=metadata.message_id,
            )
        except Exception as exc:
            raise DownloadError("Pyrogram bot could not fetch the incoming media message.") from exc

        detected_media_type = _detect_media_type(message)
        self._logger.info(
            "pyrogram bot source message fetched",
            extra={
                "event": "pyrogram_bot_message_fetched",
                "download_source": "pyrogram_bot_message",
                "file_record_id": file_record_id,
                "requested_chat_id": metadata.chat_id,
                "requested_message_id": metadata.message_id,
                "resolved_message_id": getattr(message, "id", None),
                "resolved_chat_id": getattr(getattr(message, "chat", None), "id", None),
                "resolved_chat_type": getattr(getattr(message, "chat", None), "type", None),
                "detected_media_type": detected_media_type,
                "document": bool(getattr(message, "document", None)),
                "video": bool(getattr(message, "video", None)),
                "audio": bool(getattr(message, "audio", None)),
                "photo": bool(getattr(message, "photo", None)),
                "animation": bool(getattr(message, "animation", None)),
                "voice": bool(getattr(message, "voice", None)),
                "text": getattr(message, "text", None),
                "caption": getattr(message, "caption", None),
            },
        )
        if getattr(message, metadata.file_type.value, None) is None:
            raise DownloadError(
                "Pyrogram bot message does not contain the expected "
                f"{metadata.file_type.value} media."
            )

        async def progress(current: int, total: int) -> None:
            if progress_callback is None:
                return
            await progress_callback(_progress_snapshot(current, total, metadata.size, started_at))

        media = getattr(message, metadata.file_type.value)
        expected_size = getattr(media, "file_size", None)
        _verify_remote_media_size(
            expected_size=metadata.size,
            resolved_size=expected_size,
            file_record_id=file_record_id,
            download_source="pyrogram_bot_message",
            logger=self._logger,
        )
        self._logger.info(
            "download_begin",
            extra={
                "event": "download_begin",
                "download_source": "pyrogram_bot_message",
                "file_record_id": file_record_id,
                "file_name": getattr(media, "file_name", None),
                "file_size": expected_size,
                "mime_type": getattr(media, "mime_type", None),
                "resolved_file_unique_id_present": _message_media_unique_id(message, metadata)
                is not None,
                "telegram_file_unique_id_present": metadata.telegram_file_unique_id is not None,
            },
        )
        download_started_at = time.perf_counter()
        downloaded_path = await client.download_media(
            message,
            file_name=str(temp_path),
            progress=progress,
        )
        elapsed = time.perf_counter() - download_started_at
        if downloaded_path is None:
            raise DownloadError("Pyrogram did not return a downloaded file path.")
        source_path = Path(downloaded_path)
        actual_size = source_path.stat().st_size if source_path.exists() else None
        self._logger.info(
            "download_performance",
            extra={
                "event": "download_performance",
                "download_source": "pyrogram_bot_message",
                "file_record_id": file_record_id,
                "seconds": elapsed,
                "size": actual_size,
                "average_mbps": _average_mbps(actual_size, elapsed),
            },
        )
        self._logger.info(
            "download_complete",
            extra={
                "event": "download_complete",
                "download_source": "pyrogram_bot_message",
                "file_record_id": file_record_id,
                "expected_size": expected_size,
                "actual_size": actual_size,
                "match": actual_size == expected_size,
            },
        )
        _verify_download_size(
            source_path=source_path,
            expected_size=expected_size if expected_size is not None else metadata.size,
            file_record_id=file_record_id,
            download_source="pyrogram_bot_message",
            logger=self._logger,
        )
        result = self._finalize_download(file_record_id, metadata, source_path, final_path)
        self._logger.info(
            "download completed",
            extra={
                "event": "download_completed",
                "download_source": "pyrogram_bot_message",
                "file_record_id": file_record_id,
                "path": str(final_path),
                "size": result.size,
            },
        )
        return result

    async def _log_pyrogram_account(
        self,
        client: PyrogramDownloadClient,
        file_record_id: int,
        download_source: str,
    ) -> None:
        try:
            account = await client.get_me()
        except Exception as exc:
            self._logger.warning(
                "could not fetch authenticated Pyrogram account",
                extra={
                    "event": "pyrogram_account_diagnostic_failed",
                    "download_source": download_source,
                    "file_record_id": file_record_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return
        self._logger.info(
            "authenticated Pyrogram account",
            extra={
                "event": "pyrogram_account_diagnostic",
                "download_source": download_source,
                "file_record_id": file_record_id,
                "pyrogram_user_id": getattr(account, "id", None),
                "pyrogram_username": getattr(account, "username", None),
                "pyrogram_phone_number": getattr(account, "phone_number", None),
                "pyrogram_first_name": getattr(account, "first_name", None),
            },
        )

    async def _get_forward_origin_message(
        self,
        client: PyrogramDownloadClient,
        chat_id: int,
        message_id: int,
        file_record_id: int,
    ) -> object:
        try:
            return await client.get_messages(
                chat_id=chat_id,
                message_ids=message_id,
            )
        except Exception as exc:
            if not _is_peer_id_invalid(exc):
                raise
            self._logger.info(
                "warming forward-origin peer cache after message lookup failed",
                extra={
                    "event": "forward_origin_peer_cache_warmup",
                    "file_record_id": file_record_id,
                    "requested_chat_id": chat_id,
                    "requested_message_id": message_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

        async for dialog in client.get_dialogs():
            chat = getattr(dialog, "chat", None)
            if getattr(chat, "id", None) == chat_id:
                self._logger.info(
                    "forward-origin peer found in dialogs",
                    extra={
                        "event": "forward_origin_peer_found",
                        "file_record_id": file_record_id,
                        "requested_chat_id": chat_id,
                        "resolved_chat_id": getattr(chat, "id", None),
                        "resolved_chat_type": getattr(chat, "type", None),
                        "title": getattr(chat, "title", None),
                        "username": getattr(chat, "username", None),
                    },
                )
                return await client.get_messages(
                    chat_id=chat_id,
                    message_ids=message_id,
                )
        raise DownloadError(
            "Pyrogram cannot access the forwarded channel. Join or open the channel "
            "with the configured Telegram user session, then send the file again."
        )

    async def _download_via_bot_api_file_id(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        final_path: Path,
        temp_path: Path,
        started_at: float,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        self._logger.info(
            "download started",
            extra={
                "event": "download_started",
                "download_source": "bot_api_file_id",
                "file_record_id": file_record_id,
                "telegram_file_id": metadata.telegram_file_id,
                "bot_api_chat_id": metadata.chat_id,
                "bot_api_message_id": metadata.message_id,
                "file_type": metadata.file_type.value,
            },
        )
        telegram_file = await self._bot.get_file(metadata.telegram_file_id)
        self._logger.info(
            "bot api file resolved",
            extra={
                "event": "bot_api_file_resolved",
                "file_record_id": file_record_id,
                "telegram_file_id": metadata.telegram_file_id,
                "resolved_file_id": getattr(telegram_file, "file_id", None),
                "file_unique_id": getattr(telegram_file, "file_unique_id", None),
                "file_size": getattr(telegram_file, "file_size", None),
                "file_path_present": getattr(telegram_file, "file_path", None) is not None,
            },
        )

        downloaded_path = await telegram_file.download_to_drive(custom_path=temp_path)
        source_path = Path(downloaded_path)
        expected_size = getattr(telegram_file, "file_size", None)
        _verify_download_size(
            source_path=source_path,
            expected_size=expected_size if expected_size is not None else metadata.size,
            file_record_id=file_record_id,
            download_source="bot_api_file_id",
            logger=self._logger,
        )
        result = self._finalize_download(file_record_id, metadata, source_path, final_path)
        if progress_callback is not None:
            await progress_callback(
                ProgressSnapshot(
                    current=result.size or metadata.size or 0,
                    total=result.size or metadata.size or 0,
                    speed_bytes_per_second=_speed(result.size, started_at),
                    eta_seconds=0,
                )
            )
        self._logger.info(
            "download completed",
            extra={
                "event": "download_completed",
                "download_source": "bot_api_file_id",
                "file_record_id": file_record_id,
                "path": str(final_path),
                "size": result.size,
            },
        )
        return result

    async def _download_via_direct_url(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        final_path: Path,
        temp_path: Path,
        started_at: float,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        if metadata.source_url is None:
            raise DownloadError("Direct download URL is missing.")
        self._logger.info(
            "download started",
            extra={
                "event": "download_started",
                "download_source": "direct_url",
                "file_record_id": file_record_id,
                "url_host": urlparse(metadata.source_url).netloc,
                "original_name": metadata.original_name,
                "size": metadata.size,
            },
        )
        loop = asyncio.get_running_loop()
        try:
            await asyncio.to_thread(
                _stream_direct_url_to_temp_file,
                url=metadata.source_url,
                temp_path=temp_path,
                expected_size=metadata.size,
                started_at=started_at,
                progress_callback=progress_callback,
                loop=loop,
            )
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            _cleanup_file(temp_path, self._logger)
            raise DownloadError(f"Direct link download failed: {exc}") from exc

        expected_size = metadata.size or temp_path.stat().st_size
        _verify_download_size(
            source_path=temp_path,
            expected_size=expected_size,
            file_record_id=file_record_id,
            download_source="direct_url",
            logger=self._logger,
        )
        result = self._finalize_download(file_record_id, metadata, temp_path, final_path)
        if progress_callback is not None:
            await progress_callback(
                ProgressSnapshot(
                    current=result.size or expected_size,
                    total=result.size or expected_size,
                    speed_bytes_per_second=_speed(result.size, started_at),
                    eta_seconds=0,
                )
            )
        self._logger.info(
            "download completed",
            extra={
                "event": "download_completed",
                "download_source": "direct_url",
                "file_record_id": file_record_id,
                "path": str(final_path),
                "size": result.size,
            },
        )
        return result

    def _finalize_download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        source_path: Path,
        final_path: Path,
    ) -> DownloadResult:
        if not source_path.exists():
            raise DownloadError(f"Downloaded file was not found at '{source_path}'.")
        ensure_directory(final_path.parent)
        shutil.move(str(source_path), final_path)
        return DownloadResult(
            file_record_id=file_record_id,
            metadata=metadata,
            path=final_path,
            filename=final_path.name,
            size=final_path.stat().st_size if final_path.exists() else metadata.size,
        )

    def _unique_final_path(self, metadata: FileMetadata) -> Path:
        original_name = (
            metadata.original_name or f"{metadata.file_type.value}-{metadata.message_id}"
        )
        suffix = metadata.extension or Path(original_name).suffix
        stem = Path(original_name).stem or metadata.file_type.value
        return unique_path(self._download_dir / f"{stem}{suffix}")

    def _temp_path(self, final_path: Path) -> Path:
        return self._temp_dir / f"{final_path.name}.{uuid.uuid4().hex}.part"

    def _selected_download_source(self, metadata: FileMetadata) -> str:
        if metadata.source_url is not None:
            return "direct_url"
        if (
            metadata.forward_origin_chat_id is not None
            and metadata.forward_origin_message_id is not None
        ):
            return "pyrogram_forward_origin"
        if self._pyrogram_bot_session is not None:
            return "pyrogram_bot_message"
        return "bot_api_file_id"

    def _bot_api_fallback_reason(self, metadata: FileMetadata) -> str | None:
        if self._selected_download_source(metadata) != "bot_api_file_id":
            return None
        if metadata.forward_origin_chat_id is None and metadata.forward_origin_message_id is None:
            return "pyrogram_bot_session_not_configured"
        if metadata.forward_origin_chat_id is None:
            return "metadata_missing_forward_origin_chat_id"
        return "metadata_missing_forward_origin_message_id"


def _cleanup_file(path: Path, logger: logging.Logger) -> None:
    with contextlib.suppress(OSError):
        if path.exists():
            os.remove(path)
            logger.info(
                "cleaned up temporary download file",
                extra={"event": "download_cleanup", "path": str(path)},
            )


def _stream_direct_url_to_temp_file(
    *,
    url: str,
    temp_path: Path,
    expected_size: int | None,
    started_at: float,
    progress_callback: ProgressCallback | None,
    loop: asyncio.AbstractEventLoop,
) -> None:
    request = Request(url, headers={"User-Agent": f"{constants.APP_NAME}/1.0"})
    with urlopen(request, timeout=HTTP_DOWNLOAD_TIMEOUT_SECONDS) as response:
        total = _response_content_length(response.headers.get("Content-Length")) or expected_size
        if total is not None and total > constants.MAX_UPLOAD_SIZE_BYTES:
            raise DownloadError(
                "Direct link is too large. "
                f"Maximum supported size is {constants.MAX_UPLOAD_SIZE_BYTES} bytes."
            )
        ensure_directory(temp_path.parent)
        current = 0
        with temp_path.open("wb") as output:
            while True:
                chunk = response.read(HTTP_DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                current += len(chunk)
                if current > constants.MAX_UPLOAD_SIZE_BYTES:
                    raise DownloadError(
                        "Direct link is too large. "
                        f"Maximum supported size is {constants.MAX_UPLOAD_SIZE_BYTES} bytes."
                    )
                output.write(chunk)
                if progress_callback is not None:
                    _schedule_progress_callback(
                        progress_callback,
                        _progress_snapshot(current, total, expected_size, started_at),
                        loop,
                    )


def _schedule_progress_callback(
    progress_callback: ProgressCallback,
    snapshot: ProgressSnapshot,
    loop: asyncio.AbstractEventLoop,
) -> None:
    def schedule() -> None:
        asyncio.ensure_future(progress_callback(snapshot))

    loop.call_soon_threadsafe(schedule)


def _response_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _pyrogram_temp_path(path: Path) -> Path:
    return Path(f"{path}.temp")


def _detect_media_type(message: object) -> str | None:
    for media_type in ("document", "video", "audio", "photo", "animation", "voice"):
        if getattr(message, media_type, None) is not None:
            return media_type
    return None


def _message_media_unique_id(message: object, metadata: FileMetadata) -> str | None:
    media = getattr(message, metadata.file_type.value, None)
    if media is None:
        return None
    file_unique_id = getattr(media, "file_unique_id", None)
    return file_unique_id if isinstance(file_unique_id, str) else None


def _is_peer_id_invalid(exc: Exception) -> bool:
    return type(exc).__name__ == "PeerIdInvalid" or "Peer id invalid" in str(exc)


def _progress_snapshot(
    current: int,
    total: int | None,
    metadata_size: int | None,
    started_at: float,
) -> ProgressSnapshot:
    elapsed = max(0.001, time.monotonic() - started_at)
    speed = current / elapsed
    resolved_total = total or metadata_size or current
    remaining = max(0, resolved_total - current) if resolved_total else 0
    eta = remaining / speed if speed > 0 and resolved_total else None
    return ProgressSnapshot(
        current=current,
        total=resolved_total,
        speed_bytes_per_second=speed,
        eta_seconds=eta,
    )


def _speed(size: int | None, started_at: float) -> float | None:
    if size is None:
        return None
    elapsed = max(0.001, time.monotonic() - started_at)
    return size / elapsed


def _average_mbps(size: int | None, elapsed_seconds: float) -> float | None:
    if size is None:
        return None
    elapsed = max(0.001, elapsed_seconds)
    return round(size / elapsed / 1024 / 1024, 2)


def _verify_download_size(
    source_path: Path,
    expected_size: int | None,
    file_record_id: int,
    download_source: str,
    logger: logging.Logger,
) -> None:
    if expected_size is None:
        return
    actual_size = source_path.stat().st_size if source_path.exists() else None
    if actual_size == expected_size:
        return
    logger.warning(
        "downloaded file size mismatch",
        extra={
            "event": "download_size_mismatch",
            "download_source": download_source,
            "file_record_id": file_record_id,
            "expected_size": expected_size,
            "actual_size": actual_size,
        },
    )
    raise DownloadError(
        "Downloaded file size mismatch: "
        f"expected {expected_size} bytes, got {actual_size} bytes."
    )


def _verify_remote_media_size(
    expected_size: int | None,
    resolved_size: int | None,
    file_record_id: int,
    download_source: str,
    logger: logging.Logger,
) -> None:
    if expected_size is None or resolved_size is None or expected_size == resolved_size:
        return
    logger.warning(
        "remote Telegram media size mismatch",
        extra={
            "event": "remote_media_size_mismatch",
            "download_source": download_source,
            "file_record_id": file_record_id,
            "expected_size": expected_size,
            "resolved_size": resolved_size,
        },
    )
    raise DownloadError(
        "Remote Telegram media size mismatch: "
        f"expected {expected_size} bytes, got {resolved_size} bytes."
    )
