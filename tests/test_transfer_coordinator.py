from __future__ import annotations

import asyncio
import logging
import time

from app.transfer_coordinator import TransferCoordinator


def test_transfer_coordinator_serializes_transfers() -> None:
    coordinator = TransferCoordinator(logging.getLogger("test.transfer_coordinator"))
    intervals: list[tuple[str, float, float]] = []

    async def transfer(name: str) -> None:
        async with coordinator.acquire(transfer_type=name, file_record_id=len(intervals) + 1):
            started = time.perf_counter()
            await asyncio.sleep(0.05)
            intervals.append((name, started, time.perf_counter()))

    async def scenario() -> None:
        await asyncio.gather(transfer("download"), transfer("upload"))

    asyncio.run(scenario())

    assert len(intervals) == 2
    first = intervals[0]
    second = intervals[1]
    assert first[2] <= second[1]


def test_transfer_coordinator_honors_cancellation_while_waiting() -> None:
    coordinator = TransferCoordinator(logging.getLogger("test.transfer_coordinator"))
    cancel_event = asyncio.Event()

    async def holder() -> None:
        async with coordinator.acquire(transfer_type="upload", file_record_id=1):
            cancel_event.set()
            await asyncio.sleep(0.05)

    async def waiter() -> None:
        async with coordinator.acquire(
            transfer_type="download",
            file_record_id=2,
            cancellation_check=cancel_event.is_set,
        ):
            raise AssertionError("cancelled waiter should not acquire the transfer slot")

    async def scenario() -> None:
        holder_task = asyncio.create_task(holder())
        await cancel_event.wait()
        try:
            await waiter()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("waiting transfer was not cancelled")
        await holder_task

    asyncio.run(scenario())
