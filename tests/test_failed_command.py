from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.admin_commands import AdminCommandService
from app.admin_service import FailedJobSummary
from app.telegram_bot import (
    ADMIN_COMMAND_SERVICE_KEY,
    ADMIN_USER_IDS_KEY,
    LOGGER_KEY,
    failed_command,
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
    def __init__(self, summaries: tuple[FailedJobSummary, ...]) -> None:
        self.failed_job_summaries_calls = 0
        self._summaries = summaries

    def failed_job_summaries(self) -> tuple[FailedJobSummary, ...]:
        self.failed_job_summaries_calls += 1
        return self._summaries


def test_failed_command_rejects_non_admin_user() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(_failed_jobs())
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        failed_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.failed_job_summaries_calls == 0
    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_failed_command_formats_failed_jobs() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(_failed_jobs())
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        failed_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.failed_job_summaries_calls == 1
    reply = message.replies[0]
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "📊 <b>Failed Jobs</b>" in reply.text
    assert "🔴 <b>Total failed</b>: <b>2 jobs</b>" in reply.text
    assert "⬇️ <b>Failed downloads</b>: <b>1 job</b>" in reply.text
    assert "⬇️ <b>Download</b>: <b>#11 movie.mkv - network reset</b>" in reply.text
    assert "⬆️ <b>Failed uploads</b>: <b>1 job</b>" in reply.text
    assert "⬆️ <b>Upload</b>: <b>#12 archive.zip - drive quota exceeded</b>" in reply.text
    assert "No failed jobs" not in reply.text
    assert "🕒 Updated just now" in reply.text


def test_failed_command_formats_no_failed_jobs() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(())
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        failed_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert "🔴 <b>Total failed</b>: <b>Empty</b>" in reply.text
    assert "🟢 <b>Failures</b>: <b>No failed jobs</b>" in reply.text
    assert "Failed downloads" not in reply.text
    assert "Failed uploads" not in reply.text


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
                LOGGER_KEY: logging.getLogger("tests.failed_command"),
            }
        )
    )


def _failed_jobs() -> tuple[FailedJobSummary, ...]:
    return (
        FailedJobSummary(
            file_record_id=11,
            filename="movie.mkv",
            status="failed",
            file_type="document",
            size=12,
            download_status="failed",
            error_message="network reset",
            upload_retry_count=0,
            updated_at="2026-07-28T10:00:00+00:00",
        ),
        FailedJobSummary(
            file_record_id=12,
            filename="archive.zip",
            status="failed",
            file_type="document",
            size=12,
            download_status=None,
            error_message="drive quota exceeded",
            upload_retry_count=3,
            updated_at="2026-07-28T10:01:00+00:00",
        ),
    )
