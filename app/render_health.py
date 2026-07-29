from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from socket import socket

from app.exceptions import StartupValidationError

DEFAULT_RENDER_HEALTH_HOST = "0.0.0.0"
PORT_ENV = "PORT"


class RenderHealthServer:
    def __init__(self, server: asyncio.Server, logger: logging.Logger) -> None:
        self._server = server
        self._logger = logger

    @property
    def sockets(self) -> tuple[socket, ...]:
        return tuple(self._server.sockets or ())

    @classmethod
    async def start(
        cls,
        *,
        host: str,
        port: int,
        logger: logging.Logger,
    ) -> RenderHealthServer:
        server = await asyncio.start_server(_handle_client, host=host, port=port)
        sockets = server.sockets or ()
        bound_addresses = tuple(str(socket.getsockname()) for socket in sockets)
        logger.info(
            "render health server started",
            extra={
                "event": "render_health_server_started",
                "host": host,
                "port": port,
                "bound_addresses": bound_addresses,
            },
        )
        return cls(server=server, logger=logger)

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()
        self._logger.info(
            "render health server stopped",
            extra={"event": "render_health_server_stopped"},
        )


async def start_render_health_server_from_env(
    *,
    env: Mapping[str, str] | None = None,
    logger: logging.Logger,
) -> RenderHealthServer | None:
    values = env or os.environ
    raw_port = values.get(PORT_ENV)
    if raw_port is None or raw_port.strip() == "":
        return None
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise StartupValidationError("PORT must be an integer for Render web deployments.") from exc
    if port < 0 or port > 65535:
        raise StartupValidationError("PORT must be between 0 and 65535.")
    return await RenderHealthServer.start(
        host=DEFAULT_RENDER_HEALTH_HOST,
        port=port,
        logger=logger,
    )


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.read(1024)
        body = b"ok\n"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n" + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()
