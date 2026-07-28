from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.admin_commands import AdminCommandService
from app.shutdown_control import ShutdownRequestResult
from app.telegram_bot import (
    ADMIN_COMMAND_SERVICE_KEY,
    ADMIN_USER_IDS_KEY,
    LOGGER_KEY,
    shutdown_command,
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


class FakeShutdownController:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request_shutdown(self, *, source: str) -> ShutdownRequestResult:
        self.calls.append(source)
        return ShutdownRequestResult(initiated=True, source=source)


def test_shutdown_command_rejects_non_admin_user() -> None:
    message = FakeMessage()
    shutdown_controller = FakeShutdownController()
    context = _context(shutdown_controller=shutdown_controller, admin_user_ids=(123,))

    asyncio.run(
        shutdown_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=999)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    assert shutdown_controller.calls == []
    assert message.replies == [Reply("Unauthorized.", None, None)]


def test_shutdown_command_sends_html_acknowledgement() -> None:
    message = FakeMessage()
    shutdown_controller = FakeShutdownController()
    context = _context(shutdown_controller=shutdown_controller, admin_user_ids=(123,))

    asyncio.run(
        shutdown_command(
            FakeUpdate(effective_message=message, effective_user=FakeUser(id=123)),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )
    )

    reply = message.replies[0]
    assert shutdown_controller.calls == ["telegram_admin"]
    assert reply.parse_mode == "HTML"
    assert reply.disable_web_page_preview is True
    assert "📊 <b>Shutdown</b>" in reply.text
    assert "⚙️ <b>Status</b>: <b>Graceful shutdown started</b>" in reply.text


def _context(
    *,
    shutdown_controller: FakeShutdownController,
    admin_user_ids: tuple[int, ...],
) -> FakeContext:
    return FakeContext(
        application=FakeApplication(
            bot_data={
                ADMIN_COMMAND_SERVICE_KEY: AdminCommandService(
                    health_service=object(),  # type: ignore[arg-type]
                    admin_service=object(),  # type: ignore[arg-type]
                    shutdown_controller=shutdown_controller,  # type: ignore[arg-type]
                ),
                ADMIN_USER_IDS_KEY: admin_user_ids,
                LOGGER_KEY: logging.getLogger("tests.shutdown_command"),
            }
        )
    )
