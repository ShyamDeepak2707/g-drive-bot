from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.exceptions import ConfigurationError
from app.logging_config import get_logger

logger = get_logger(__name__)


class PyrogramSessionManager:
    def __init__(self, client: Any, logger: logging.Logger) -> None:
        self.client = client
        self._logger = logger
        self._running = False
        self._peer_cache_warmed = False

    async def start(self) -> None:
        if self._running or self.client.is_connected:
            self._validate_user_session()
            self._running = True
            await self._warm_peer_cache()
            return
        await self.client.start()
        self._validate_user_session()
        self._running = True
        self._logger.info("pyrogram session started", extra={"event": "pyrogram_started"})
        await self._warm_peer_cache()

    async def stop(self) -> None:
        if not self._running and not self.client.is_connected:
            return
        await self.client.stop()
        self._running = False
        self._logger.info("pyrogram session stopped", extra={"event": "pyrogram_stopped"})

    def is_running(self) -> bool:
        return self._running or bool(self.client.is_connected)

    def _validate_user_session(self) -> None:
        me = getattr(self.client, "me", None)
        if getattr(me, "is_bot", False):
            raise ConfigurationError(
                "Pyrogram must use a Telegram user session for channel-origin downloads. "
                "Recreate the configured PYROGRAM_SESSION_NAME without a bot token."
            )

    async def _warm_peer_cache(self) -> None:
        if self._peer_cache_warmed:
            return
        count = 0
        async for _ in self.client.get_dialogs():
            count += 1
        self._peer_cache_warmed = True
        self._logger.info(
            "pyrogram peer cache warmed",
            extra={"event": "pyrogram_peer_cache_warmed", "dialog_count": count},
        )


def create_pyrogram_client(settings: Settings) -> PyrogramSessionManager | None:
    if settings.pyrogram_api_id is None or settings.pyrogram_api_hash is None:
        logger.info("Pyrogram credentials are incomplete; channel-origin downloads disabled")
        return None

    from pyrogram import Client

    settings.pyrogram_workdir.mkdir(parents=True, exist_ok=True)
    client = Client(
        name=settings.pyrogram_session_name,
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash,
        no_updates=True,
        max_concurrent_transmissions=settings.pyrogram_max_concurrent_transmissions,
        workdir=str(settings.pyrogram_workdir),
    )
    logger.info("Pyrogram user session initialized but not started")
    return PyrogramSessionManager(client=client, logger=logger)
