from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from telegram import Bot

from app.constants import DOWNLOAD_STATUS_CANCELLED, DOWNLOAD_STATUS_QUEUED, FILE_STATUS_QUEUED
from app.database import DatabaseRepository, DownloadRecord, SQLiteDatabase
from app.download_queue import DownloadJob, DownloadJobStatus, DownloadQueue, _retry_delay
from app.exceptions import DownloadError
from app.models import DownloadResult, FileMetadata, TelegramFileType
from app.progress import ProgressSnapshot

ProgressCallback = Callable[[ProgressSnapshot], Awaitable[None]]


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.edit_kwargs: list[dict[str, object]] = []
        self.messages: list[str] = []

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        **kwargs: object,
    ) -> None:
        del chat_id, message_id
        self.edits.append(text)
        self.edit_kwargs.append(kwargs)

    async def send_message(
        self, chat_id: int, text: str, reply_markup: object | None = None
    ) -> None:
        del chat_id, reply_markup
        self.messages.append(text)


class CountingRepository(DatabaseRepository):
    def __init__(self, database: SQLiteDatabase) -> None:
        super().__init__(database)
        self.progress_update_count = 0

    def update_download_progress(
        self,
        file_id: int,
        bytes_downloaded: int,
        total_bytes: int | None,
    ) -> DownloadRecord | None:
        self.progress_update_count += 1
        return super().update_download_progress(file_id, bytes_downloaded, total_bytes)


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


class FailingOnceDownloadManager:
    def __init__(self) -> None:
        self.calls = 0

    async def download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        progress_callback: object | None = None,
    ) -> DownloadResult:
        self.calls += 1
        if self.calls == 1:
            raise DownloadError("temporary failure")
        path = Path("downloads/retried.txt")
        return DownloadResult(
            file_record_id=file_record_id,
            metadata=metadata,
            path=path,
            filename=path.name,
            size=metadata.size,
        )


class CancellableDownloadManager:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        progress_callback: object | None = None,
    ) -> DownloadResult:
        self.started.set()
        callback = cast(ProgressCallback, progress_callback)
        try:
            while True:
                await asyncio.sleep(0.01)
                await callback(ProgressSnapshot(current=1, total=metadata.size))
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FirstCancellableThenSuccessDownloadManager:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        progress_callback: object | None = None,
    ) -> DownloadResult:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            callback = cast(ProgressCallback, progress_callback)
            try:
                while True:
                    await asyncio.sleep(0.01)
                    await callback(ProgressSnapshot(current=1, total=metadata.size))
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        path = Path(f"downloads/{file_record_id}.txt")
        return DownloadResult(
            file_record_id=file_record_id,
            metadata=metadata,
            path=path,
            filename=path.name,
            size=metadata.size,
        )


class ProgressBurstDownloadManager:
    async def download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        progress_callback: object | None = None,
    ) -> DownloadResult:
        if progress_callback is not None:
            callback = cast(ProgressCallback, progress_callback)
            for current in range(1, 51):
                await callback(
                    ProgressSnapshot(
                        current=current,
                        total=50,
                        speed_bytes_per_second=1000,
                        eta_seconds=0,
                    )
                )
            await asyncio.sleep(0.06)
        path = Path("downloads/progress.txt")
        return DownloadResult(
            file_record_id=file_record_id,
            metadata=metadata,
            path=path,
            filename=path.name,
            size=metadata.size,
        )


