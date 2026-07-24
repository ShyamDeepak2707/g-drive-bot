from __future__ import annotations

import logging
from typing import cast

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from app.config import Settings
from app.constants import START_COMMAND
from app.database import DatabaseRepository

REPOSITORY_KEY = "repository"
LOGGER_KEY = "logger"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user and user.first_name else "there"
    message = (
        f"Hi {name}. Send me files and I will be able to organize them in "
        "Google Drive once uploads are enabled."
    )

    if update.message is None:
        _get_logger(context).warning("/start received without a message payload")
        return

    if user is not None:
        repository = _get_repository(context)
        repository.create_user(
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
        )

    await update.message.reply_text(message)


def create_application(settings: Settings) -> Application:
    return ApplicationBuilder().token(settings.telegram_bot_token).build()


def register_handlers(
    application: Application,
    repository: DatabaseRepository,
    logger: logging.Logger,
) -> None:
    application.bot_data[REPOSITORY_KEY] = repository
    application.bot_data[LOGGER_KEY] = logger
    application.add_handler(CommandHandler(START_COMMAND, start_command))
    application.add_error_handler(global_error_handler)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger = _get_logger(context)
    logger.error(
        "telegram update failed",
        extra={"event": "telegram_update_error"},
        exc_info=context.error,
    )


def _get_repository(context: ContextTypes.DEFAULT_TYPE) -> DatabaseRepository:
    return cast(DatabaseRepository, context.application.bot_data[REPOSITORY_KEY])


def _get_logger(context: ContextTypes.DEFAULT_TYPE) -> logging.Logger:
    return cast(logging.Logger, context.application.bot_data[LOGGER_KEY])
