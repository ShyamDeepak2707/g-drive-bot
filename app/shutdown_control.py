from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass


@dataclass(frozen=True)
class ShutdownRequestResult:
    initiated: bool
    source: str


class ShutdownController:
    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        signal_delay_seconds: float = 0.25,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._signal_delay_seconds = signal_delay_seconds
        self._event = asyncio.Event()
        self._requested = False
        self._source: str | None = None

    @property
    def is_shutdown_requested(self) -> bool:
        return self._requested

    @property
    def source(self) -> str | None:
        return self._source

    def request_shutdown(self, *, source: str) -> ShutdownRequestResult:
        if self._requested:
            return ShutdownRequestResult(initiated=False, source=self._source or source)

        self._requested = True
        self._source = source
        self._logger.info(
            "graceful shutdown requested",
            extra={
                "event": "graceful_shutdown_requested",
                "shutdown_source": source,
            },
        )
        self._schedule_shutdown_signal()
        return ShutdownRequestResult(initiated=True, source=source)

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signame in ("SIGINT", "SIGTERM"):
            signal_value = getattr(signal, signame, None)
            if signal_value is None:
                continue
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(
                    signal_value,
                    self._request_signal_shutdown,
                    signame,
                )

    async def wait(self) -> None:
        await self._event.wait()

    def _schedule_shutdown_signal(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event.set()
            return
        loop.call_later(self._signal_delay_seconds, self._event.set)

    def _request_signal_shutdown(self, signame: str) -> None:
        self.request_shutdown(source=signame)
