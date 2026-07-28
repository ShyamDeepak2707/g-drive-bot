from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.admin_commands import AdminCommandService
from app.health import HealthSnapshot, QueueHealth
from app.startup_recovery import StartupRecoverySummary
from app.telegram_bot import (
    ADMIN_COMMAND_SERVICE_KEY,
    ADMIN_USER_IDS_KEY,
    LOGGER_KEY,
    health_command,
)


@dataclass
class Reply:
    text: str
    parse_mode: str | None
    disable_web_page_preview: bool | None


class FakeMessage:
    def __init__(self) -> None:
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


class FakeHealthService:
    def __init__(self, snapshot: HealthSnapshot) -> None:
        self.snapshot_calls = 0
        self._snapshot = snapshot

    def snapshot(self) -> HealthSnapshot:
        self.snapshot_calls += 1
        return self._snapshot


def test_health_command_rejects_non_admin_user() -> None:
    message = FakeMessage()
    health_service = FakeHealthService(_health_snapshot())
    context = _context(health_service=health_service, admin_user_ids=(123,))

    asyncio.run(
        health_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert health_service.snapshot_calls == 0
    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_health_command_formats_admin_health_snapshot() -> None:
    message = FakeMessage()
    health_service = FakeHealthService(_health_snapshot())
    context = _context(health_service=health_service, admin_user_ids=(123,))

    asyncio.run(
        health_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert health_service.snapshot_calls == 1
    reply = message.replies[0]
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "📊 <b>System Health</b>" in reply.text
    assert "⏳ <b>Uptime</b>: <b>1h 01m</b>" in reply.text
    assert "💾 <b>Database</b>: <b>🟢 Connected</b>" in reply.text
    assert "ℹ️ <b>Telegram Bot</b>: <b>🟢 Connected</b>" in reply.text
    assert "⬇️ <b>Pyrogram</b>: <b>🔴 Unhealthy</b>" in reply.text
    assert "📂 <b>Google Drive</b>: <b>🟢 Authenticated</b>" in reply.text
    assert "⬇️ <b>Downloads</b>: <b>1 Active / 4 Pending</b>" in reply.text
    assert "⬆️ <b>Uploads</b>: <b>1 Active / 2 Pending</b>" in reply.text
    assert "⚙️ <b>Recovery</b>: <b>Downloads 1, Uploads 2, Cleanup 3</b>" in reply.text
    assert "🕒 Updated just now" in reply.text


def _context(
    *,
    health_service: FakeHealthService,
    admin_user_ids: tuple[int, ...],
) -> FakeContext:
    return FakeContext(
        application=FakeApplication(
            bot_data={
                ADMIN_COMMAND_SERVICE_KEY: AdminCommandService(
                    health_service=health_service,  # type: ignore[arg-type]
                    admin_service=object(),  # type: ignore[arg-type]
                ),
                ADMIN_USER_IDS_KEY: admin_user_ids,
                LOGGER_KEY: logging.getLogger("tests.health_command"),
            }
        )
    )


def _health_snapshot() -> HealthSnapshot:
    return HealthSnapshot(
        startup_time=datetime(2026, 7, 28, 10, 0, 0, tzinfo=UTC),
        uptime_seconds=3661,
        telegram_bot_connected=True,
        pyrogram_connected=False,
        google_drive_authenticated=True,
        database_connected=True,
        queues=QueueHealth(
            pending_downloads=4,
            active_downloads=1,
            pending_uploads=2,
            active_uploads=1,
        ),
        startup_recovery=StartupRecoverySummary(
            downloads_recovered=1,
            uploads_recovered=2,
            cleanup_recovered=3,
        ),
    )
