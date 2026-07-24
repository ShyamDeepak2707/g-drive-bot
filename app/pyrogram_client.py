from __future__ import annotations

import logging

from pyrogram import Client

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger(__name__)


class PyrogramSessionManager:
    def __init__(self, client: Client, logger: logging.Logger) -> None:
        self.client = client
        self._logger = logger
        self._running = False

    async def start(self) -> None:
        if self._running or self.client.is_connected:
            self._running = True
            return
        await self.client.start()
        self._running = True
        self._logger.info("pyrogram session started", extra={"event": "pyrogram_started"})

    async def stop(self) -> None:
        if not self._running and not self.client.is_connected:
            return
        await self.client.stop()
        self._running = False
        self._logger.info("pyrogram session stopped", extra={"event": "pyrogram_stopped"})

    def is_running(self) -> bool:
        return self._running or bool(self.client.is_connected)


def create_pyrogram_client(settings: Settings) -> PyrogramSessionManager | None:
    if settings.pyrogram_api_id is None or settings.pyrogram_api_hash is None:
        logger.warning("Pyrogram credentials are incomplete; client initialization skipped")
        return None

    settings.pyrogram_workdir.mkdir(parents=True, exist_ok=True)
    client = Client(
        name=settings.pyrogram_session_name,
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash,
        bot_token=settings.telegram_bot_token,
        workdir=str(settings.pyrogram_workdir),
    )
    logger.info("Pyrogram client initialized but not started")
    return PyrogramSessionManager(client=client, logger=logger)
