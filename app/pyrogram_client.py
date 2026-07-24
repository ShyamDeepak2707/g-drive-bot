from __future__ import annotations

from pyrogram import Client

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger(__name__)


def create_pyrogram_client(settings: Settings) -> Client | None:
    if settings.pyrogram_api_id is None or settings.pyrogram_api_hash is None:
        logger.warning("Pyrogram credentials are incomplete; client initialization skipped")
        return None

    settings.pyrogram_workdir.mkdir(parents=True, exist_ok=True)
    client = Client(
        name=settings.pyrogram_session_name,
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash,
        workdir=str(settings.pyrogram_workdir),
    )
    logger.info("Pyrogram client initialized but not started")
    return client