class NearlyCompleteDownloadManager:
    async def download(
        self,
        file_record_id: int,
        metadata: FileMetadata,
        progress_callback: object | None = None,
    ) -> DownloadResult:
        if progress_callback is not None:
            callback = cast(ProgressCallback, progress_callback)
            await callback(
                ProgressSnapshot(
                    current=49,
                    total=50,
                    speed_bytes_per_second=1000,
                    eta_seconds=1,
                )
            )
            await asyncio.sleep(0.02)
        path = Path("downloads/complete.txt")
        return DownloadResult(
            file_record_id=file_record_id,
            metadata=metadata,
            path=path,
            filename=path.name,
            size=50,
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
        forward_origin_chat_id=None,
        forward_origin_message_id=None,
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
    snapshot = queue.snapshot()
    assert snapshot.current_download is None
    assert snapshot.queue_length == 0
    assert snapshot.completed_since_startup == 1
    assert snapshot.failed_since_startup == 0


def test_download_queue_edits_progress_to_done_on_completion(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = _metadata()
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    bot = FakeBot()
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, NearlyCompleteDownloadManager()),  # type: ignore[arg-type]
        bot=cast(Bot, bot),
        logger=logging.getLogger("test"),
        progress_interval_seconds=0.001,
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

    progress_edits = [edit for edit in bot.edits if "<b>📥 Downloading</b>" in edit]
    assert any("████████████████████ <b>100%</b>" in edit for edit in progress_edits)
    assert progress_edits[-1] == bot.edits[-1]
    assert "████████████████████ <b>100%</b>" in bot.edits[-1]
    assert "✅ Done" in bot.edits[-1]
    assert "💾 50 B / 50 B" in bot.edits[-1]
    assert len(bot.messages) == 1


def test_download_queue_retries_after_failure(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = _metadata()
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    manager = FailingOnceDownloadManager()
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, manager),  # type: ignore[arg-type]
        bot=cast(Bot, FakeBot()),
        logger=logging.getLogger("test"),
        retry_limit=1,
        retry_backoff_base_seconds=0,
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
        await asyncio.wait_for(queue._queue.join(), timeout=3)  # noqa: SLF001
        await queue.stop()

    asyncio.run(run_queue())

    assert manager.calls == 2
    assert queue.status(file_record.id) == DownloadJobStatus.COMPLETED
    assert queue.snapshot().completed_since_startup == 1


def test_download_queue_uses_capped_exponential_retry_delay() -> None:
    assert _retry_delay(attempt=1, base_seconds=1, max_seconds=30) == 1
    assert _retry_delay(attempt=2, base_seconds=1, max_seconds=30) == 2
    assert _retry_delay(attempt=3, base_seconds=1, max_seconds=30) == 4
    assert _retry_delay(attempt=10, base_seconds=1, max_seconds=30) == 30


def test_download_queue_recovers_interrupted_job(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = _metadata()
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    repository.save_download(
        file_id=file_record.id,
        total_bytes=metadata.size,
        status_chat_id=metadata.chat_id,
        status_message_id=99,
    )
    manager = FakeDownloadManager()
    bot = FakeBot()
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, manager),  # type: ignore[arg-type]
        bot=cast(Bot, bot),
        logger=logging.getLogger("test"),
        progress_interval_seconds=0,
    )

    assert queue.recover_interrupted_jobs() == 1
    assert queue.recover_interrupted_jobs() == 0
    assert queue.snapshot().queue_length == 1

    async def run_queue() -> None:
        queue.start()
        await asyncio.wait_for(queue._queue.join(), timeout=2)  # noqa: SLF001
        await queue.stop()

    asyncio.run(run_queue())

    assert queue.status(file_record.id) == DownloadJobStatus.COMPLETED
    assert bot.messages


def test_download_queue_requeues_failed_download_for_admin_retry(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = _metadata()
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    repository.save_download(
        file_record.id,
        total_bytes=12,
        status_chat_id=metadata.chat_id,
        status_message_id=99,
    )
    repository.mark_file_queued(file_record.id)
    download = repository.mark_download_failed(file_record.id, "temporary network error", 3)
    assert download is not None
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, FakeDownloadManager()),  # type: ignore[arg-type]
        bot=cast(Bot, FakeBot()),
        logger=logging.getLogger("test"),
    )

    assert queue.retry_failed_download(file_record, download) is True

    retried_download = repository.get_download(file_record.id)
    retried_file = repository.get_file_record(file_record.id)
    assert retried_download is not None
    assert retried_file is not None
    assert retried_download.status == DOWNLOAD_STATUS_QUEUED
    assert retried_file.status == FILE_STATUS_QUEUED
    assert queue.snapshot().queue_length == 1
    database.close()


def test_download_queue_skips_permanent_failed_download_for_admin_retry(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = _metadata()
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    repository.save_download(
        file_record.id,
        total_bytes=12,
        status_chat_id=metadata.chat_id,
        status_message_id=99,
    )
    repository.mark_file_queued(file_record.id)
    download = repository.mark_download_failed(file_record.id, "File is too big", 3)
    assert download is not None
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, FakeDownloadManager()),  # type: ignore[arg-type]
        bot=cast(Bot, FakeBot()),
        logger=logging.getLogger("test"),
    )

    assert queue.retry_failed_download(file_record, download) is False
    assert queue.snapshot().queue_length == 0
    database.close()


