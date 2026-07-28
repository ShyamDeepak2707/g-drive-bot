from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pyrogram import Client
from pyrogram.types import InputMediaDocument

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    settings = load_settings()
    if settings.pyrogram_api_id is None or settings.pyrogram_api_hash is None:
        raise RuntimeError("Pyrogram credentials are required for E2E sends.")

    media = [
        InputMediaDocument(media=path, caption=f"e2e-album-{index}")
        for index, path in enumerate(args.paths, start=1)
    ]
    async with Client(
        name=settings.pyrogram_session_name,
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash,
        no_updates=True,
        max_concurrent_transmissions=settings.pyrogram_max_concurrent_transmissions,
        workdir=str(settings.pyrogram_workdir),
    ) as client:
        messages = await client.send_media_group(args.bot, media)
        print("sent message_ids=" + ",".join(str(message.id) for message in messages))


if __name__ == "__main__":
    asyncio.run(main())
