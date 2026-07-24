from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast

from app.exceptions import DownloadError
from app.models import DownloadResult, FileMetadata
from app.progress import ProgressSnapshot
from app.utils.filesystem import ensure_directory, unique_path

ProgressCallback = Callable[[ProgressSnapshot], Awaitable[None]]


class PyrogramDownloadClient(Protocol):
    async def download_media(
        self,
        message: str,
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
        session: PyrogramSession,
        download_dir: Path,
        temp_dir: Path,
    ) -> None:
        self._session = session
        self._download_dir = ensure_directory(download_dir)
        self._temp_dir = ensure_directory(temp_dir)

    async def download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadResult:
        final_path = self._unique_final_path(metadata)
        temp_path = self._temp_path(final_path)
        started_at = time.monotonic()

        async def progress(current: int, total: int) -> None:
            elapsed = max(0.001, time.monotonic() - started_at)
            speed = current / elapsed
            remaining = max(0, total - current) if total else 0
            eta = remaining / speed if speed > 0 and total else None
            if progress_callback is not None:
                await progress_callback(
                    ProgressSnapshot(
                        current=current,
                        total=total or metadata.size,
                        speed_bytes_per_second=speed,
                        eta_seconds=eta,
                    )
                )

        try:
            await self._session.start()
            client = cast(PyrogramDownloadClient, self._session.client)
            downloaded_path = await client.download_media(
                metadata.telegram_file_id,
                file_name=str(temp_path),
                progress=progress,
            )
            if downloaded_path is None:
                raise DownloadError("Pyrogram did not return a downloaded file path.")
            source_path = Path(downloaded_path)
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
        except asyncio.CancelledError:
            _cleanup_file(temp_path)
            raise
        except OSError as exc:
            _cleanup_file(temp_path)
            raise DownloadError(f"Download failed due to a filesystem error: {exc}") from exc
        except Exception as exc:
            _cleanup_file(temp_path)
            if isinstance(exc, DownloadError):
                raise
            raise DownloadError(f"Download failed: {exc}") from exc

    def _unique_final_path(self, metadata: FileMetadata) -> Path:
        original_name = (
            metadata.original_name or f"{metadata.file_type.value}-{metadata.message_id}"
        )
        suffix = metadata.extension or Path(original_name).suffix
        stem = Path(original_name).stem or metadata.file_type.value
        return unique_path(self._download_dir / f"{stem}{suffix}")

    def _temp_path(self, final_path: Path) -> Path:
        return self._temp_dir / f"{final_path.name}.{uuid.uuid4().hex}.part"


def _cleanup_file(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.exists():
            os.remove(path)
