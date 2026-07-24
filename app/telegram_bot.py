from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import cast

from telegram import Message, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import constants
from app.config import Settings
from app.database import DatabaseRepository
from app.download_queue import DownloadJob, DownloadQueue
from app.exceptions import RenameValidationError
from app.models import FileMetadata, TelegramFileType
from app.utils.filesystem import unique_path
from app.utils.time_utils import utc_now_iso
from app.utils.validators import validate_filename

REPOSITORY_KEY = "repository"
LOGGER_KEY = "logger"
DOWNLOAD_QUEUE_KEY = "download_queue"
PENDING_RENAME_KEY = "pending_rename_file_id"


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
    download_queue: DownloadQueue | None,
    logger: logging.Logger,
) -> None:
    application.bot_data[REPOSITORY_KEY] = repository
    application.bot_data[DOWNLOAD_QUEUE_KEY] = download_queue
    application.bot_data[LOGGER_KEY] = logger
    application.add_handler(CommandHandler(constants.START_COMMAND, start_command))
    application.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO
            | filters.PHOTO
            | filters.ANIMATION
            | filters.VOICE,
            handle_media_message,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_rename_callback,
            pattern=f"^{constants.RENAME_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rename_text))
    application.add_error_handler(global_error_handler)


async def handle_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        _get_logger(context).warning("media update missing message or user")
        return

    metadata = _extract_file_metadata(message)
    if metadata is None:
        await message.reply_text("This file type is not supported yet.")
        return

    repository = _get_repository(context)
    created_user = repository.create_user(
        telegram_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )
    file_record = repository.create_file_record(user_id=created_user.id, metadata=metadata)

    queue = _get_download_queue(context)
    if queue is None:
        await message.reply_text("File metadata stored, but the download worker is not configured.")
        return

    ack = await message.reply_text(
        f"Received {metadata.file_type.value}. Metadata stored. Download queued."
    )
    await queue.enqueue(
        DownloadJob(
            file_record_id=file_record.id,
            metadata=metadata,
            chat_id=message.chat_id,
            status_message_id=ack.message_id,
        )
    )


async def handle_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    action, file_record_id = _parse_rename_callback(query.data)
    repository = _get_repository(context)

    if action == constants.RENAME_ACTION_KEEP:
        repository.mark_file_ready_for_upload(file_record_id)
        await query.edit_message_text("Original filename kept. Ready for upload later.")
        return

    if action == constants.RENAME_ACTION_SKIP:
        repository.mark_file_skipped(file_record_id)
        await query.edit_message_text("Skipped. This file will not be uploaded later.")
        return

    if action == constants.RENAME_ACTION_RENAME:
        if context.user_data is None:
            await query.edit_message_text("Rename state is unavailable. Try again later.")
            return
        context.user_data[PENDING_RENAME_KEY] = file_record_id
        await query.edit_message_text("Send the new filename for this download.")


async def handle_rename_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    pending_file_id = context.user_data.get(PENDING_RENAME_KEY)
    if pending_file_id is None:
        return
    message = update.effective_message
    if message is None or message.text is None:
        return

    repository = _get_repository(context)
    file_record_id = int(pending_file_id)
    try:
        requested_name = validate_filename(message.text)
        target_name = _append_original_extension_if_missing(
            repository, file_record_id, requested_name
        )
        target_name = validate_filename(target_name)
        new_path = _rename_downloaded_file(repository, file_record_id, target_name)
    except RenameValidationError as exc:
        await message.reply_text(f"Invalid filename: {exc}")
        return
    except OSError as exc:
        await message.reply_text(f"Could not rename the downloaded file: {exc}")
        return

    context.user_data.pop(PENDING_RENAME_KEY, None)
    repository.mark_file_ready_for_upload(file_record_id, original_name=new_path.name)
    await message.reply_text(f"Renamed to {new_path.name}. Ready for upload later.")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger = _get_logger(context)
    logger.error(
        "telegram update failed",
        extra={"event": "telegram_update_error"},
        exc_info=context.error,
    )


