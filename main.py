from __future__ import annotations

import asyncio
import sys

from app.exceptions import AppError
from app.lifecycle import run


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Shutdown requested.")
    except AppError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
