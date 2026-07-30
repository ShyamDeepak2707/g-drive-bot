from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager


class TransferCoordinator:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(
        self,
        *,
        transfer_type: str,
        file_record_id: int,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> AsyncIterator[None]:
        waited = False
        while True:
            if cancellation_check is not None and cancellation_check():
                raise asyncio.CancelledError
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=0.2)
                break
            except TimeoutError:
                if not waited:
                    waited = True
                    self._logger.info(
                        "transfer waiting for active transfer to finish",
                        extra={
                            "event": "transfer_waiting",
                            "transfer_type": transfer_type,
                            "file_record_id": file_record_id,
                        },
                    )

        self._logger.info(
            "transfer slot acquired",
            extra={
                "event": "transfer_slot_acquired",
                "transfer_type": transfer_type,
                "file_record_id": file_record_id,
                "waited": waited,
            },
        )
        try:
            yield
        finally:
            self._lock.release()
            self._logger.info(
                "transfer slot released",
                extra={
                    "event": "transfer_slot_released",
                    "transfer_type": transfer_type,
                    "file_record_id": file_record_id,
                },
            )
