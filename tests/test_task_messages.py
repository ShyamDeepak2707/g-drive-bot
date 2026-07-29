from __future__ import annotations

import asyncio
import logging

from app.task_messages import ActiveTaskMessageRegistry
from app.ui import TelegramMessage


class FakeTaskMessageBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.deleted: list[tuple[int, int]] = []
        self.next_message_id = 1000

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> object:
        message_id = self.next_message_id
        self.next_message_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
            }
        )
        return FakeSentMessage(message_id)

    async def delete_message(self, chat_id: int, message_id: int) -> object:
        self.deleted.append((chat_id, message_id))
        return object()


class FakeSentMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


def test_registry_promotes_latest_remaining_active_task_message() -> None:
    registry = ActiveTaskMessageRegistry()
    bot = FakeTaskMessageBot()
    replacement_ids: list[int] = []

    registry.register(
        key=("download", 1),
        chat_id=123,
        message_id=10,
        message=TelegramMessage("download card", parse_mode="HTML"),
        replace_message_id=replacement_ids.append,
    )
    registry.register(
        key=("upload", 2),
        chat_id=123,
        message_id=11,
        message=TelegramMessage("upload card", parse_mode="HTML"),
    )
    registry.unregister(("upload", 2))

    asyncio.run(
        registry.promote_latest_active(
            chat_id=123,
            bot=bot,
            logger=logging.getLogger("test.task_messages"),
        )
    )

    assert bot.sent == [
        {
            "chat_id": 123,
            "message_id": 1000,
            "text": "download card",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ]
    assert bot.deleted == [(123, 10)]
    assert replacement_ids == [1000]


def test_registry_does_not_promote_when_chat_has_no_active_tasks() -> None:
    registry = ActiveTaskMessageRegistry()
    bot = FakeTaskMessageBot()

    asyncio.run(
        registry.promote_latest_active(
            chat_id=123,
            bot=bot,
            logger=logging.getLogger("test.task_messages"),
        )
    )

    assert bot.sent == []
    assert bot.deleted == []
