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
    parser.add_argument("--bot", required=True)
    parser.add_argument("--chat-id", required=True, type=int)
    parser.add_argument("--message-id", required=True, type=int)
    args = parser.parse_args()

    settings = load_settings()
    if settings.pyrogram_api_id is None or settings.pyrogram_api_hash is None:
        raise RuntimeError("Pyrogram credentials are required for E2E forwards.")

    async with Client(
        name=settings.pyrogram_session_name,
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash,
        no_updates=True,
        max_concurrent_transmissions=settings.pyrogram_max_concurrent_transmissions,
        workdir=str(settings.pyrogram_workdir),
    ) as client:
        messages = await client.forward_messages(
            chat_id=args.bot,
            from_chat_id=args.chat_id,
            message_ids=args.message_id,
        )
        message = messages[0] if isinstance(messages, list) else messages
        print(f"forwarded message_id={message.id}")


if __name__ == "__main__":
    asyncio.run(main())
