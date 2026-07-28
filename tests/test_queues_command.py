from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.admin_commands import AdminCommandService
from app.admin_service import QueueInspection
from app.download_queue import CurrentDownloadSnapshot
from app.telegram_bot import (
    ADMIN_COMMAND_SERVICE_KEY,
    ADMIN_USER_IDS_KEY,
    LOGGER_KEY,
    queues_command,
)
from app.upload_worker import CurrentUploadSnapshot


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
    def __init__(self, inspection: QueueInspection) -> None:
        self.queue_inspection_calls = 0
        self._inspection = inspection

    def queue_inspection(self) -> QueueInspection:
        self.queue_inspection_calls += 1
        return self._inspection


def test_queues_command_rejects_non_admin_user() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(_busy_inspection())
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        queues_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.queue_inspection_calls == 0
    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_queues_command_formats_active_and_pending_jobs() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(_busy_inspection())
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        queues_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.queue_inspection_calls == 1
    reply = message.replies[0]
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "📊 <b>Queues</b>" in reply.text
    assert "⬇️ <b>Pending downloads</b>: <b>3 jobs</b>" in reply.text
    assert ("⬇️ <b>Active downloads</b>: " "<b>#11 movie.mkv (42%, Downloading)</b>") in reply.text
    assert "⬆️ <b>Pending uploads</b>: <b>2 jobs</b>" in reply.text
    assert "⬆️ <b>Active uploads</b>: <b>#12 archive.zip (88%, Uploading)</b>" in reply.text
    assert "No queued or active jobs" not in reply.text
    assert "🕒 Updated just now" in reply.text


def test_queues_command_formats_no_work_message() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(
        QueueInspection(
            pending_downloads=0,
            active_download=None,
            pending_uploads=0,
            active_upload=None,
            upload_worker_busy=False,
        )
    )
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        queues_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert "⬇️ <b>Pending downloads</b>: <b>Empty</b>" in reply.text
    assert "⬇️ <b>Active downloads</b>: <b>None</b>" in reply.text
    assert "⬆️ <b>Pending uploads</b>: <b>Empty</b>" in reply.text
    assert "⬆️ <b>Active uploads</b>: <b>None</b>" in reply.text
    assert "🟢 <b>Work</b>: <b>No queued or active jobs</b>" in reply.text


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
                LOGGER_KEY: logging.getLogger("tests.queues_command"),
            }
        )
    )


def _busy_inspection() -> QueueInspection:
    return QueueInspection(
        pending_downloads=3,
        active_download=CurrentDownloadSnapshot(
            file_record_id=11,
            filename="movie.mkv",
            progress_percent=42,
            speed_bytes_per_second=1024.0,
        ),
        pending_uploads=2,
        active_upload=CurrentUploadSnapshot(
            file_record_id=12,
            filename="archive.zip",
            progress_percent=88,
            status="uploading",
        ),
        upload_worker_busy=True,
    )
