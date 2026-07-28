from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.telegram_bot import (
    ADMIN_USER_IDS_KEY,
    LOGGER_KEY,
    SETTINGS_KEY,
    help_command,
    id_command,
    ping_command,
    settings_command,
)


@dataclass
class Reply:
    text: str
    parse_mode: str | None
    disable_web_page_preview: bool | None


class FakeMessage:
    def __init__(self, chat_id: int = 456) -> None:
        self.chat_id = chat_id
        self.replies: list[Reply] = []

    async def reply_text(
        self,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> None:
        self.replies.append(
            Reply(
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        )


@dataclass
class FakeUser:
    id: int


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


def _context(
    *,
    admin_user_ids: tuple[int, ...],
    settings: Settings | None = None,
) -> FakeContext:
    bot_data: dict[str, object] = {
        ADMIN_USER_IDS_KEY: admin_user_ids,
        LOGGER_KEY: logging.getLogger("tests.utility_commands"),
    }
    if settings is not None:
        bot_data[SETTINGS_KEY] = settings
    return FakeContext(
        application=FakeApplication(
            bot_data=bot_data,
        )
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
