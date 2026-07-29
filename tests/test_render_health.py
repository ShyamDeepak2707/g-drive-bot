from __future__ import annotations

import asyncio
import logging

import pytest

from app.exceptions import StartupValidationError
from app.render_health import RenderHealthServer, start_render_health_server_from_env


def test_render_health_server_is_disabled_without_port() -> None:
    async def run() -> None:
        server = await start_render_health_server_from_env(
            env={},
            logger=logging.getLogger("tests.render_health"),
        )
        assert server is None

    asyncio.run(run())


def test_render_health_server_rejects_invalid_port() -> None:
    async def run() -> None:
        with pytest.raises(StartupValidationError, match="PORT must be an integer"):
            await start_render_health_server_from_env(
                env={"PORT": "not-a-port"},
                logger=logging.getLogger("tests.render_health"),
            )

    asyncio.run(run())


def test_render_health_server_returns_ok_response() -> None:
    async def run() -> None:
        server = await RenderHealthServer.start(
            host="127.0.0.1",
            port=0,
            logger=logging.getLogger("tests.render_health"),
        )
        try:
            sockets = server.sockets
            assert sockets
            host, port = sockets[0].getsockname()[:2]
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            assert b"HTTP/1.1 200 OK" in response
            assert response.endswith(b"ok\n")
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(run())
