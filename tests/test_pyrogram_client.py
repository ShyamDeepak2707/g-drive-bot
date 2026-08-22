from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.config import Settings
from app.pyrogram_client import (
    PyrogramBotSessionManager,
    PyrogramSessionManager,
    create_pyrogram_bot_client,
    create_pyrogram_client,
)


class FakeClient:
    def __init__(self, *, is_bot: bool = False) -> None:
        self.is_connected = False
        self.me = type("FakeMe", (), {"is_bot": is_bot})()
        self.start_calls = 0
        self.stop_calls = 0
        self.get_dialogs_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.is_connected = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_connected = False

    async def get_dialogs(self) -> object:
        self.get_dialogs_calls += 1
        for _ in range(3):
            yield object()


def test_pyrogram_session_warms_peer_cache_once() -> None:
    client = FakeClient()
    manager = PyrogramSessionManager(client=client, logger=logging.getLogger("test"))

    async def run() -> None:
        await manager.start()
        await manager.start()
        await manager.stop()

    asyncio.run(run())

    assert client.start_calls == 1
    assert client.get_dialogs_calls == 1
    assert client.stop_calls == 1


def test_pyrogram_bot_session_starts_without_warming_peer_cache() -> None:
    client = FakeClient(is_bot=True)
    manager = PyrogramBotSessionManager(client=client, logger=logging.getLogger("test"))

    async def run() -> None:
        await manager.start()
        await manager.start()
        await manager.stop()

    asyncio.run(run())

    assert client.start_calls == 1
    assert client.get_dialogs_calls == 0
    assert client.stop_calls == 1


def test_create_pyrogram_client_uses_session_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, object]] = []
    _install_fake_pyrogram(monkeypatch, created)

    manager = create_pyrogram_client(
        _settings(
            tmp_path,
            pyrogram_session_string="session-string",
            pyrogram_session_mode="session_string",
        )
    )

    assert manager is not None
    assert created[0]["session_string"] == "session-string"
    assert created[0]["api_id"] == 12345
    assert created[0]["api_hash"] == "api-hash"


def test_create_pyrogram_client_uses_base64_session_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, object]] = []
    _install_fake_pyrogram(monkeypatch, created)

    manager = create_pyrogram_client(
        _settings(tmp_path, pyrogram_session_mode="base64_session_file")
    )

    assert manager is not None
    assert "session_string" not in created[0]
    assert created[0]["name"] == "g_drive_bot"
    assert created[0]["workdir"] == str(tmp_path / "sessions")


def test_create_pyrogram_client_uses_local_session_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, object]] = []
    _install_fake_pyrogram(monkeypatch, created)

    manager = create_pyrogram_client(_settings(tmp_path))

    assert manager is not None
    assert "session_string" not in created[0]
    assert created[0]["name"] == "g_drive_bot"
    assert created[0]["workdir"] == str(tmp_path / "sessions")


def test_create_pyrogram_bot_client_does_not_use_session_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, object]] = []
    _install_fake_pyrogram(monkeypatch, created)

    manager = create_pyrogram_bot_client(
        _settings(
            tmp_path,
            pyrogram_session_string="user-session-string",
            pyrogram_session_mode="session_string",
        )
    )

    assert manager is not None
    assert "session_string" not in created[0]
    assert created[0]["bot_token"] == "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
    assert created[0]["name"] == "g_drive_bot_bot"


def _install_fake_pyrogram(
    monkeypatch: pytest.MonkeyPatch, created: list[dict[str, object]]
) -> None:
    class FakePyrogramClient:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)
            self.is_connected = False
            self.me = type("FakeMe", (), {"is_bot": "bot_token" in kwargs})()

    fake_module = ModuleType("pyrogram")
    fake_module.Client = FakePyrogramClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyrogram", fake_module)


def _settings(
    tmp_path: Path,
    *,
    pyrogram_session_string: str | None = None,
    pyrogram_session_mode: str = "local_session_file",
) -> Settings:
    return Settings(
        app_env="development",
        log_level="INFO",
        log_file=tmp_path / "logs" / "app.log",
        telegram_bot_token="123456789:abcdefghijklmnopqrstuvwxyzABCDE",
        telegram_admin_user_ids=(123,),
        pyrogram_api_id=12345,
        pyrogram_api_hash="api-hash",
        pyrogram_session_name="g_drive_bot",
        pyrogram_workdir=tmp_path / "sessions",
        pyrogram_max_concurrent_transmissions=4,
        google_credentials_file=tmp_path / "credentials.json",
        google_token_file=tmp_path / "tokens" / "token.json",
        google_scopes=("https://www.googleapis.com/auth/drive.file",),
        google_auto_auth=False,
        sqlite_db_path=tmp_path / "data" / "app.sqlite3",
        downloads_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "tmp",
        folder_browser_page_size=8,
        folder_browser_cache_ttl_seconds=300,
        folder_recent_limit=5,
        pyrogram_session_string=pyrogram_session_string,
        pyrogram_session_mode=pyrogram_session_mode,
    )