def _get_repository(context: ContextTypes.DEFAULT_TYPE) -> DatabaseRepository:
    return cast(DatabaseRepository, context.application.bot_data[REPOSITORY_KEY])


def _get_download_queue(context: ContextTypes.DEFAULT_TYPE) -> DownloadQueue | None:
    return cast(DownloadQueue | None, context.application.bot_data[DOWNLOAD_QUEUE_KEY])


def _get_logger(context: ContextTypes.DEFAULT_TYPE) -> logging.Logger:
    return cast(logging.Logger, context.application.bot_data[LOGGER_KEY])


def _extract_file_metadata(message: Message) -> FileMetadata | None:
    file_type: TelegramFileType
    telegram_file_id: str
    original_name: str | None = None
    mime_type: str | None = None
    size: int | None = None

    if message.document is not None:
        file_type = TelegramFileType.DOCUMENT
        telegram_file_id = message.document.file_id
        original_name = message.document.file_name
        mime_type = message.document.mime_type
        size = message.document.file_size
    elif message.video is not None:
        file_type = TelegramFileType.VIDEO
        telegram_file_id = message.video.file_id
        original_name = message.video.file_name
        mime_type = message.video.mime_type
        size = message.video.file_size
    elif message.audio is not None:
        file_type = TelegramFileType.AUDIO
        telegram_file_id = message.audio.file_id
        original_name = message.audio.file_name
        mime_type = message.audio.mime_type
        size = message.audio.file_size
    elif message.photo:
        file_type = TelegramFileType.PHOTO
        photo = message.photo[-1]
        telegram_file_id = photo.file_id
        original_name = f"photo-{message.message_id}.jpg"
        mime_type = "image/jpeg"
        size = photo.file_size
    elif message.animation is not None:
        file_type = TelegramFileType.ANIMATION
        telegram_file_id = message.animation.file_id
        original_name = message.animation.file_name
        mime_type = message.animation.mime_type
        size = message.animation.file_size
    elif message.voice is not None:
        file_type = TelegramFileType.VOICE
        telegram_file_id = message.voice.file_id
        original_name = f"voice-{message.message_id}.ogg"
        mime_type = message.voice.mime_type
        size = message.voice.file_size
    else:
        return None

    extension = _extension_from_metadata(original_name, mime_type)
    return FileMetadata(
        telegram_file_id=telegram_file_id,
        message_id=message.message_id,
        chat_id=message.chat_id,
        original_name=original_name,
        mime_type=mime_type,
        size=size,
        extension=extension,
        file_type=file_type,
        created_at=utc_now_iso(),
    )


def _extension_from_metadata(original_name: str | None, mime_type: str | None) -> str | None:
    if original_name:
        suffix = Path(original_name).suffix
        if suffix:
            return suffix
    if mime_type:
        return mimetypes.guess_extension(mime_type)
    return None


def _parse_rename_callback(data: str) -> tuple[str, int]:
    prefix, action, file_record_id = data.split(":", maxsplit=2)
    if prefix != constants.RENAME_CALLBACK_PREFIX:
        raise ValueError(f"Unsupported callback prefix: {prefix}")
    return action, int(file_record_id)


def _append_original_extension_if_missing(
    repository: DatabaseRepository,
    file_record_id: int,
    requested_name: str,
) -> str:
    if Path(requested_name).suffix:
        return requested_name
    file_record = repository.get_file_record(file_record_id)
    if file_record is None or not file_record.extension:
        return requested_name
    return f"{requested_name}{file_record.extension}"


def _rename_downloaded_file(
    repository: DatabaseRepository,
    file_record_id: int,
    target_name: str,
) -> Path:
    download = repository.get_download(file_record_id)
    if download is None or download.local_path is None:
        raise OSError("Downloaded file is not available.")
    current_path = Path(download.local_path)
    target_path = unique_path(current_path.with_name(target_name))
    os.replace(current_path, target_path)
    repository.update_download_path(file_record_id, str(target_path))
    return target_path
