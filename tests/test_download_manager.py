from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from telegram.error import BadRequest

from app.download_manager import BotApiFile, DownloadManager
from app.exceptions import DownloadError
from app.models import FileMetadata, TelegramFileType
from app.progress import ProgressSnapshot


class FakeBotApiFile:
    def __init__(self, *, fail: bool = False, wait_forever: bool = False) -> None:
        self.file_id = "resolved-file-id"
        self.file_unique_id = "unique-file-id"
        self.file_size = 5
        self.file_path = "documents/example.txt"
        self.fail = fail
        self.wait_forever = wait_forever
        self.custom_path: Path | None = None
        self.started = asyncio.Event()

    async def download_to_drive(self, custom_path: str | Path) -> Path:
        self.custom_path = Path(custom_path)
        self.started.set()
        if self.wait_forever:
            self.custom_path.write_bytes(b"partial")
            await asyncio.Event().wait()
        if self.fail:
            self.custom_path.write_bytes(b"partial")
            raise OSError("disk full")
        self.custom_path.write_bytes(b"hello")
        return self.custom_path


class FakeBotApiClient:
    def __init__(self, file: FakeBotApiFile | None = None) -> None:
        self.file = file or FakeBotApiFile()
        self.requested_file_id: str | None = None
        self.fail_get_file = False
        self.get_me_calls = 0

    async def get_file(self, file_id: str) -> BotApiFile:
        self.requested_file_id = file_id
        if self.fail_get_file:
            raise BadRequest("file not found")
        return self.file

    async def get_me(self) -> object:
        self.get_me_calls += 1
        return type("BotUser", (), {"id": 999, "username": "test_bot"})()


class FakePyrogramMessage:
    def __init__(
        self,
        message_id: int,
        chat_id: int,
        has_media: bool = True,
        document_size: int = 5,
        document_unique_id: str = "unique-file-id",
    ) -> None:
        self.id = message_id
        self.chat = FakePyrogramChat(chat_id)
        self.document = (
            FakePyrogramDocument(document_size, document_unique_id) if has_media else None
        )
        self.video = None
        self.audio = None
        self.photo = None
        self.animation = None
        self.voice = None
        self.text = None
        self.caption = None


class FakePyrogramChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id
        self.type = "channel"
        self.title = "Database"
        self.username = None


class FakePyrogramDocument:
    file_name = "example.txt"
    mime_type = "text/plain"

    def __init__(self, file_size: int = 5, file_unique_id: str = "unique-file-id") -> None:
        self.file_size = file_size
        self.file_unique_id = file_unique_id


class FakePyrogramDialog:
    def __init__(self, chat_id: int) -> None:
        self.chat = FakePyrogramChat(chat_id)