def test_download_queue_throttles_progress_persistence(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = CountingRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = _metadata()
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    bot = FakeBot()
    queue = DownloadQueue(
        repository=cast(DatabaseRepository, repository),
        download_manager=cast(object, ProgressBurstDownloadManager()),  # type: ignore[arg-type]
        bot=cast(Bot, bot),
        logger=logging.getLogger("test"),
        progress_interval_seconds=0.05,
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

    assert repository.progress_update_count <= 2
    assert repository.progress_update_count < 50
    assert queue.status(file_record.id) == DownloadJobStatus.COMPLETED
    progress_edits = [edit for edit in bot.edits if "<b>📥 Downloading</b>" in edit]
    assert progress_edits
    assert any("████████████████████ <b>100%</b>" in edit for edit in progress_edits)
    assert any("📄 <b>example.txt</b>" in edit for edit in progress_edits)
    assert any("💾 Total: 50 B" in edit for edit in progress_edits)
    assert any("💾 50 B / 50 B" in edit for edit in progress_edits)
    assert any("🚀 1000 B/s" in edit for edit in progress_edits)
    assert any("⏱ 0s remaining" in edit for edit in progress_edits)
    assert any("🕒 Updated just now" in edit for edit in progress_edits)
    assert any(kwargs.get("parse_mode") == "HTML" for kwargs in bot.edit_kwargs)
    assert len(bot.messages) == 1


def test_download_queue_cancels_active_download_task(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = _metadata()
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    manager = CancellableDownloadManager()
    bot = FakeBot()
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, manager),  # type: ignore[arg-type]
        bot=cast(Bot, bot),
        logger=logging.getLogger("test"),
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
        await asyncio.wait_for(manager.started.wait(), timeout=2)
        assert queue.cancel(file_record.id, source="test")
        await asyncio.wait_for(queue._queue.join(), timeout=2)  # noqa: SLF001
        await queue.stop()

    asyncio.run(run_queue())

    assert manager.cancelled
    assert queue.status(file_record.id) == DownloadJobStatus.CANCELLED
    download = repository.get_download(file_record.id)
    assert download is not None
    assert download.status == DOWNLOAD_STATUS_CANCELLED
    assert "Download cancelled." in bot.edits


def test_download_queue_survives_cancellation(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    first_metadata = _metadata(message_id=10)
    second_metadata = _metadata(message_id=11)
    first = repository.create_file_record(user_id=user.id, metadata=first_metadata)
    second = repository.create_file_record(user_id=user.id, metadata=second_metadata)
    manager = FirstCancellableThenSuccessDownloadManager()
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, manager),  # type: ignore[arg-type]
        bot=cast(Bot, FakeBot()),
        logger=logging.getLogger("test"),
    )

    async def run_queue() -> None:
        queue.start()
        await queue.enqueue(
            DownloadJob(first.id, first_metadata, first_metadata.chat_id, status_message_id=99)
        )
        await queue.enqueue(
            DownloadJob(second.id, second_metadata, second_metadata.chat_id, status_message_id=100)
        )
        await asyncio.wait_for(manager.started.wait(), timeout=2)
        assert queue.cancel(first.id, source="test")
        await asyncio.wait_for(queue._queue.join(), timeout=2)  # noqa: SLF001
        await queue.stop()

    asyncio.run(run_queue())

    assert manager.cancelled
    assert queue.status(first.id) == DownloadJobStatus.CANCELLED
    assert queue.status(second.id) == DownloadJobStatus.COMPLETED


def test_download_queue_cancels_queued_job(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = FileMetadata(
        telegram_file_id="file-id",
        message_id=10,
        chat_id=20,
        forward_origin_chat_id=None,
        forward_origin_message_id=None,
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


def test_download_queue_recovery_ignores_cancelled_download(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    user = repository.create_user(telegram_user_id=1, username=None, first_name=None)
    metadata = _metadata()
    file_record = repository.create_file_record(user_id=user.id, metadata=metadata)
    repository.save_download(
        file_record.id,
        total_bytes=12,
        status_chat_id=metadata.chat_id,
        status_message_id=99,
    )
    repository.cancel_download(file_record.id)
    queue = DownloadQueue(
        repository=repository,
        download_manager=cast(object, FakeDownloadManager()),  # type: ignore[arg-type]
        bot=cast(Bot, FakeBot()),
        logger=logging.getLogger("test"),
    )

    recovered = queue.recover_interrupted_jobs()

    assert recovered == 0
    assert queue.snapshot().queue_length == 0
    database.close()


def _metadata(message_id: int = 10) -> FileMetadata:
    return FileMetadata(
        telegram_file_id="file-id",
        message_id=message_id,
        chat_id=20,
        forward_origin_chat_id=None,
        forward_origin_message_id=None,
        original_name="example.txt",
        mime_type="text/plain",
        size=5,
        extension=".txt",
        file_type=TelegramFileType.DOCUMENT,
        created_at="2026-07-24T00:00:00+00:00",
    )
