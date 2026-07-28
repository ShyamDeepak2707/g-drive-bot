from __future__ import annotations

import asyncio
import logging

from app.pyrogram_client import PyrogramSessionManager


class FakeClient:
    def __init__(self) -> None:
        self.is_connected = False
        self.me = object()
        self.start_calls = 0
        self.stop_calls = 0
        self.get_dialogs_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.is_connected = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_connected = False

    async def get_dialogs(self) -> object:
        self.get_dialogs_calls += 1
        for _ in range(3):
            yield object()


def test_pyrogram_session_warms_peer_cache_once() -> None:
    client = FakeClient()
    manager = PyrogramSessionManager(client=client, logger=logging.getLogger("test"))

    async def run() -> None:
        await manager.start()
        await manager.start()
        await manager.stop()

    asyncio.run(run())

    assert client.start_calls == 1
    assert client.get_dialogs_calls == 1
    assert client.stop_calls == 1
