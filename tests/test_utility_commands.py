from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.database import DatabaseRepository, SQLiteDatabase
from app.download_queue import DownloadJob
from app.telegram_bot import (
    ADMIN_USER_IDS_KEY,
    DOWNLOAD_QUEUE_KEY,
    LOGGER_KEY,
    REPOSITORY_KEY,
    SETTINGS_KEY,
    help_command,
    id_command,
    ping_command,
    settings_command,
    upload_url_command,
)


@dataclass
class Reply:
    text: str
    parse_mode: str | None
    disable_web_page_preview: bool | None


class FakeMessage:
    def __init__(
        self,
        chat_id: int = 456,
        message_id: int = 10,
        text: str | None = None,
        reply_to_message: object | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.reply_to_message = reply_to_message
        self.replies: list[Reply] = []

    async def reply_text(
        self,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> object:
        self.replies.append(
            Reply(
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        )
        return FakeSentMessage(message_id=100 + len(self.replies))


@dataclass
class FakeSentMessage:
    message_id: int


@dataclass
class FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = None


@dataclass
class FakeUpdate:
    effective_message: FakeMessage
    effective_user: FakeUser | None


@dataclass
class FakeApplication:
    bot_data: dict[str, object]


@dataclass
class FakeContext:
    application: FakeApplication
    args: list[str] | None = None


class FakeDownloadQueue:
    def __init__(self) -> None:
        self.jobs: list[DownloadJob] = []

    async def enqueue(self, job: DownloadJob) -> None:
        self.jobs.append(job)


def test_help_command_shows_public_commands_for_regular_user() -> None:
    message = FakeMessage()
    context = _context(admin_user_ids=(123,))

    asyncio.run(
        help_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "📊 <b>Help</b>" in reply.text
    assert "📄 <b>Send files</b>: <b>Download, rename, choose folder, upload</b>" in reply.text
    assert "⬆️ <b>/u link</b>: <b>Download a direct link and upload it</b>" in reply.text
    assert "📊 <b>/status</b>" in reply.text
    assert "/health" not in reply.text


def test_help_command_shows_admin_commands_for_admin_user() -> None:
    message = FakeMessage()
    context = _context(admin_user_ids=(123,))

    asyncio.run(
        help_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert "📂 <b>/health</b>: <b>Check subsystem health</b>" in reply.text
    assert "📦 <b>/queues</b>: <b>Inspect queued and active jobs</b>" in reply.text
    assert "🟡 <b>/retry_failed</b>: <b>Retry eligible failures</b>" in reply.text
    assert "⚠️ <b>/shutdown</b>: <b>Gracefully stop the bot</b>" in reply.text


def test_ping_command_replies_with_responsive_card() -> None:
    message = FakeMessage()
    context = _context(admin_user_ids=())

    asyncio.run(
        ping_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert "📊 <b>Bot Online</b>" in reply.text
    assert "🟢 <b>Status</b>: <b>Responsive</b>" in reply.text
    assert "ℹ️ <b>Mode</b>: <b>Polling</b>" in reply.text


def test_id_command_replies_with_user_and_chat_ids() -> None:
    message = FakeMessage(chat_id=-100456)
    context = _context(admin_user_ids=())

    asyncio.run(
        id_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert "📊 <b>Telegram IDs</b>" in reply.text
    assert "ℹ️ <b>User ID</b>: <b>123</b>" in reply.text
    assert "📦 <b>Chat ID</b>: <b>-100456</b>" in reply.text


def test_settings_command_rejects_non_admin_user() -> None:
    message = FakeMessage()
    context = _context(admin_user_ids=(123,), settings=_settings())

    asyncio.run(
        settings_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_settings_command_shows_safe_runtime_settings_for_admin() -> None:
    message = FakeMessage()
    context = _context(admin_user_ids=(123,), settings=_settings())

    asyncio.run(
        settings_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert reply.parse_mode == "HTML"
    assert "📊 <b>Runtime Settings</b>" in reply.text
    assert "⚙️ <b>Environment</b>: <b>Production</b>" in reply.text
    assert "ℹ️ <b>Log level</b>: <b>INFO</b>" in reply.text
    assert "🟢 <b>Admins</b>: <b>1</b>" in reply.text
    assert "⬇️ <b>Pyrogram</b>: <b>Configured</b>" in reply.text
    assert "⬇️ <b>Pyrogram concurrency</b>: <b>4</b>" in reply.text
    assert "📂 <b>Google auto auth</b>: <b>Disabled</b>" in reply.text
    assert "💾 <b>Downloads dir</b>: <b>downloads</b>" in reply.text
    assert "bot-token" not in reply.text
    assert "api-hash" not in reply.text


def test_upload_url_command_queues_direct_link_download(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "app.sqlite3")
    database.initialize()
    repository = DatabaseRepository(database)
    queue = FakeDownloadQueue()
    message = FakeMessage(
        chat_id=456,
        message_id=42,
        text="/u https://example.com/files/report.pdf",
    )
    context = _context(
        admin_user_ids=(),
        repository=repository,
        download_queue=queue,
        args=["https://example.com/files/report.pdf"],
    )

    asyncio.run(
        upload_url_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job.status_message_id == 101
    metadata = job.metadata
    assert metadata.source_url == "https://example.com/files/report.pdf"
    assert metadata.original_name == "report.pdf"
    records = repository.list_failed_file_records(limit=10)
    assert records == ()
    file_record = repository.get_file_record(job.file_record_id)
    assert file_record is not None
    assert file_record.source_url == "https://example.com/files/report.pdf"
    assert "Received direct link. Download queued." in message.replies[0].text
    database.close()


def _context(
    *,
    admin_user_ids: tuple[int, ...],
    settings: Settings | None = None,
    repository: DatabaseRepository | None = None,
    download_queue: FakeDownloadQueue | None = None,
    args: list[str] | None = None,
) -> FakeContext:
    bot_data: dict[str, object] = {
        ADMIN_USER_IDS_KEY: admin_user_ids,
        LOGGER_KEY: logging.getLogger("tests.utility_commands"),
    }
    if settings is not None:
        bot_data[SETTINGS_KEY] = settings
    if repository is not None:
        bot_data[REPOSITORY_KEY] = repository
    if download_queue is not None:
        bot_data[DOWNLOAD_QUEUE_KEY] = download_queue
    return FakeContext(
        application=FakeApplication(
            bot_data=bot_data,
        ),
        args=args,
    )


def _settings() -> Settings:
    return Settings(
        app_env="production",
        log_level="INFO",
        log_file=Path("logs/app.log"),
        telegram_bot_token="123456789:bot-token",
        telegram_admin_user_ids=(123,),
        pyrogram_api_id=12345,
        pyrogram_api_hash="api-hash",
        pyrogram_session_name="g_drive_bot",
        pyrogram_workdir=Path("sessions"),
        pyrogram_max_concurrent_transmissions=4,
        google_credentials_file=Path("credentials.json"),
        google_token_file=Path("data/token.json"),
        google_scopes=("https://www.googleapis.com/auth/drive.file",),
        google_auto_auth=False,
        sqlite_db_path=Path("data/app.sqlite3"),
        downloads_dir=Path("downloads"),
        temp_dir=Path("tmp"),
        folder_browser_page_size=10,
        folder_browser_cache_ttl_seconds=60,
        folder_recent_limit=5,
    )
