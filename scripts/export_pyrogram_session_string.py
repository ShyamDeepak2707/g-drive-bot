from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pyrogram import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings


async def main() -> None:
    settings = load_settings()
    if settings.pyrogram_api_id is None or settings.pyrogram_api_hash is None:
        raise SystemExit("PYROGRAM_API_ID and PYROGRAM_API_HASH are required.")

    async with Client(
        name=settings.pyrogram_session_name,
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash,
        workdir=str(settings.pyrogram_workdir),
        no_updates=True,
    ) as client:
        me = await client.get_me()
        if getattr(me, "is_bot", False):
            raise SystemExit(
                "The configured Pyrogram session is a bot session, not a user session."
            )
        print(await client.export_session_string())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