class FakePyrogramClient:
    def __init__(
        self,
        *,
        has_media: bool = True,
        resolved_chat_id: int | None = None,
        document_size: int = 5,
        document_unique_id: str = "unique-file-id",
        downloaded_bytes: bytes = b"hello",
        search_messages: list[FakePyrogramMessage] | None = None,
        chat_history: list[FakePyrogramMessage] | None = None,
    ) -> None:
        self.has_media = has_media
        self.resolved_chat_id = resolved_chat_id
        self.document_size = document_size
        self.document_unique_id = document_unique_id
        self.downloaded_bytes = downloaded_bytes
        self.search_message_results = search_messages or []
        self.chat_history_results = chat_history or []
        self.requested_chat_id: int | str | None = None
        self.requested_message_id: int | None = None
        self.downloaded_message: object | None = None
        self.get_dialogs_calls = 0
        self.get_me_calls = 0
        self.get_messages_calls = 0
        self.search_messages_calls = 0
        self.get_chat_history_calls = 0
        self.get_messages_peer_invalid_once = False
        self.dialog_chat_ids: list[int] = []

    async def get_dialogs(self) -> object:
        self.get_dialogs_calls += 1
        for chat_id in self.dialog_chat_ids:
            yield FakePyrogramDialog(chat_id)

    async def get_me(self) -> object:
        self.get_me_calls += 1
        return type(
            "PyrogramUser",
            (),
            {
                "id": 123456,
                "username": "samuel",
                "phone_number": "+10000000000",
                "first_name": "Samuel",
            },
        )()

    async def get_chat_history(self, chat_id: int | str, limit: int = 0) -> object:
        self.get_chat_history_calls += 1
        for message in self.chat_history_results[:limit]:
            yield message

    async def get_messages(self, chat_id: int | str, message_ids: int) -> FakePyrogramMessage:
        self.get_messages_calls += 1
        self.requested_chat_id = chat_id
        self.requested_message_id = message_ids
        if self.get_messages_peer_invalid_once and self.get_messages_calls == 1:
            raise ValueError(f"Peer id invalid: {chat_id}")
        default_chat_id = chat_id if isinstance(chat_id, int) else 999
        return FakePyrogramMessage(
            message_ids,
            self.resolved_chat_id if self.resolved_chat_id is not None else default_chat_id,
            has_media=self.has_media,
            document_size=self.document_size,
            document_unique_id=self.document_unique_id,
        )

    async def search_messages(
        self,
        chat_id: int | str,
        query: str = "",
        offset: int = 0,
        filter: object = None,
        limit: int = 0,
        from_user: int | str | None = None,
    ) -> object:
        del chat_id, query, offset, filter, from_user
        self.search_messages_calls += 1
        for message in self.search_message_results[:limit]:
            yield message

    async def download_media(
        self,
        message: object,
        file_name: str,
        progress: object | None = None,
    ) -> str:
        self.downloaded_message = message
        path = Path(file_name)
        path.write_bytes(self.downloaded_bytes)
        if progress is not None:
            result = progress(5, 5)  # type: ignore[operator]
            if hasattr(result, "__await__"):
                await result
        return str(path)


class FakePyrogramSession:
    def __init__(self, client: FakePyrogramClient | None = None) -> None:
        self.client = client or FakePyrogramClient()
        self.started = False

    async def start(self) -> None:
        self.started = True


async def _download(
    tmp_path: Path,
    progress: list[ProgressSnapshot] | None = None,
) -> tuple[FakeBotApiClient, Path]:
    bot = FakeBotApiClient()
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=None,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    async def progress_callback(snapshot: ProgressSnapshot) -> None:
        if progress is not None:
            progress.append(snapshot)

    result = await manager.download(
        file_record_id=1,
        metadata=_metadata(),
        progress_callback=progress_callback if progress is not None else None,
    )
    return bot, result.path


def test_download_manager_downloads_bot_api_file_to_unique_path(tmp_path: Path) -> None:
    bot, path = asyncio.run(_download(tmp_path))

    assert bot.requested_file_id == "bot-api-file-id"
    assert bot.file.custom_path is not None
    assert path.exists()
    assert path.name == "example.txt"
    assert path.read_bytes() == b"hello"
    assert not bot.file.custom_path.exists()


def test_download_manager_logs_bot_api_fallback_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = DownloadManager(
        bot=FakeBotApiClient(),
        pyrogram_session=None,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with caplog.at_level(logging.INFO, logger="test"):
        asyncio.run(manager.download(file_record_id=1, metadata=_metadata()))

    decision = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "download_source_selected"
    )
    assert decision.__dict__["detected_download_source"] == "bot_api_file_id"
    assert decision.__dict__["bot_api_fallback_reason"] == "metadata_has_no_forward_origin_ids"
    assert decision.__dict__["metadata_chat_id"] == 2
    assert decision.__dict__["metadata_message_id"] == 1


def test_download_manager_uses_forward_origin_with_pyrogram(tmp_path: Path) -> None:
    bot = FakeBotApiClient()
    session = FakePyrogramSession()
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    result = asyncio.run(
        manager.download(
            file_record_id=1,
            metadata=_metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
        )
    )

    assert session.started
    assert session.client.get_dialogs_calls == 0
    assert session.client.get_messages_calls == 1
    assert session.client.requested_chat_id == -100123
    assert session.client.requested_message_id == 99
    assert session.client.downloaded_message is not None
    assert bot.requested_file_id is None
    assert result.path.read_bytes() == b"hello"


