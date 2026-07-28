from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.admin_commands import AdminCommandService
from app.admin_service import CancelJobSummary
from app.telegram_bot import (
    ADMIN_COMMAND_SERVICE_KEY,
    ADMIN_USER_IDS_KEY,
    DOWNLOAD_QUEUE_KEY,
    LOGGER_KEY,
    UPLOAD_WORKER_KEY,
    cancel_command,
)


@dataclass
class Reply:
    text: str
    parse_mode: str | None
    disable_web_page_preview: bool | None


class FakeMessage:
    chat_id = 123

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
    args: list[str]
    user_data: dict[str, object] | None = None


class FakeAdminService:
    def __init__(self) -> None:
        self.cancel_job_calls: list[tuple[int, str | None]] = []

    def cancel_job(self, job_id: int, job_type: str | None = None) -> CancelJobSummary:
        self.cancel_job_calls.append((job_id, job_type))
        return CancelJobSummary(
            job_id=job_id,
            job_type=job_type or "download",
            filename="example.txt",
            status="Cancelled",
            message="Cancelled",
            cancelled=True,
        )


def test_cancel_command_rejects_non_admin_job_cancel() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService()
    context = _context(admin_service=admin_service, admin_user_ids=(123,), args=["42"])

    asyncio.run(
        cancel_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.cancel_job_calls == []
    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_cancel_command_delegates_admin_job_cancel() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService()
    context = _context(
        admin_service=admin_service,
        admin_user_ids=(123,),
        args=["42", "upload"],
    )

    asyncio.run(
        cancel_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert admin_service.cancel_job_calls == [(42, "upload")]
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "🛑 <b>Cancel Job</b>" in reply.text
    assert "Cancelled" in reply.text


def _context(
    *,
    admin_service: FakeAdminService,
    admin_user_ids: tuple[int, ...],
    args: list[str],
) -> FakeContext:
    return FakeContext(
        application=FakeApplication(
            bot_data={
                ADMIN_COMMAND_SERVICE_KEY: AdminCommandService(
                    health_service=object(),  # type: ignore[arg-type]
                    admin_service=admin_service,  # type: ignore[arg-type]
                ),
                ADMIN_USER_IDS_KEY: admin_user_ids,
                LOGGER_KEY: logging.getLogger("tests.cancel_command"),
                DOWNLOAD_QUEUE_KEY: None,
                UPLOAD_WORKER_KEY: None,
            }
        ),
        args=args,
        user_data={},
    )
