from __future__ import annotations

from app import constants
from app.telegram_bot import bot_commands


def test_bot_commands_include_supported_commands() -> None:
    commands = bot_commands()

    assert [command.command for command in commands] == [
        constants.START_COMMAND,
        constants.HELP_COMMAND,
        constants.PING_COMMAND,
        constants.ID_COMMAND,
        constants.SETTINGS_COMMAND,
        constants.UPLOAD_SHORT_COMMAND,
        constants.UPLOAD_COMMAND,
        constants.STATUS_COMMAND,
        constants.HEALTH_COMMAND,
        constants.QUEUES_COMMAND,
        constants.FAILED_COMMAND,
        constants.STATS_COMMAND,
        constants.RETRY_FAILED_COMMAND,
        constants.CLEANUP_TEMP_COMMAND,
        constants.SHUTDOWN_COMMAND,
        constants.CANCEL_COMMAND,
    ]
    assert [command.description for command in commands] == [
        "Start the bot",
        "Show available commands",
        "Check bot responsiveness",
        "Show your Telegram IDs",
        "Show safe runtime settings",
        "Upload a direct download link",
        "Upload a direct download link",
        "Show download, upload, and queue status",
        "Show system health",
        "Inspect active and queued jobs",
        "Show failed jobs",
        "Show operational statistics",
        "Retry eligible failed jobs",
        "Preview temporary file cleanup",
        "Gracefully shut down the bot",
        "Cancel the current action or download",
    ]


def test_bot_commands_are_valid_for_telegram_menu() -> None:
    for command in bot_commands():
        assert command.command.islower()
        assert 1 <= len(command.command) <= 32
        assert 1 <= len(command.description) <= 256