def test_download_manager_uses_pyrogram_bot_dialog_without_forward_origin(
    tmp_path: Path,
) -> None:
    bot = FakeBotApiClient()
    session = FakePyrogramSession()
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    result = asyncio.run(manager.download(file_record_id=1, metadata=_metadata(message_id=212)))

    assert session.started
    assert bot.get_me_calls == 1
    assert session.client.get_messages_calls == 1
    assert session.client.requested_chat_id == "test_bot"
    assert session.client.requested_message_id == 212
    assert session.client.downloaded_message is not None
    assert bot.requested_file_id is None
    assert result.path.read_bytes() == b"hello"


def test_download_manager_allows_bot_dialog_peer_id_mismatch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = FakeBotApiClient()
    session = FakePyrogramSession(FakePyrogramClient(resolved_chat_id=1846101730))
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with caplog.at_level(logging.INFO, logger="test"):
        result = asyncio.run(manager.download(file_record_id=1, metadata=_metadata(message_id=214)))

    mismatch = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "bot_dialog_peer_id_mismatch"
    )
    assert mismatch.__dict__["bot_dialog_peer_id"] == 999
    assert mismatch.__dict__["resolved_chat_id"] == 1846101730
    assert session.client.downloaded_message is not None
    assert bot.requested_file_id is None
    assert result.path.read_bytes() == b"hello"


def test_download_manager_searches_bot_dialog_when_direct_message_size_differs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    matching_message = FakePyrogramMessage(
        message_id=777,
        chat_id=1846101730,
        document_size=5,
    )
    client = FakePyrogramClient(
        resolved_chat_id=1846101730,
        document_size=303 * 1024 * 1024,
        search_messages=[matching_message],
    )
    bot = FakeBotApiClient()
    session = FakePyrogramSession(client)
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with caplog.at_level(logging.INFO, logger="test"):
        result = asyncio.run(manager.download(file_record_id=1, metadata=_metadata(message_id=214)))

    mismatch = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "bot_dialog_direct_message_mismatch"
    )
    resolved = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "bot_dialog_matching_message_resolved"
    )
    assert mismatch.__dict__["resolved_size"] == 303 * 1024 * 1024
    assert resolved.__dict__["resolved_message_id"] == 777
    assert client.search_messages_calls == 1
    assert client.downloaded_message is matching_message
    assert bot.requested_file_id is None
    assert result.path.read_bytes() == b"hello"


def test_download_manager_searches_bot_dialog_when_direct_message_unique_id_differs(
    tmp_path: Path,
) -> None:
    matching_message = FakePyrogramMessage(
        message_id=778,
        chat_id=1846101730,
        document_size=5,
        document_unique_id="expected-unique-id",
    )
    client = FakePyrogramClient(
        resolved_chat_id=1846101730,
        document_size=5,
        document_unique_id="wrong-unique-id",
        search_messages=[matching_message],
    )
    bot = FakeBotApiClient()
    session = FakePyrogramSession(client)
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    result = asyncio.run(
        manager.download(
            file_record_id=1,
            metadata=_metadata(
                message_id=214,
                telegram_file_unique_id="expected-unique-id",
            ),
        )
    )

    assert client.search_messages_calls == 1
    assert client.downloaded_message is matching_message
    assert bot.requested_file_id is None
    assert result.path.read_bytes() == b"hello"


def test_download_manager_searches_bot_dialog_when_direct_message_has_no_media(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    matching_message = FakePyrogramMessage(
        message_id=779,
        chat_id=1846101730,
        document_size=5,
        document_unique_id="expected-unique-id",
    )
    client = FakePyrogramClient(
        has_media=False,
        resolved_chat_id=1846101730,
        search_messages=[matching_message],
    )
    bot = FakeBotApiClient()
    session = FakePyrogramSession(client)
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with caplog.at_level(logging.INFO, logger="test"):
        result = asyncio.run(
            manager.download(
                file_record_id=1,
                metadata=_metadata(
                    message_id=214,
                    telegram_file_unique_id="expected-unique-id",
                ),
            )
        )

    non_media = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "bot_dialog_direct_message_without_expected_media"
    )
    resolved = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "bot_dialog_matching_message_resolved"
    )
    assert non_media.__dict__["expected_file_type"] == "document"
    assert resolved.__dict__["resolved_message_id"] == 779
    assert client.search_messages_calls == 1
    assert client.downloaded_message is matching_message
    assert bot.requested_file_id is None
    assert result.path.read_bytes() == b"hello"


