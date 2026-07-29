from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.ui import TelegramMessage

TaskMessageKey = tuple[str, int]


class TaskMessageBot(Protocol):
    async def send_message(
        self,
        chat_id: Any,
        text: str,
        parse_mode: Any = None,
        disable_web_page_preview: Any = None,
    ) -> object: ...

    async def delete_message(self, chat_id: Any, message_id: Any) -> object: ...


@dataclass
class _ActiveTaskMessage:
    key: TaskMessageKey
    chat_id: int
    message_id: int
    message: TelegramMessage
    sequence: int
    replace_message_id: Callable[[int], None] | None = None


class ActiveTaskMessageRegistry:
    def __init__(self) -> None:
        self._messages: dict[TaskMessageKey, _ActiveTaskMessage] = {}
        self._sequence = 0

    def register(
        self,
        *,
        key: TaskMessageKey,
        chat_id: int,
        message_id: int,
        message: TelegramMessage,
        replace_message_id: Callable[[int], None] | None = None,
    ) -> None:
        self._messages[key] = _ActiveTaskMessage(
            key=key,
            chat_id=chat_id,
            message_id=message_id,
            message=message,
            sequence=self._next_sequence(),
            replace_message_id=replace_message_id,
        )

    def update(self, key: TaskMessageKey, message: TelegramMessage) -> None:
        active = self._messages.get(key)
        if active is not None:
            active.message = message

    def unregister(self, key: TaskMessageKey) -> None:
        self._messages.pop(key, None)

    async def promote_latest_active(
        self,
        *,
        chat_id: int,
        bot: Any,
        logger: logging.Logger,
    ) -> None:
        candidates = [message for message in self._messages.values() if message.chat_id == chat_id]
        if not candidates:
            return
        active = max(candidates, key=lambda message: message.sequence)
        try:
            sent_message = await bot.send_message(
                chat_id=active.chat_id,
                text=active.message.text,
                parse_mode=active.message.parse_mode,
                disable_web_page_preview=active.message.disable_web_page_preview,
            )
        except Exception as exc:
            logger.warning(
                "failed to promote active task message",
                extra={
                    "event": "active_task_message_promote_failed",
                    "chat_id": active.chat_id,
                    "message_id": active.message_id,
                    "task_type": active.key[0],
                    "file_record_id": active.key[1],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return

        new_message_id = getattr(sent_message, "message_id", None)
        if not isinstance(new_message_id, int):
            logger.warning(
                "active task message promotion did not return a Telegram message id",
                extra={
                    "event": "active_task_message_promote_id_missing",
                    "chat_id": active.chat_id,
                    "task_type": active.key[0],
                    "file_record_id": active.key[1],
                },
            )
            return

        try:
            await bot.delete_message(chat_id=active.chat_id, message_id=active.message_id)
        except Exception as exc:
            logger.warning(
                "failed to delete previous active task message after promotion",
                extra={
                    "event": "active_task_message_promote_delete_failed",
                    "chat_id": active.chat_id,
                    "message_id": active.message_id,
                    "new_message_id": new_message_id,
                    "task_type": active.key[0],
                    "file_record_id": active.key[1],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

        current = self._messages.get(active.key)
        if current is None or current.message_id != active.message_id:
            return
        current.message_id = new_message_id
        current.sequence = self._next_sequence()
        if current.replace_message_id is not None:
            current.replace_message_id(new_message_id)
        logger.info(
            "active task message promoted",
            extra={
                "event": "active_task_message_promoted",
                "chat_id": active.chat_id,
                "old_message_id": active.message_id,
                "new_message_id": new_message_id,
                "task_type": active.key[0],
                "file_record_id": active.key[1],
            },
        )

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence
