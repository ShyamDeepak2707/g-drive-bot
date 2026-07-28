from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pyrogram import Client  # noqa: E402

from app.config import load_settings  # noqa: E402


async def main() -> None:
    settings = load_settings()
    if settings.pyrogram_api_id is None or settings.pyrogram_api_hash is None:
        raise RuntimeError("Set PYROGRAM_API_ID and PYROGRAM_API_HASH before authorizing.")
    settings.pyrogram_workdir.mkdir(parents=True, exist_ok=True)
    async with Client(
        name=settings.pyrogram_session_name,
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash,
        no_updates=True,
        max_concurrent_transmissions=settings.pyrogram_max_concurrent_transmissions,
        workdir=str(settings.pyrogram_workdir),
    ) as client:
        me = await client.get_me()
        print(f"Authorized Pyrogram user session for {me.id}.")


if __name__ == "__main__":
    asyncio.run(main())
