from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import cast

from telegram import Bot

from app.constants import DOWNLOAD_STATUS_CANCELLED
from app.database import DatabaseRepository, SQLiteDatabase
from app.download_queue import DownloadJob, DownloadJobStatus, DownloadQueue
from app.models import DownloadResult, FileMetadata, TelegramFileType


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.messages: list[str] = []

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        del chat_id, message_id
        self.edits.append(text)

    async def send_message(
        self, chat_id: int, text: str, reply_markup: object | None = None
    ) -> None:
        del chat_id, reply_markup
        self.messages.append(text)


class FakeDownloadManager:
    async def download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        progress_callback: object | None = None,
    ) -> DownloadResult:
        path = Path("downloads/example.txt")
        return DownloadResult(
            file_record_id=file_record_id,
            metadata=metadata,
            path=path,
            filename=path.name,
            size=metadata.size,
        )


def test_download_queue_processes_job(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = FileMetadata(
        telegram_file_id="file-id",
        message_id=10,
        chat_id=20,
        original_name="example.txt",
        mime_type="text/plain",
        size=5,
        extension=".txt",
        file_type=TelegramFileType.DOCUMENT,
        created_at="2026-07-24T00:00:00+00:00",
    )
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    bot = FakeBot()
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, FakeDownloadManager()),  # type: ignore[arg-type]
        bot=cast(Bot, bot),
        logger=logging.getLogger("test"),
        progress_interval_seconds=0,
    )

    async def run_queue() -> None:
        queue.start()
        await queue.enqueue(
            DownloadJob(
                file_record_id=file_record.id,
                metadata=metadata,
                chat_id=metadata.chat_id,
                status_message_id=99,
            )
        )
        await asyncio.wait_for(queue._queue.join(), timeout=2)  # noqa: SLF001
        await queue.stop()

    asyncio.run(run_queue())

    assert queue.status(file_record.id) == DownloadJobStatus.COMPLETED
    assert repository.get_download(file_record.id) is not None
    assert bot.messages


def test_download_queue_cancels_queued_job(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = FileMetadata(
        telegram_file_id="file-id",
        message_id=10,
        chat_id=20,
        original_name="example.txt",
        mime_type="text/plain",
        size=5,
        extension=".txt",
        file_type=TelegramFileType.DOCUMENT,
        created_at="2026-07-24T00:00:00+00:00",
    )
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, FakeDownloadManager()),  # type: ignore[arg-type]
        bot=cast(Bot, FakeBot()),
        logger=logging.getLogger("test"),
    )

    async def run_queue() -> None:
        job = DownloadJob(
            file_record_id=file_record.id,
            metadata=metadata,
            chat_id=metadata.chat_id,
            status_message_id=99,
        )
        await queue.enqueue(job)
        assert queue.cancel(file_record.id)
        queue.start()
        await asyncio.wait_for(queue._queue.join(), timeout=2)  # noqa: SLF001
        await queue.stop()

    asyncio.run(run_queue())

    download = repository.get_download(file_record.id)
    assert download is not None
    assert download.status == DOWNLOAD_STATUS_CANCELLED
