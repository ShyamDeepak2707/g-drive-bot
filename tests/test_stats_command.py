from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.admin_commands import AdminCommandService
from app.admin_service import RuntimeStatistics
from app.startup_recovery import StartupRecoverySummary
from app.telegram_bot import (
    ADMIN_COMMAND_SERVICE_KEY,
    ADMIN_USER_IDS_KEY,
    LOGGER_KEY,
    stats_command,
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


class FakeAdminService:
    def __init__(self, stats: RuntimeStatistics) -> None:
        self.runtime_statistics_calls = 0
        self._stats = stats

    def runtime_statistics(self) -> RuntimeStatistics:
        self.runtime_statistics_calls += 1
        return self._stats


def test_stats_command_rejects_non_admin_user() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(_runtime_statistics())
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        stats_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.runtime_statistics_calls == 0
    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_stats_command_formats_runtime_statistics() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(_runtime_statistics())
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        stats_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.runtime_statistics_calls == 1
    reply = message.replies[0]
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "📊 <b>Statistics</b>" in reply.text
    assert "⏳ <b>Uptime</b>: <b>1h 01m</b>" in reply.text
    assert "📄 <b>Total files</b>: <b>12 jobs</b>" in reply.text
    assert "⬇️ <b>Completed downloads</b>: <b>7</b>" in reply.text
    assert "⬆️ <b>Completed uploads</b>: <b>5</b>" in reply.text
    assert "🔴 <b>Failed jobs</b>: <b>2 Downloads / 1 Uploads</b>" in reply.text
    assert "🟡 <b>Retry attempts</b>: <b>4 Downloads / 3 Uploads</b>" in reply.text
    assert "🟡 <b>Scheduled retries</b>: <b>1</b>" in reply.text
    assert "📦 <b>Queue</b>: <b>3 Downloads / 2 Uploads</b>" in reply.text
    assert "🔵 <b>Active</b>: <b>1 Downloads / 1 Uploads</b>" in reply.text
    assert "🕒 Updated just now" in reply.text


def _context(
    *,
    admin_service: FakeAdminService,
    admin_user_ids: tuple[int, ...],
) -> FakeContext:
    return FakeContext(
        application=FakeApplication(
            bot_data={
                ADMIN_COMMAND_SERVICE_KEY: AdminCommandService(
                    health_service=object(),  # type: ignore[arg-type]
                    admin_service=admin_service,  # type: ignore[arg-type]
                ),
                ADMIN_USER_IDS_KEY: admin_user_ids,
                LOGGER_KEY: logging.getLogger("tests.stats_command"),
            }
        )
    )


def _runtime_statistics() -> RuntimeStatistics:
    return RuntimeStatistics(
        startup_time=datetime(2026, 7, 28, 10, 0, 0, tzinfo=UTC),
        uptime_seconds=3661,
        telegram_bot_connected=True,
        pyrogram_connected=True,
        google_drive_authenticated=True,
        database_connected=True,
        pending_downloads=3,
        active_downloads=1,
        pending_uploads=2,
        active_uploads=1,
        startup_recovery=StartupRecoverySummary(0, 0, 0),
        total_tracked_files=12,
        completed_downloads=7,
        completed_uploads=5,
        failed_downloads=2,
        failed_uploads=1,
        download_retry_attempts=4,
        upload_retry_attempts=3,
        scheduled_upload_retries=1,
    )