def test_download_manager_falls_back_to_bot_api_when_bot_dialog_has_no_media(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = FakeBotApiClient()
    session = FakePyrogramSession(FakePyrogramClient(has_media=False))
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with caplog.at_level(logging.WARNING, logger="test"):
        result = asyncio.run(manager.download(file_record_id=1, metadata=_metadata()))

    fallback = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "pyrogram_bot_dialog_fallback"
    )
    assert "could not locate the matching media" in fallback.__dict__["error"]
    assert bot.requested_file_id == "bot-api-file-id"
    assert result.path.read_bytes() == b"hello"


def test_download_manager_logs_pyrogram_download_size_match(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = FakePyrogramSession()
    manager = DownloadManager(
        bot=FakeBotApiClient(),
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with caplog.at_level(logging.INFO, logger="test"):
        asyncio.run(
            manager.download(
                file_record_id=1,
                metadata=_metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
            )
        )

    begin = next(
        record for record in caplog.records if getattr(record, "event", None) == "download_begin"
    )
    performance = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "download_performance"
    )
    complete = next(
        record for record in caplog.records if getattr(record, "event", None) == "download_complete"
    )
    decision = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "download_source_selected"
    )
    assert decision.__dict__["detected_download_source"] == "pyrogram_forward_origin"
    assert decision.__dict__["bot_api_fallback_reason"] is None
    assert begin.__dict__["file_name"] == "example.txt"
    assert begin.__dict__["file_size"] == 5
    assert begin.__dict__["mime_type"] == "text/plain"
    assert performance.__dict__["seconds"] > 0
    assert performance.__dict__["size"] == 5
    assert isinstance(performance.__dict__["average_mbps"], float)
    assert complete.__dict__["expected_size"] == 5
    assert complete.__dict__["actual_size"] == 5
    assert complete.__dict__["match"] is True


