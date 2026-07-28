from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pyrogram import Client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", required=True, type=int)
    parser.add_argument("--min-bytes", required=True, type=int)
    parser.add_argument("--limit", default=2000, type=int)
    args = parser.parse_args()

    settings = load_settings()
    if settings.pyrogram_api_id is None or settings.pyrogram_api_hash is None:
        raise RuntimeError("Pyrogram credentials are required for E2E scans.")

    async with Client(
        name=settings.pyrogram_session_name,
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash,
        no_updates=True,
        max_concurrent_transmissions=settings.pyrogram_max_concurrent_transmissions,
        workdir=str(settings.pyrogram_workdir),
    ) as client:
        count = 0
        async for message in client.get_chat_history(args.chat_id, limit=args.limit):
            count += 1
            for media_type in ("document", "video"):
                media = getattr(message, media_type, None)
                if media is None:
                    continue
                size = getattr(media, "file_size", None)
                if isinstance(size, int) and size >= args.min_bytes:
                    print(
                        "found "
                        f"message_id={message.id} "
                        f"media_type={media_type} "
                        f"size={size} "
                        f"name={getattr(media, 'file_name', None)}"
                    )
                    return
        print(f"not found scanned={count}")


if __name__ == "__main__":
    asyncio.run(main())
