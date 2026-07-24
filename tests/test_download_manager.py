from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.download_manager import DownloadManager
from app.models import FileMetadata, TelegramFileType


class FakePyrogramClient:
    async def download_media(
        self,
        message: str,
        file_name: str,
        progress: Callable[[int, int], object] | None = None,
    ) -> str:
        del message
        path = Path(file_name)
        path.write_bytes(b"hello")
        if progress is not None:
            result = progress(5, 5)
            if hasattr(result, "__await__"):
                await result
        return str(path)


class FakeSession:
    def __init__(self) -> None:
        self.client = FakePyrogramClient()
        self.started = False

    async def start(self) -> None:
        self.started = True


async def _download(tmp_path: Path) -> tuple[FakeSession, Path]:
    session = FakeSession()
    manager = DownloadManager(
        session=session, download_dir=tmp_path / "downloads", temp_dir=tmp_path / "tmp"
    )
    metadata = FileMetadata(
        telegram_file_id="file-id",
        message_id=1,
        chat_id=2,
        original_name="example.txt",
        mime_type="text/plain",
        size=5,
        extension=".txt",
        file_type=TelegramFileType.DOCUMENT,
        created_at="2026-07-24T00:00:00+00:00",
    )
    result = await manager.download(file_record_id=1, metadata=metadata)
    return session, result.path


def test_download_manager_downloads_to_unique_path(tmp_path: Path) -> None:
    import asyncio

    session, path = asyncio.run(_download(tmp_path))

    assert session.started
    assert path.exists()
    assert path.name == "example.txt"
    assert path.read_bytes() == b"hello"