def test_download_manager_rejects_pyrogram_size_mismatch(tmp_path: Path) -> None:
    session = FakePyrogramSession(FakePyrogramClient(document_size=6, downloaded_bytes=b"hello"))
    manager = DownloadManager(
        bot=FakeBotApiClient(),
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with pytest.raises(DownloadError, match="size mismatch"):
        asyncio.run(
            manager.download(
                file_record_id=1,
                metadata=_metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
            )
        )

    assert not list((tmp_path / "tmp").glob("*.part"))


def test_download_manager_rejects_bot_api_size_mismatch(tmp_path: Path) -> None:
    bot = FakeBotApiClient()
    bot.file.file_size = 6
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=None,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with pytest.raises(DownloadError, match="size mismatch"):
        asyncio.run(manager.download(file_record_id=1, metadata=_metadata()))

    assert not list((tmp_path / "tmp").glob("*.part"))


def test_download_manager_warms_forward_origin_peer_cache(tmp_path: Path) -> None:
    client = FakePyrogramClient()
    client.get_messages_peer_invalid_once = True
    client.dialog_chat_ids = [-100123]
    session = FakePyrogramSession(client)
    manager = DownloadManager(
        bot=FakeBotApiClient(),
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    result = asyncio.run(
        manager.download(
            file_record_id=1,
            metadata=_metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
        )
    )

    assert session.client.get_dialogs_calls == 1
    assert session.client.get_messages_calls == 2
    assert session.client.requested_chat_id == -100123
    assert result.path.read_bytes() == b"hello"


def test_download_manager_reports_unknown_forward_origin_peer(tmp_path: Path) -> None:
    client = FakePyrogramClient()
    client.get_messages_peer_invalid_once = True
    session = FakePyrogramSession(client)
    manager = DownloadManager(
        bot=FakeBotApiClient(),
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with pytest.raises(DownloadError, match="cannot access the forwarded channel"):
        asyncio.run(
            manager.download(
                file_record_id=1,
                metadata=_metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
            )
        )

    assert session.client.get_messages_calls == 1


def test_download_manager_requires_pyrogram_for_forward_origin(tmp_path: Path) -> None:
    manager = DownloadManager(
        bot=FakeBotApiClient(),
        pyrogram_session=None,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with pytest.raises(DownloadError, match="requires Pyrogram"):
        asyncio.run(
            manager.download(
                file_record_id=1,
                metadata=_metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
            )
        )


def test_download_manager_rejects_forward_origin_without_media(tmp_path: Path) -> None:
    session = FakePyrogramSession(FakePyrogramClient(has_media=False))
    manager = DownloadManager(
        bot=FakeBotApiClient(),
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with pytest.raises(DownloadError, match="does not contain the expected document media"):
        asyncio.run(
            manager.download(
                file_record_id=1,
                metadata=_metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
            )
        )


def test_download_manager_rejects_forward_origin_wrong_peer(tmp_path: Path) -> None:
    session = FakePyrogramSession(FakePyrogramClient(resolved_chat_id=-100999))
    manager = DownloadManager(
        bot=FakeBotApiClient(),
        pyrogram_session=session,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with pytest.raises(DownloadError, match="resolved to a different chat"):
        asyncio.run(
            manager.download(
                file_record_id=1,
                metadata=_metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
            )
        )

    assert session.client.downloaded_message is None


def test_download_manager_reports_completion_progress(tmp_path: Path) -> None:
    progress: list[ProgressSnapshot] = []

    asyncio.run(_download(tmp_path, progress))

    assert len(progress) == 1
    assert progress[0].current == 5
    assert progress[0].total == 5
    assert progress[0].speed_bytes_per_second is not None
    assert progress[0].eta_seconds == 0


def test_download_manager_reports_bot_api_get_file_failure(tmp_path: Path) -> None:
    bot = FakeBotApiClient()
    bot.fail_get_file = True
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=None,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with pytest.raises(DownloadError, match="Telegram Bot API download failed"):
        asyncio.run(manager.download(file_record_id=1, metadata=_metadata()))


def test_download_manager_cleans_temp_files_on_download_failure(tmp_path: Path) -> None:
    bot = FakeBotApiClient(FakeBotApiFile(fail=True))
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=None,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    with pytest.raises(DownloadError, match="filesystem error"):
        asyncio.run(manager.download(file_record_id=1, metadata=_metadata()))

    assert not list((tmp_path / "tmp").glob("*.part"))


def test_download_manager_cleans_temp_files_on_cancellation(tmp_path: Path) -> None:
    bot = FakeBotApiClient(FakeBotApiFile(wait_forever=True))
    manager = DownloadManager(
        bot=bot,
        pyrogram_session=None,
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        logger=logging.getLogger("test"),
    )

    async def run_download() -> None:
        task = asyncio.create_task(manager.download(file_record_id=1, metadata=_metadata()))
        await bot.file.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_download())

    assert not list((tmp_path / "tmp").glob("*.part"))


def _metadata(
    message_id: int = 1,
    forward_origin_chat_id: int | None = None,
    forward_origin_message_id: int | None = None,
    telegram_file_unique_id: str | None = None,
) -> FileMetadata:
    return FileMetadata(
        telegram_file_id="bot-api-file-id",
        message_id=message_id,
        chat_id=2,
        forward_origin_chat_id=forward_origin_chat_id,
        forward_origin_message_id=forward_origin_message_id,
        original_name="example.txt",
        mime_type="text/plain",
        size=5,
        extension=".txt",
        file_type=TelegramFileType.DOCUMENT,
        created_at="2026-07-24T00:00:00+00:00",
        telegram_file_unique_id=telegram_file_unique_id,
    )
