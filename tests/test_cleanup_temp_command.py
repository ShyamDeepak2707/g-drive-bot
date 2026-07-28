from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.admin_commands import AdminCommandService
from app.telegram_bot import (
    ADMIN_COMMAND_SERVICE_KEY,
    ADMIN_USER_IDS_KEY,
    LOGGER_KEY,
    cleanup_temp_command,
)
from app.temp_files import TempCleanupSummary


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
    args: list[str]


class FakeAdminService:
    def __init__(self, summary: TempCleanupSummary) -> None:
        self.cleanup_temp_calls: list[bool] = []
        self._summary = summary

    def cleanup_temp(self, *, confirm: bool = False) -> TempCleanupSummary:
        self.cleanup_temp_calls.append(confirm)
        return self._summary


def test_cleanup_temp_command_rejects_non_admin_user() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(
        TempCleanupSummary(
            confirmed=False,
            total_files=1,
            total_bytes=12,
            deleted_files=0,
            deleted_bytes=0,
            files=(),
        )
    )
    context = _context(admin_service=admin_service, admin_user_ids=(123,), args=[])

    asyncio.run(
        cleanup_temp_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert admin_service.cleanup_temp_calls == []
    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_cleanup_temp_command_defaults_to_dry_run_preview() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(
        TempCleanupSummary(
            confirmed=False,
            total_files=2,
            total_bytes=3072,
            deleted_files=0,
            deleted_bytes=0,
            files=(),
        )
    )
    context = _context(admin_service=admin_service, admin_user_ids=(123,), args=[])

    asyncio.run(
        cleanup_temp_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert admin_service.cleanup_temp_calls == [False]
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "📊 <b>Temporary Cleanup</b>" in reply.text
    assert "⚙️ <b>Mode</b>: <b>Dry Run</b>" in reply.text
    assert "📄 <b>Orphaned files</b>: <b>2 jobs</b>" in reply.text
    assert "💾 <b>Orphaned size</b>: <b>3.0 KB</b>" in reply.text
    assert "Preview only; rerun with --confirm" in reply.text


def test_cleanup_temp_command_confirm_deletes_orphans() -> None:
    message = FakeMessage()
    admin_service = FakeAdminService(
        TempCleanupSummary(
            confirmed=True,
            total_files=2,
            total_bytes=3072,
            deleted_files=2,
            deleted_bytes=3072,
            files=(),
        )
    )
    context = _context(admin_service=admin_service, admin_user_ids=(123,), args=["--confirm"])

    asyncio.run(
        cleanup_temp_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert admin_service.cleanup_temp_calls == [True]
    assert "⚙️ <b>Mode</b>: <b>Confirmed</b>" in reply.text
    assert "🏁 <b>Deleted files</b>: <b>2 jobs</b>" in reply.text
    assert "💾 <b>Deleted size</b>: <b>3.0 KB</b>" in reply.text
    assert "Cleanup complete" in reply.text


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
                LOGGER_KEY: logging.getLogger("tests.cleanup_temp_command"),
            }
        ),
        args=args,
    )
