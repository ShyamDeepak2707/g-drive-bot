from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine

from app.constants import TASK_SHUTDOWN_TIMEOUT_SECONDS


class AsyncTaskManager:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._tasks: set[asyncio.Task[object]] = set()

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def create_task(
        self, coroutine: Coroutine[object, object, object], name: str
    ) -> asyncio.Task[object]:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        self._logger.info("async task created", extra={"event": "task_created"})
        return task

    async def shutdown(self, grace_period: float = TASK_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        if not self._tasks:
            self._logger.info("async task manager stopped", extra={"event": "tasks_stopped"})
            return

        self._logger.info("cancelling async tasks", extra={"event": "tasks_cancelling"})
        for task in self._tasks:
            task.cancel()

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=grace_period,
            )
        self._tasks.clear()
        self._logger.info("async task manager stopped", extra={"event": "tasks_stopped"})

    def _on_task_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self._logger.error(
                "async task failed",
                extra={"event": "task_failed"},
                exc_info=exception,
            )
