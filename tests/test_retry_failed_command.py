from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.admin_commands import AdminCommandService
from app.admin_service import RetryFailedSummary
from app.telegram_bot import (
    ADMIN_COMMAND_SERVICE_KEY,
    ADMIN_USER_IDS_KEY,
    LOGGER_KEY,
    retry_failed_command,
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
    def __init__(self, summary: RetryFailedSummary) -> None:
        self.retry_failed_jobs_calls = 0
        self._summary = summary

    def retry_failed_jobs(self) -> RetryFailedSummary:
        self.retry_failed_jobs_calls += 1
        return self._summary


def test_retry_failed_command_rejects_non_admin_user() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(
        RetryFailedSummary(eligible_jobs=1, requeued_jobs=1, skipped_jobs=0)
    )
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        retry_failed_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.retry_failed_jobs_calls == 0
    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_retry_failed_command_formats_no_eligible_jobs() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(
        RetryFailedSummary(eligible_jobs=0, requeued_jobs=0, skipped_jobs=2)
    )
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        retry_failed_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert admin_service.retry_failed_jobs_calls == 1
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "📊 <b>Retry Failed Jobs</b>" in reply.text
    assert "🟡 <b>Eligible jobs</b>: <b>Empty</b>" in reply.text
    assert "📦 <b>Requeued jobs</b>: <b>Empty</b>" in reply.text
    assert "⚠️ <b>Skipped jobs</b>: <b>2 jobs</b>" in reply.text
    assert "🟢 <b>Result</b>: <b>No retry-eligible failed jobs</b>" in reply.text


def test_retry_failed_command_formats_mixed_summary() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(
        RetryFailedSummary(eligible_jobs=2, requeued_jobs=2, skipped_jobs=1)
    )
    context = _context(admin_service=admin_service, admin_user_ids=(123,))

    asyncio.run(
        retry_failed_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert "🟡 <b>Eligible jobs</b>: <b>2 jobs</b>" in reply.text
    assert "📦 <b>Requeued jobs</b>: <b>2 jobs</b>" in reply.text
    assert "⚠️ <b>Skipped jobs</b>: <b>1 job</b>" in reply.text
    assert "No retry-eligible failed jobs" not in reply.text
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
                LOGGER_KEY: logging.getLogger("tests.retry_failed_command"),
            }
        )
    )
