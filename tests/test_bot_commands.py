from __future__ import annotations

from app import constants
from app.telegram_bot import bot_commands


def test_bot_commands_include_supported_commands() -> None:
    commands = bot_commands()

    assert [command.command for command in commands] == [
        constants.START_COMMAND,
        constants.STATUS_COMMAND,
        constants.CANCEL_COMMAND,
    ]
    assert [command.description for command in commands] == [
        "Start the bot",
        "Show download, upload, and queue status",
        "Cancel the current action or download",
    ]


def test_bot_commands_are_valid_for_telegram_menu() -> None:
    for command in bot_commands():
        assert command.command.islower()
        assert "_" not in command.command
        assert 1 <= len(command.command) <= 32
        assert 1 <= len(command.description) <= 256
