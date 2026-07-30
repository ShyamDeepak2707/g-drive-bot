from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from googleapiclient.errors import HttpError
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
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
from app.admin_commands import AdminCommandService
from app.config import Settings
from app.database import DatabaseRepository, UserRecord
from app.download_queue import DownloadJob, DownloadQueue, DownloadQueueSnapshot
from app.drive.browser import (
    MY_DRIVE_NAME,
    MY_DRIVE_ROOT_ID,
    DriveFolderBrowser,
    FolderPage,
    SharedDrive,
)
from app.exceptions import RenameValidationError
from app.models import FileMetadata, Folder, TelegramFileType
from app.progress import format_bytes
from app.ui import Icons, TelegramMessage, UIStyle, status_card
from app.upload_worker import UploadWorker, UploadWorkerSnapshot
from app.utils.filesystem import unique_path
from app.utils.time_utils import utc_now_iso
from app.utils.validators import validate_filename

REPOSITORY_KEY = "repository"
SETTINGS_KEY = "settings"
LOGGER_KEY = "logger"
DOWNLOAD_QUEUE_KEY = "download_queue"
UPLOAD_WORKER_KEY = "upload_worker"
ADMIN_COMMAND_SERVICE_KEY = "admin_command_service"
FOLDER_BROWSER_KEY = "folder_browser"
FOLDER_RECENT_LIMIT_KEY = "folder_recent_limit"
ADMIN_USER_IDS_KEY = "admin_user_ids"
PENDING_RENAME_KEY = "pending_rename_file_id"
PENDING_FOLDER_FILE_KEY = "pending_folder_file_id"
PENDING_FOLDER_ID_KEY = "pending_folder_id_file_id"
PENDING_FOLDER_SEARCH_KEY = "pending_folder_search_file_id"
FOLDER_VIEW_KEY = "folder_view"
FOLDER_BACK_STACK_KEY = "folder_back_stack"
FOLDER_SEARCH_QUERY_KEY = "folder_search_query"
FOLDER_SHARED_DRIVES_KEY = "folder_shared_drives"
FOLDER_TOKEN_KEY = "folder_tokens"
FOLDER_TOKEN_COUNTER_KEY = "folder_token_counter"
StatusDisplayMode = Literal["mobile", "desktop"]
STATUS_MOBILE_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━"
STATUS_DESKTOP_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
DESTINATION_PROMPT_RECENT_LIMIT = 3


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user and user.first_name else "there"
    message = (
        f"Hi {name}. Send me files and I will download them, let you choose a "
        "Google Drive folder, and upload them there."
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/help received without a message payload")
        return

    help_message = _format_help_message(
        is_admin=user is not None and _is_admin_user(context, user.id),
    )
    await message.reply_text(
        help_message.text,
        parse_mode=help_message.parse_mode,
        disable_web_page_preview=help_message.disable_web_page_preview,
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        _get_logger(context).warning("/ping received without a message payload")
        return

    ping_message = _format_ping_message()
    await message.reply_text(
        ping_message.text,
        parse_mode=ping_message.parse_mode,
        disable_web_page_preview=ping_message.disable_web_page_preview,
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/id received without a message payload")
        return

    id_message = _format_id_message(
        user_id=user.id if user is not None else None,
        chat_id=message.chat_id,
    )
    await message.reply_text(
        id_message.text,
        parse_mode=id_message.parse_mode,
        disable_web_page_preview=id_message.disable_web_page_preview,
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/settings received without a message payload")
        return
    if user is None or not _is_admin_user(context, user.id):
        _get_logger(context).warning(
            "unauthorized /settings command rejected",
            extra={
                "event": "settings_command_unauthorized",
                "telegram_user_id": getattr(user, "id", None),
            },
        )
        await message.reply_text("Unauthorized.")
        return

    settings_message = _format_settings_message(_get_settings(context))
    await message.reply_text(
        settings_message.text,
        parse_mode=settings_message.parse_mode,
        disable_web_page_preview=settings_message.disable_web_page_preview,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/cancel received without a message payload")
        return
    if _admin_cancel_requested(context):
        if user is None or not _is_admin_user(context, user.id):
            _get_logger(context).warning(
                "unauthorized admin /cancel command rejected",
                extra={
                    "event": "cancel_job_command_unauthorized",
                    "telegram_user_id": getattr(user, "id", None),
                },
            )
            await message.reply_text("Unauthorized.")
            return
        cancel_arguments = _cancel_command_arguments(context)
        if cancel_arguments is None:
            await message.reply_text("Usage: /cancel <job_id> [download|upload]")
            return
        job_id, job_type = cancel_arguments
        cancel_message = _get_admin_command_service(context).cancel_job(
            admin_user_id=user.id,
            job_id=job_id,
            job_type=job_type,
        )
        await message.reply_text(
            cancel_message.text,
            parse_mode=cancel_message.parse_mode,
            disable_web_page_preview=cancel_message.disable_web_page_preview,
        )
        return

    cancelled_state = False
    if context.user_data is not None:
        for key in (
            PENDING_RENAME_KEY,
            PENDING_FOLDER_FILE_KEY,
            PENDING_FOLDER_ID_KEY,
            PENDING_FOLDER_SEARCH_KEY,
        ):
            cancelled_state = context.user_data.pop(key, None) is not None or cancelled_state
        _clear_folder_state(context)

    queue = _get_download_queue(context)
    cancelled_download = (
        queue.cancel_chat(message.chat_id, source="telegram_command")
        if queue is not None
        else False
    )

    if cancelled_download:
        await message.reply_text("Cancelling the current download.")
        return
    if cancelled_state:
        await message.reply_text("Cancelled the current action.")
        return
    await message.reply_text("Nothing is active to cancel.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        _get_logger(context).warning("/status received without a message payload")
        return

    queue = _get_download_queue(context)
    upload_worker = _get_upload_worker(context)
    status_message = _format_system_status(
        download_snapshot=queue.snapshot() if queue is not None else None,
        upload_snapshot=upload_worker.snapshot() if upload_worker is not None else None,
        display_mode=_status_display_mode(context),
    )
    await message.reply_text(
        status_message.text,
        parse_mode=status_message.parse_mode,
        disable_web_page_preview=status_message.disable_web_page_preview,
    )


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/health received without a message payload")
        return
    if user is None or not _is_admin_user(context, user.id):
        _get_logger(context).warning(
            "unauthorized /health command rejected",
            extra={
                "event": "health_command_unauthorized",
                "telegram_user_id": getattr(user, "id", None),
            },
        )
        await message.reply_text("Unauthorized.")
        return

    health_message = _get_admin_command_service(context).health()
    await message.reply_text(
        health_message.text,
        parse_mode=health_message.parse_mode,
        disable_web_page_preview=health_message.disable_web_page_preview,
    )


async def queues_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/queues received without a message payload")
        return
    if user is None or not _is_admin_user(context, user.id):
        _get_logger(context).warning(
            "unauthorized /queues command rejected",
            extra={
                "event": "queues_command_unauthorized",
                "telegram_user_id": getattr(user, "id", None),
            },
        )
        await message.reply_text("Unauthorized.")
        return

    queues_message = _get_admin_command_service(context).queues()
    await message.reply_text(
        queues_message.text,
        parse_mode=queues_message.parse_mode,
        disable_web_page_preview=queues_message.disable_web_page_preview,
    )


async def failed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/failed received without a message payload")
        return
    if user is None or not _is_admin_user(context, user.id):
        _get_logger(context).warning(
            "unauthorized /failed command rejected",
            extra={
                "event": "failed_command_unauthorized",
                "telegram_user_id": getattr(user, "id", None),
            },
        )
        await message.reply_text("Unauthorized.")
        return

    failed_message = _get_admin_command_service(context).failed()
    await message.reply_text(
        failed_message.text,
        parse_mode=failed_message.parse_mode,
        disable_web_page_preview=failed_message.disable_web_page_preview,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/stats received without a message payload")
        return
    if user is None or not _is_admin_user(context, user.id):
        _get_logger(context).warning(
            "unauthorized /stats command rejected",
            extra={
                "event": "stats_command_unauthorized",
                "telegram_user_id": getattr(user, "id", None),
            },
        )
        await message.reply_text("Unauthorized.")
        return

    stats_message = _get_admin_command_service(context).stats()
    await message.reply_text(
        stats_message.text,
        parse_mode=stats_message.parse_mode,
        disable_web_page_preview=stats_message.disable_web_page_preview,
    )


async def retry_failed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/retry_failed received without a message payload")
        return
    if user is None or not _is_admin_user(context, user.id):
        _get_logger(context).warning(
            "unauthorized /retry_failed command rejected",
            extra={
                "event": "retry_failed_command_unauthorized",
                "telegram_user_id": getattr(user, "id", None),
            },
        )
        await message.reply_text("Unauthorized.")
        return

    retry_message = _get_admin_command_service(context).retry_failed(user.id)
    await message.reply_text(
        retry_message.text,
        parse_mode=retry_message.parse_mode,
        disable_web_page_preview=retry_message.disable_web_page_preview,
    )


async def cleanup_temp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/cleanup_temp received without a message payload")
        return
    if user is None or not _is_admin_user(context, user.id):
        _get_logger(context).warning(
            "unauthorized /cleanup_temp command rejected",
            extra={
                "event": "cleanup_temp_command_unauthorized",
                "telegram_user_id": getattr(user, "id", None),
            },
        )
        await message.reply_text("Unauthorized.")
        return

    cleanup_message = _get_admin_command_service(context).cleanup_temp(
        admin_user_id=user.id,
        confirm=_cleanup_temp_confirmed(context),
    )
    await message.reply_text(
        cleanup_message.text,
        parse_mode=cleanup_message.parse_mode,
        disable_web_page_preview=cleanup_message.disable_web_page_preview,
    )


async def shutdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None:
        _get_logger(context).warning("/shutdown received without a message payload")
        return
    if user is None or not _is_admin_user(context, user.id):
        _get_logger(context).warning(
            "unauthorized /shutdown command rejected",
            extra={
                "event": "shutdown_command_unauthorized",
                "telegram_user_id": getattr(user, "id", None),
            },
        )
        await message.reply_text("Unauthorized.")
        return

    shutdown_message = _get_admin_command_service(context).shutdown(admin_user_id=user.id)
    await message.reply_text(
        shutdown_message.text,
        parse_mode=shutdown_message.parse_mode,
        disable_web_page_preview=shutdown_message.disable_web_page_preview,
    )


def bot_commands() -> tuple[BotCommand, ...]:
    return (
        BotCommand(constants.START_COMMAND, "Start the bot"),
        BotCommand(constants.HELP_COMMAND, "Show available commands"),
        BotCommand(constants.PING_COMMAND, "Check bot responsiveness"),
        BotCommand(constants.ID_COMMAND, "Show your Telegram IDs"),
        BotCommand(constants.SETTINGS_COMMAND, "Show safe runtime settings"),
        BotCommand(constants.STATUS_COMMAND, "Show download, upload, and queue status"),
        BotCommand(constants.HEALTH_COMMAND, "Show system health"),
        BotCommand(constants.QUEUES_COMMAND, "Inspect active and queued jobs"),
        BotCommand(constants.FAILED_COMMAND, "Show failed jobs"),
        BotCommand(constants.STATS_COMMAND, "Show operational statistics"),
        BotCommand(constants.RETRY_FAILED_COMMAND, "Retry eligible failed jobs"),
        BotCommand(constants.CLEANUP_TEMP_COMMAND, "Preview temporary file cleanup"),
        BotCommand(constants.SHUTDOWN_COMMAND, "Gracefully shut down the bot"),
        BotCommand(constants.CANCEL_COMMAND, "Cancel the current action or download"),
    )


async def configure_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(bot_commands())


def create_application(settings: Settings) -> Application:
    return ApplicationBuilder().token(settings.telegram_bot_token).build()


def register_handlers(
    application: Application,
    repository: DatabaseRepository,
    download_queue: DownloadQueue | None,
    upload_worker: UploadWorker | None,
    folder_browser: DriveFolderBrowser | None,
    admin_command_service: AdminCommandService,
    admin_user_ids: tuple[int, ...],
    folder_recent_limit: int,
    logger: logging.Logger,
    settings: Settings | None = None,
) -> None:
    if settings is not None:
        application.bot_data[SETTINGS_KEY] = settings
    application.bot_data[REPOSITORY_KEY] = repository
    application.bot_data[DOWNLOAD_QUEUE_KEY] = download_queue
    application.bot_data[UPLOAD_WORKER_KEY] = upload_worker
    application.bot_data[ADMIN_COMMAND_SERVICE_KEY] = admin_command_service
    application.bot_data[FOLDER_BROWSER_KEY] = folder_browser
    application.bot_data[FOLDER_RECENT_LIMIT_KEY] = folder_recent_limit
    application.bot_data[ADMIN_USER_IDS_KEY] = admin_user_ids
    application.bot_data[LOGGER_KEY] = logger
    application.add_handler(CommandHandler(constants.START_COMMAND, start_command))
    application.add_handler(CommandHandler(constants.HELP_COMMAND, help_command))
    application.add_handler(CommandHandler(constants.PING_COMMAND, ping_command))
    application.add_handler(CommandHandler(constants.ID_COMMAND, id_command))
    application.add_handler(CommandHandler(constants.SETTINGS_COMMAND, settings_command))
    application.add_handler(CommandHandler(constants.CANCEL_COMMAND, cancel_command))
    application.add_handler(CommandHandler(constants.STATUS_COMMAND, status_command))
    application.add_handler(CommandHandler(constants.HEALTH_COMMAND, health_command))
    application.add_handler(CommandHandler(constants.QUEUES_COMMAND, queues_command))
    application.add_handler(CommandHandler(constants.FAILED_COMMAND, failed_command))
    application.add_handler(CommandHandler(constants.STATS_COMMAND, stats_command))
    application.add_handler(CommandHandler(constants.RETRY_FAILED_COMMAND, retry_failed_command))
    application.add_handler(CommandHandler(constants.CLEANUP_TEMP_COMMAND, cleanup_temp_command))
    application.add_handler(CommandHandler(constants.SHUTDOWN_COMMAND, shutdown_command))
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
    application.add_handler(
        CallbackQueryHandler(
            handle_folder_callback,
            pattern=f"^{constants.FOLDER_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_error_handler(global_error_handler)


async def handle_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        _get_logger(context).warning("media update missing message or user")
        return

    _get_logger(context).info(
        "incoming media update received",
        extra={
            "event": "incoming_media_update_trace",
            "update_message_id": getattr(getattr(update, "message", None), "message_id", None),
            "effective_message_id": message.message_id,
            "effective_chat_id": getattr(update.effective_chat, "id", None),
            "effective_user_id": user.id,
            "message_chat_id": message.chat_id,
            "has_document": message.document is not None,
            "has_video": message.video is not None,
            "has_audio": message.audio is not None,
            "has_photo": bool(message.photo),
            "has_animation": message.animation is not None,
            "has_voice": message.voice is not None,
        },
    )
    metadata = _extract_file_metadata(message)
    if metadata is None:
        await message.reply_text("This file type is not supported yet.")
        return
    _get_logger(context).info(
        "media metadata message id assigned",
        extra={
            "event": "media_metadata_message_id_assigned",
            "incoming_update_message_id": getattr(
                getattr(update, "message", None), "message_id", None
            ),
            "effective_message_id": message.message_id,
            "metadata_message_id": metadata.message_id,
            "metadata_chat_id": metadata.chat_id,
            "effective_chat_id": getattr(update.effective_chat, "id", None),
            "effective_user_id": user.id,
            "file_type": metadata.file_type.value,
            "original_name": metadata.original_name,
            "size": metadata.size,
        },
    )
    _get_logger(context).info(
        "incoming file download source diagnosed",
        extra=_incoming_file_download_diagnostics(message, metadata),
    )
    _get_logger(context).info(
        "media metadata extracted",
        extra={
            "event": "media_metadata_extracted",
            "bot_api_chat_id": metadata.chat_id,
            "bot_api_message_id": metadata.message_id,
            "forward_origin_chat_id": metadata.forward_origin_chat_id,
            "forward_origin_message_id": metadata.forward_origin_message_id,
            "message_chat_id": getattr(message.chat, "id", None),
            "message_chat_type": getattr(message.chat, "type", None),
            "message_id": message.message_id,
            "message_forward_origin": getattr(message, "forward_origin", None),
            "message_forward_from_chat_id": getattr(
                getattr(message, "forward_from_chat", None), "id", None
            ),
            "message_forward_from_chat_type": getattr(
                getattr(message, "forward_from_chat", None), "type", None
            ),
            "message_sender_chat_id": getattr(getattr(message, "sender_chat", None), "id", None),
            "message_sender_chat_type": getattr(
                getattr(message, "sender_chat", None), "type", None
            ),
            "from_user_id": user.id,
            "file_type": metadata.file_type.value,
            "original_name": metadata.original_name,
            "size": metadata.size,
            "telegram_file_unique_id_present": metadata.telegram_file_unique_id is not None,
        },
    )

    repository = _get_repository(context)
    created_user = repository.create_user(
        telegram_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )
    file_record = repository.create_file_record(user_id=created_user.id, metadata=metadata)
    _get_logger(context).info(
        "media metadata persisted",
        extra={
            "event": "media_metadata_persisted",
            "file_record_id": file_record.id,
            "metadata_message_id": metadata.message_id,
            "persisted_message_id": file_record.message_id,
            "metadata_chat_id": metadata.chat_id,
            "persisted_chat_id": file_record.chat_id,
            "effective_chat_id": getattr(update.effective_chat, "id", None),
            "effective_user_id": user.id,
            "file_type": metadata.file_type.value,
            "original_name": metadata.original_name,
            "size": metadata.size,
        },
    )

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
        repository.mark_file_awaiting_folder(file_record_id)
        await _send_destination_entry_prompt(
            update=update,
            context=context,
            file_record_id=file_record_id,
            text="Original filename kept. Choose the destination folder.",
        )
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


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    if context.user_data.get(PENDING_RENAME_KEY) is not None:
        await _handle_rename_text(update, context)
        return
    if context.user_data.get(PENDING_FOLDER_ID_KEY) is not None:
        await _handle_folder_id_text(update, context)
        return
    if context.user_data.get(PENDING_FOLDER_SEARCH_KEY) is not None:
        await _handle_folder_search_text(update, context)


async def _handle_rename_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    repository.update_file_original_name(file_record_id, new_path.name)
    repository.mark_file_awaiting_folder(file_record_id)
    await message.reply_text(f"Renamed to {new_path.name}.")
    await _send_destination_entry_prompt(
        update=update,
        context=context,
        file_record_id=file_record_id,
        text="Choose the destination folder.",
    )


async def handle_folder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    action, file_record_id, payload = _parse_folder_callback(query.data)
    repository = _get_repository(context)
    folder_browser = _get_folder_browser(context)
    if folder_browser is None:
        await query.edit_message_text(
            "Google Drive is not connected yet. The file is downloaded and renamed, "
            "but I cannot choose a destination folder."
        )
        return

    user_record = _get_current_user_record(update, context)
    if user_record is None:
        await query.edit_message_text("I could not identify your Telegram user. Try again.")
        return

    if context.user_data is not None:
        context.user_data[PENDING_FOLDER_FILE_KEY] = file_record_id

    if action == constants.FOLDER_ACTION_CANCEL:
        _clear_folder_state(context)
        await query.edit_message_text("Folder selection cancelled.")
        return

    if action == constants.FOLDER_ACTION_LAST:
        folder = repository.get_last_folder(user_record.id)
        if folder is None:
            await _send_folder_home(update, context, file_record_id, "No last folder is saved yet.")
            return
        await _select_destination_folder(update, context, file_record_id, folder)
        return

    if action == constants.FOLDER_ACTION_CHOOSE_ANOTHER:
        await _send_folder_home(update, context, file_record_id)
        return

    if action == constants.FOLDER_ACTION_BROWSE:
        if payload == "" or payload == "roots":
            await _send_browse_roots(update, context, file_record_id, folder_browser)
            return
        if payload == "my":
            await _show_folder_page(
                update,
                context,
                file_record_id=file_record_id,
                folder_browser=folder_browser,
                parent_id=MY_DRIVE_ROOT_ID,
                drive_id=None,
                parent_path=MY_DRIVE_NAME,
            )
            return
        if payload.startswith("sd"):
            drive = _shared_drive_from_payload(context, payload)
            if drive is None:
                await query.edit_message_text("That shared drive option expired. Tap Browse again.")
                return
            await _show_folder_page(
                update,
                context,
                file_record_id=file_record_id,
                folder_browser=folder_browser,
                parent_id=drive.id,
                drive_id=drive.id,
                parent_path=drive.name,
            )
            return

    if action == constants.FOLDER_ACTION_SEARCH:
        if context.user_data is not None:
            context.user_data[PENDING_FOLDER_SEARCH_KEY] = file_record_id
        await query.edit_message_text(
            "Send a folder name to search.",
            reply_markup=_cancel_keyboard(file_record_id),
        )
        return

    if action == constants.FOLDER_ACTION_ID:
        if context.user_data is not None:
            context.user_data[PENDING_FOLDER_ID_KEY] = file_record_id
        await query.edit_message_text(
            "Paste the Google Drive folder ID.",
            reply_markup=_cancel_keyboard(file_record_id),
        )
        return

    if action == constants.FOLDER_ACTION_FAVORITES:
        await _send_folder_list(
            update,
            context,
            file_record_id,
            "Favorite folders",
            repository.list_favorite_folders(user_record.id),
        )
        return

    if action == constants.FOLDER_ACTION_RECENT:
        await _send_folder_list(
            update,
            context,
            file_record_id,
            "Recent folders",
            repository.list_recent_folders(user_record.id, _get_folder_recent_limit(context)),
        )
        return

    if action in {constants.FOLDER_ACTION_OPEN, constants.FOLDER_ACTION_SELECT}:
        folder = _folder_from_token(context, payload)
        if folder is None:
            await query.edit_message_text("That folder option expired. Refresh and try again.")
            return
        if action == constants.FOLDER_ACTION_SELECT:
            await _select_destination_folder(update, context, file_record_id, folder)
            return
        _push_folder_view(context)
        await _show_folder_page(
            update,
            context,
            file_record_id=file_record_id,
            folder_browser=folder_browser,
            parent_id=folder.id,
            drive_id=folder.drive_id,
            parent_path=folder.path,
        )
        return

    if action == constants.FOLDER_ACTION_BACK:
        previous = _pop_folder_view(context)
        if previous is None:
            await _send_folder_home(update, context, file_record_id)
            return
        await _render_folder_view(update, context, file_record_id, folder_browser, previous)
        return

    if action in {constants.FOLDER_ACTION_REFRESH, constants.FOLDER_ACTION_PAGE}:
        view = _current_folder_view(context)
        if view is None:
            await _send_folder_home(update, context, file_record_id)
            return
        if action == constants.FOLDER_ACTION_PAGE:
            view = {**view, "page_token": view.get("next_page_token")}
        await _render_folder_view(
            update,
            context,
            file_record_id,
            folder_browser,
            view,
            refresh=action == constants.FOLDER_ACTION_REFRESH,
        )
        return

    if action == constants.FOLDER_ACTION_FAVORITE_ADD:
        folder = _folder_from_token(context, payload)
        if folder is not None:
            repository.add_favorite_folder(user_record.id, folder)
        await _refresh_current_folder_view(update, context, file_record_id, folder_browser)
        return

    if action == constants.FOLDER_ACTION_FAVORITE_REMOVE:
        folder = _folder_from_token(context, payload)
        if folder is not None:
            repository.remove_favorite_folder(user_record.id, folder.id)
        await _refresh_current_folder_view(update, context, file_record_id, folder_browser)


async def _handle_folder_id_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    pending_file_id = context.user_data.get(PENDING_FOLDER_ID_KEY)
    if pending_file_id is None:
        return
    file_record_id = int(pending_file_id)
    message = update.effective_message
    folder_browser = _get_folder_browser(context)
    if message is None or message.text is None or folder_browser is None:
        return
    result = folder_browser.validate_folder_id(message.text)
    if not result.is_valid or result.folder is None:
        await message.reply_text(result.error_message or "That folder is not available.")
        return
    context.user_data.pop(PENDING_FOLDER_ID_KEY, None)
    await _select_destination_folder(update, context, file_record_id, result.folder)


async def _handle_folder_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    pending_file_id = context.user_data.get(PENDING_FOLDER_SEARCH_KEY)
    if pending_file_id is None:
        return
    file_record_id = int(pending_file_id)
    message = update.effective_message
    folder_browser = _get_folder_browser(context)
    if message is None or message.text is None or folder_browser is None:
        return
    context.user_data.pop(PENDING_FOLDER_SEARCH_KEY, None)
    context.user_data[FOLDER_SEARCH_QUERY_KEY] = message.text.strip()
    await _show_search_results(update, context, file_record_id, folder_browser, message.text)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger = _get_logger(context)
    logger.error(
        "telegram update failed",
        extra={"event": "telegram_update_error"},
        exc_info=context.error,
    )


async def _send_destination_entry_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    text: str,
) -> None:
    if _get_folder_browser(context) is None:
        await _reply_or_edit(
            update,
            "Google Drive is not connected yet. The file is downloaded and renamed, "
            "but I cannot choose a destination folder.",
            None,
        )
        return
    if context.user_data is not None:
        context.user_data[PENDING_FOLDER_FILE_KEY] = file_record_id
    repository = _get_repository(context)
    user_record = _get_current_user_record(update, context)
    last_folder = repository.get_last_folder(user_record.id) if user_record else None
    recent_folders = (
        repository.list_recent_folders(
            user_record.id,
            max(_get_folder_recent_limit(context), DESTINATION_PROMPT_RECENT_LIMIT),
        )
        if user_record
        else []
    )
    if last_folder is None and not recent_folders:
        await _send_folder_home(update, context, file_record_id, text)
        return
    rows: list[list[InlineKeyboardButton]] = []
    if last_folder is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    f"📂 Last Folder: {_shorten(last_folder.name)}",
                    callback_data=_folder_callback(
                        constants.FOLDER_ACTION_LAST,
                        file_record_id,
                    ),
                )
            ]
        )
    recent_prompt_limit = DESTINATION_PROMPT_RECENT_LIMIT - (1 if last_folder is not None else 0)
    for folder in _unique_recent_prompt_folders(
        recent_folders,
        last_folder,
        max_count=recent_prompt_limit,
    ):
        token = _remember_folder_token(context, folder)
        rows.append(
            [
                InlineKeyboardButton(
                    f"🕒 {_shorten(folder.name)}",
                    callback_data=_folder_callback(
                        constants.FOLDER_ACTION_SELECT,
                        file_record_id,
                        token,
                    ),
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "📂 Choose Another",
                    callback_data=_folder_callback(
                        constants.FOLDER_ACTION_CHOOSE_ANOTHER,
                        file_record_id,
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=_folder_callback(constants.FOLDER_ACTION_CANCEL, file_record_id),
                )
            ],
        ]
    )
    keyboard = InlineKeyboardMarkup(rows)
    await _reply_or_edit(update, text, keyboard)


async def _send_folder_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    text: str = "Choose a destination folder.",
) -> None:
    _clear_folder_navigation(context)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Browse",
                    callback_data=_folder_callback(constants.FOLDER_ACTION_BROWSE, file_record_id),
                ),
                InlineKeyboardButton(
                    "Search",
                    callback_data=_folder_callback(constants.FOLDER_ACTION_SEARCH, file_record_id),
                ),
            ],
            [
                InlineKeyboardButton(
                    "Favorites",
                    callback_data=_folder_callback(
                        constants.FOLDER_ACTION_FAVORITES,
                        file_record_id,
                    ),
                ),
                InlineKeyboardButton(
                    "Recent",
                    callback_data=_folder_callback(constants.FOLDER_ACTION_RECENT, file_record_id),
                ),
            ],
            [
                InlineKeyboardButton(
                    "Folder ID",
                    callback_data=_folder_callback(constants.FOLDER_ACTION_ID, file_record_id),
                )
            ],
            [
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=_folder_callback(constants.FOLDER_ACTION_CANCEL, file_record_id),
                )
            ],
        ]
    )
    await _reply_or_edit(update, text, keyboard)


async def _send_browse_roots(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    folder_browser: DriveFolderBrowser,
) -> None:
    try:
        drives = folder_browser.list_shared_drives()
    except HttpError as exc:
        _get_logger(context).warning(
            "failed to list shared drives",
            extra={
                "event": "folder_browse_shared_drives_failed",
                "file_record_id": file_record_id,
                "error": str(exc),
            },
        )
        drives = ()
    if context.user_data is not None:
        context.user_data[FOLDER_SHARED_DRIVES_KEY] = drives
    rows = [
        [
            InlineKeyboardButton(
                MY_DRIVE_NAME,
                callback_data=_folder_callback(
                    constants.FOLDER_ACTION_BROWSE, file_record_id, "my"
                ),
            )
        ]
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                drive.name,
                callback_data=_folder_callback(
                    constants.FOLDER_ACTION_BROWSE,
                    file_record_id,
                    f"sd{index}",
                ),
            )
        ]
        for index, drive in enumerate(drives)
    )
    rows.append(
        [
            InlineKeyboardButton(
                "Back",
                callback_data=_folder_callback(constants.FOLDER_ACTION_BACK, file_record_id),
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=_folder_callback(constants.FOLDER_ACTION_CANCEL, file_record_id),
            ),
        ]
    )
    await _reply_or_edit(update, "Browse folders.", InlineKeyboardMarkup(rows))


async def _show_folder_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    folder_browser: DriveFolderBrowser,
    parent_id: str,
    drive_id: str | None,
    parent_path: str,
    page_token: str | None = None,
    refresh: bool = False,
) -> None:
    try:
        page = folder_browser.list_folders(
            parent_id=parent_id,
            drive_id=drive_id,
            page_token=page_token,
            parent_path=parent_path,
            refresh=refresh,
        )
    except HttpError as exc:
        _get_logger(context).warning(
            "failed to list drive folders",
            extra={
                "event": "folder_browse_list_failed",
                "file_record_id": file_record_id,
                "parent_id": parent_id,
                "drive_id": drive_id,
                "error": str(exc),
            },
        )
        await _reply_or_edit(
            update,
            _folder_list_error_message(exc),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Back",
                            callback_data=_folder_callback(
                                constants.FOLDER_ACTION_BACK,
                                file_record_id,
                            ),
                        ),
                        InlineKeyboardButton(
                            "Cancel",
                            callback_data=_folder_callback(
                                constants.FOLDER_ACTION_CANCEL,
                                file_record_id,
                            ),
                        ),
                    ]
                ]
            ),
        )
        return
    view: dict[str, object] = {
        "mode": "browse",
        "parent_id": parent_id,
        "drive_id": drive_id,
        "parent_path": parent_path,
        "page_token": page_token,
        "next_page_token": page.next_page_token,
    }
    _set_current_folder_view(context, view)
    await _send_folder_page(update, context, file_record_id, page)


async def _show_search_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    folder_browser: DriveFolderBrowser,
    query_text: str,
    page_token: str | None = None,
    refresh: bool = False,
) -> None:
    try:
        page = folder_browser.search_folders(query_text, page_token=page_token, refresh=refresh)
    except HttpError as exc:
        _get_logger(context).warning(
            "failed to search drive folders",
            extra={
                "event": "folder_search_failed",
                "file_record_id": file_record_id,
                "query": query_text,
                "error": str(exc),
            },
        )
        await _reply_or_edit(
            update,
            _folder_list_error_message(exc),
            _cancel_keyboard(file_record_id),
        )
        return
    view: dict[str, object] = {
        "mode": "search",
        "query": query_text,
        "page_token": page_token,
        "next_page_token": page.next_page_token,
    }
    _set_current_folder_view(context, view)
    await _send_folder_page(update, context, file_record_id, page, title=f"Search: {query_text}")


async def _send_folder_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    page: FolderPage,
    title: str | None = None,
) -> None:
    repository = _get_repository(context)
    user_record = _get_current_user_record(update, context)
    rows: list[list[InlineKeyboardButton]] = []
    for folder in page.folders:
        token = _remember_folder_token(context, folder)
        rows.append(
            [
                InlineKeyboardButton(
                    f"📁 {_shorten(folder.name)}",
                    callback_data=_folder_callback(
                        constants.FOLDER_ACTION_OPEN,
                        file_record_id,
                        token,
                    ),
                ),
                InlineKeyboardButton(
                    "Select",
                    callback_data=_folder_callback(
                        constants.FOLDER_ACTION_SELECT,
                        file_record_id,
                        token,
                    ),
                ),
            ]
        )

    current_folder = Folder(
        id=page.parent_id,
        name=page.parent_path.rsplit("/", maxsplit=1)[-1],
        parent_id=None,
        drive_id=page.drive_id,
        path=page.parent_path,
        is_shared=page.drive_id is not None,
        created_at=None,
    )
    current_token = _remember_folder_token(context, current_folder)
    rows.append(
        [
            InlineKeyboardButton(
                "Select this folder",
                callback_data=_folder_callback(
                    constants.FOLDER_ACTION_SELECT,
                    file_record_id,
                    current_token,
                ),
            )
        ]
    )
    if user_record is not None:
        favorite_action = (
            constants.FOLDER_ACTION_FAVORITE_REMOVE
            if repository.is_favorite_folder(user_record.id, current_folder.id)
            else constants.FOLDER_ACTION_FAVORITE_ADD
        )
        favorite_text = "Remove favorite" if favorite_action.endswith("remove") else "Add favorite"
        rows.append(
            [
                InlineKeyboardButton(
                    favorite_text,
                    callback_data=_folder_callback(favorite_action, file_record_id, current_token),
                )
            ]
        )
    if page.next_page_token:
        rows.append(
            [
                InlineKeyboardButton(
                    "Next",
                    callback_data=_folder_callback(constants.FOLDER_ACTION_PAGE, file_record_id),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "Back",
                callback_data=_folder_callback(constants.FOLDER_ACTION_BACK, file_record_id),
            ),
            InlineKeyboardButton(
                "Refresh",
                callback_data=_folder_callback(constants.FOLDER_ACTION_REFRESH, file_record_id),
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=_folder_callback(constants.FOLDER_ACTION_CANCEL, file_record_id),
            ),
        ]
    )
    folder_count = len(page.folders)
    text = title or page.parent_path
    text = f"{text}\n\n{folder_count} folder{'s' if folder_count != 1 else ''}."
    await _reply_or_edit(update, text, InlineKeyboardMarkup(rows))


async def _send_folder_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    title: str,
    folders: list[Folder],
) -> None:
    rows = [
        [
            InlineKeyboardButton(
                f"📂 {_shorten(folder.name)}",
                callback_data=_folder_callback(
                    constants.FOLDER_ACTION_SELECT,
                    file_record_id,
                    _remember_folder_token(context, folder),
                ),
            )
        ]
        for folder in folders
    ]
    rows.append(
        [
            InlineKeyboardButton(
                "Back",
                callback_data=_folder_callback(constants.FOLDER_ACTION_BACK, file_record_id),
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=_folder_callback(constants.FOLDER_ACTION_CANCEL, file_record_id),
            ),
        ]
    )
    text = f"{title}\n\n"
    text += "Tap a folder to select it." if folders else "No folders saved here yet."
    await _reply_or_edit(update, text, InlineKeyboardMarkup(rows))


async def _select_destination_folder(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    folder: Folder,
) -> None:
    folder_browser = _get_folder_browser(context)
    if folder_browser is None:
        await _reply_or_edit(update, "Google Drive is not connected yet.", None)
        return
    result = folder_browser.validate_folder_id(folder.id)
    if not result.is_valid or result.folder is None:
        await _reply_or_edit(update, result.error_message or "That folder is not available.", None)
        return
    selected = replace(result.folder, path=folder.path)
    repository = _get_repository(context)
    user_record = _get_current_user_record(update, context)
    destination_record = repository.set_file_destination_folder(file_record_id, selected)
    ready_record = repository.mark_file_ready_for_upload(file_record_id)
    upload_worker = _get_upload_worker(context)
    _get_logger(context).info(
        "destination folder selected and upload queued",
        extra={
            "event": "destination_folder_selected",
            "file_record_id": file_record_id,
            "destination_folder_id": selected.id,
            "destination_folder_path": selected.path,
            "destination_drive_id": selected.drive_id,
            "destination_is_shared": selected.is_shared,
            "destination_status": destination_record.status if destination_record else None,
            "ready_status": ready_record.status if ready_record else None,
            "pending_uploads": repository.count_pending_uploads(),
            "upload_worker_configured": upload_worker is not None,
        },
    )
    if upload_worker is not None:
        upload_worker.start()
    if user_record is not None:
        repository.set_last_folder(user_record.id, selected)
        repository.record_recent_folder(
            user_record.id,
            selected,
            _get_folder_recent_limit(context),
        )
    _clear_folder_state(context)
    await _reply_or_edit(
        update,
        f"Destination selected: {selected.path}\nReady for upload.",
        None,
    )


async def _render_folder_view(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    folder_browser: DriveFolderBrowser,
    view: dict[str, object],
    refresh: bool = False,
) -> None:
    if view.get("mode") == "search":
        await _show_search_results(
            update,
            context,
            file_record_id,
            folder_browser,
            str(view.get("query") or ""),
            page_token=cast(str | None, view.get("page_token")),
            refresh=refresh,
        )
        return
    await _show_folder_page(
        update,
        context,
        file_record_id,
        folder_browser,
        parent_id=str(view["parent_id"]),
        drive_id=cast(str | None, view.get("drive_id")),
        parent_path=str(view["parent_path"]),
        page_token=cast(str | None, view.get("page_token")),
        refresh=refresh,
    )


async def _refresh_current_folder_view(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_record_id: int,
    folder_browser: DriveFolderBrowser,
) -> None:
    view = _current_folder_view(context)
    if view is None:
        await _send_folder_home(update, context, file_record_id)
        return
    await _render_folder_view(update, context, file_record_id, folder_browser, view, refresh=True)


def _format_help_message(*, is_admin: bool) -> TelegramMessage:
    icons = Icons()
    style = _utility_style(icons)
    rows: list[tuple[str, str, object]] = [
        (icons.file, "Send files", "Download, rename, choose folder, upload"),
        (icons.status, "/status", "Show current pipeline state"),
        (icons.worker, "/cancel", "Cancel the current action"),
        (icons.info, "/id", "Show Telegram user and chat IDs"),
        (icons.healthy, "/ping", "Check bot responsiveness"),
    ]
    if is_admin:
        rows.extend(
            (
                (icons.folder, "/health", "Check subsystem health"),
                (icons.queue, "/queues", "Inspect queued and active jobs"),
                (icons.failed, "/failed", "Show failed jobs"),
                (icons.progress, "/stats", "Show operational statistics"),
                (icons.retrying, "/retry_failed", "Retry eligible failures"),
                (icons.warning, "/cleanup_temp", "Preview temp cleanup"),
                (icons.warning, "/shutdown", "Gracefully stop the bot"),
            )
        )
    return status_card("Help", rows, style=style)


def _format_ping_message() -> TelegramMessage:
    icons = Icons()
    return status_card(
        "Bot Online",
        (
            (icons.healthy, "Status", "Responsive"),
            (icons.info, "Mode", "Polling"),
        ),
        style=_utility_style(icons),
    )


def _format_id_message(*, user_id: int | None, chat_id: int) -> TelegramMessage:
    icons = Icons()
    return status_card(
        "Telegram IDs",
        (
            (icons.info, "User ID", user_id if user_id is not None else "Unknown"),
            (icons.queue, "Chat ID", chat_id),
        ),
        style=_utility_style(icons),
    )


def _format_settings_message(settings: Settings) -> TelegramMessage:
    icons = Icons()
    return status_card(
        "Runtime Settings",
        (
            (icons.worker, "Environment", settings.app_env.title()),
            (icons.info, "Log level", settings.log_level),
            (icons.healthy, "Admins", len(settings.telegram_admin_user_ids)),
            (
                icons.download,
                "Pyrogram",
                "Configured" if settings.pyrogram_api_id is not None else "Not Configured",
            ),
            (
                icons.download,
                "Pyrogram concurrency",
                settings.pyrogram_max_concurrent_transmissions,
            ),
            (icons.folder, "Google scopes", len(settings.google_scopes)),
            (
                icons.folder,
                "Google auto auth",
                "Enabled" if settings.google_auto_auth else "Disabled",
            ),
            (icons.queue, "Folder page size", settings.folder_browser_page_size),
            (icons.queue, "Folder cache TTL", f"{settings.folder_browser_cache_ttl_seconds}s"),
            (icons.folder, "Recent folders", settings.folder_recent_limit),
            (icons.storage, "Downloads dir", _safe_path(settings.downloads_dir)),
            (icons.storage, "Temp dir", _safe_path(settings.temp_dir)),
        ),
        style=_utility_style(icons),
    )


def _utility_style(icons: Icons) -> UIStyle:
    return UIStyle(
        separator=STATUS_MOBILE_SEPARATOR,
        footer=f"{icons.updated} Updated just now",
        icons=icons,
    )


def _safe_path(path: Path) -> str:
    return path.name or str(path)


def _format_system_status(
    download_snapshot: DownloadQueueSnapshot | None,
    upload_snapshot: UploadWorkerSnapshot | None,
    display_mode: StatusDisplayMode = "mobile",
) -> TelegramMessage:
    icons = Icons()
    style = UIStyle(
        separator=_status_separator(display_mode),
        footer=f"{icons.updated} Updated just now",
        icons=icons,
    )
    completed_jobs = (download_snapshot.completed_since_startup if download_snapshot else 0) + (
        upload_snapshot.completed_since_startup if upload_snapshot else 0
    )
    failed_jobs = (download_snapshot.failed_since_startup if download_snapshot else 0) + (
        upload_snapshot.failed_since_startup if upload_snapshot else 0
    )
    queue_length = download_snapshot.queue_length if download_snapshot else 0
    upload_worker_state = _format_upload_worker_state(upload_snapshot, icons)
    return status_card(
        "System Status",
        (
            (icons.download, "Current download", _format_current_download(download_snapshot)),
            (icons.upload, "Current upload", _format_current_upload(upload_snapshot)),
            (icons.queue, "Queue", _format_queue_length(queue_length)),
            (icons.completed, "Completed jobs since startup", completed_jobs),
            (icons.failed, "Failed jobs", failed_jobs),
            (icons.worker, "Upload worker", upload_worker_state),
        ),
        style=style,
    )


def _format_current_download(snapshot: DownloadQueueSnapshot | None) -> str:
    if snapshot is None or snapshot.current_download is None:
        return "None"
    current = snapshot.current_download
    progress = (
        f"{current.progress_percent}%"
        if current.progress_percent is not None
        else "Progress Unknown"
    )
    speed = (
        f", {format_bytes(int(current.speed_bytes_per_second))}/s"
        if current.speed_bytes_per_second
        else ""
    )
    return f"{current.filename} ({progress}{speed})"


def _format_current_upload(snapshot: UploadWorkerSnapshot | None) -> str:
    if snapshot is None or snapshot.current_upload is None:
        return "None"
    current = snapshot.current_upload
    progress = (
        f"{current.progress_percent}%"
        if current.progress_percent is not None
        else "Progress Unavailable"
    )
    return f"{current.filename} ({progress}, {current.status.title()})"


def _format_queue_length(queue_length: int) -> str:
    if queue_length == 0:
        return "Empty"
    suffix = "job" if queue_length == 1 else "jobs"
    return f"{queue_length} {suffix}"


def _status_display_mode(context: ContextTypes.DEFAULT_TYPE) -> StatusDisplayMode:
    args = getattr(context, "args", None)
    if args and str(args[0]).casefold() == "desktop":
        return "desktop"
    return "mobile"


def _status_separator(display_mode: StatusDisplayMode) -> str:
    if display_mode == "desktop":
        return STATUS_DESKTOP_SEPARATOR
    return STATUS_MOBILE_SEPARATOR


def _format_upload_worker_state(snapshot: UploadWorkerSnapshot | None, icons: Icons) -> str:
    if snapshot is not None and snapshot.current_upload is not None:
        status = snapshot.current_upload.status.lower()
        if "retry" in status:
            return f"{icons.retrying} Retrying"
    if snapshot is not None and snapshot.is_busy:
        return f"{icons.active} Uploading"
    if snapshot is not None and snapshot.failed_since_startup > 0:
        return f"{icons.failed} Error"
    return f"{icons.healthy} Idle"


def _get_repository(context: ContextTypes.DEFAULT_TYPE) -> DatabaseRepository:
    return cast(DatabaseRepository, context.application.bot_data[REPOSITORY_KEY])


def _get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return cast(Settings, context.application.bot_data[SETTINGS_KEY])


def _get_download_queue(context: ContextTypes.DEFAULT_TYPE) -> DownloadQueue | None:
    return cast(DownloadQueue | None, context.application.bot_data[DOWNLOAD_QUEUE_KEY])


def _get_upload_worker(context: ContextTypes.DEFAULT_TYPE) -> UploadWorker | None:
    return cast(UploadWorker | None, context.application.bot_data[UPLOAD_WORKER_KEY])


def _get_admin_command_service(context: ContextTypes.DEFAULT_TYPE) -> AdminCommandService:
    return cast(
        AdminCommandService,
        context.application.bot_data[ADMIN_COMMAND_SERVICE_KEY],
    )


def _cleanup_temp_confirmed(context: ContextTypes.DEFAULT_TYPE) -> bool:
    args = getattr(context, "args", None) or ()
    return any(str(arg).casefold() == "--confirm" for arg in args)


def _admin_cancel_requested(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(getattr(context, "args", None) or ())


def _cancel_command_arguments(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, str | None] | None:
    args = [str(arg) for arg in (getattr(context, "args", None) or ())]
    if not args:
        return None
    try:
        job_id = int(args[0])
    except ValueError:
        return None
    job_type = args[1].casefold() if len(args) > 1 else None
    if job_type not in {None, "download", "upload"}:
        return None
    return job_id, job_type


def _get_folder_browser(context: ContextTypes.DEFAULT_TYPE) -> DriveFolderBrowser | None:
    return cast(DriveFolderBrowser | None, context.application.bot_data[FOLDER_BROWSER_KEY])


def _get_folder_recent_limit(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.application.bot_data[FOLDER_RECENT_LIMIT_KEY])


def _is_admin_user(context: ContextTypes.DEFAULT_TYPE, telegram_user_id: int) -> bool:
    admin_user_ids = cast(tuple[int, ...], context.application.bot_data.get(ADMIN_USER_IDS_KEY, ()))
    return telegram_user_id in admin_user_ids


def _get_logger(context: ContextTypes.DEFAULT_TYPE) -> logging.Logger:
    return cast(logging.Logger, context.application.bot_data[LOGGER_KEY])


def _get_current_user_record(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> UserRecord | None:
    user = update.effective_user
    if user is None:
        return None
    repository = _get_repository(context)
    return repository.create_user(
        telegram_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )


async def _reply_or_edit(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
        return
    message = update.effective_message
    if message is not None:
        await message.reply_text(text=text, reply_markup=reply_markup)


def _cancel_keyboard(file_record_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=_folder_callback(constants.FOLDER_ACTION_CANCEL, file_record_id),
                )
            ]
        ]
    )


def _folder_callback(action: str, file_record_id: int, payload: str = "") -> str:
    return f"{constants.FOLDER_CALLBACK_PREFIX}:{action}:{file_record_id}:{payload}"


def _parse_folder_callback(data: str) -> tuple[str, int, str]:
    prefix, action, file_record_id, payload = data.split(":", maxsplit=3)
    if prefix != constants.FOLDER_CALLBACK_PREFIX:
        raise ValueError(f"Unsupported callback prefix: {prefix}")
    return action, int(file_record_id), payload


def _remember_folder_token(context: ContextTypes.DEFAULT_TYPE, folder: Folder) -> str:
    if context.user_data is None:
        return folder.id
    token_map = cast(dict[str, Folder], context.user_data.setdefault(FOLDER_TOKEN_KEY, {}))
    for token, cached_folder in token_map.items():
        if cached_folder.id == folder.id and cached_folder.path == folder.path:
            return token
    counter = int(context.user_data.get(FOLDER_TOKEN_COUNTER_KEY, 0)) + 1
    context.user_data[FOLDER_TOKEN_COUNTER_KEY] = counter
    token = f"f{counter}"
    token_map[token] = folder
    return token


def _folder_from_token(context: ContextTypes.DEFAULT_TYPE, token: str) -> Folder | None:
    if context.user_data is None:
        return None
    token_map = cast(dict[str, Folder], context.user_data.get(FOLDER_TOKEN_KEY, {}))
    return token_map.get(token)


def _unique_recent_prompt_folders(
    recent_folders: list[Folder],
    last_folder: Folder | None,
    max_count: int = DESTINATION_PROMPT_RECENT_LIMIT,
) -> tuple[Folder, ...]:
    folders: list[Folder] = []
    seen_ids: set[str] = {last_folder.id} if last_folder is not None else set()
    if max_count <= 0:
        return ()
    for folder in recent_folders:
        if folder.id in seen_ids:
            continue
        seen_ids.add(folder.id)
        folders.append(folder)
        if len(folders) >= max_count:
            break
    return tuple(folders)


def _set_current_folder_view(
    context: ContextTypes.DEFAULT_TYPE,
    view: dict[str, object],
) -> None:
    if context.user_data is not None:
        context.user_data[FOLDER_VIEW_KEY] = view


def _current_folder_view(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object] | None:
    if context.user_data is None:
        return None
    return cast(dict[str, object] | None, context.user_data.get(FOLDER_VIEW_KEY))


def _push_folder_view(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    view = _current_folder_view(context)
    if view is None:
        return
    stack = cast(list[dict[str, object]], context.user_data.setdefault(FOLDER_BACK_STACK_KEY, []))
    stack.append(view)


def _pop_folder_view(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object] | None:
    if context.user_data is None:
        return None
    stack = cast(list[dict[str, object]], context.user_data.setdefault(FOLDER_BACK_STACK_KEY, []))
    if not stack:
        return None
    return stack.pop()


def _clear_folder_navigation(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    context.user_data.pop(FOLDER_VIEW_KEY, None)
    context.user_data.pop(FOLDER_BACK_STACK_KEY, None)


def _clear_folder_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    for key in (
        PENDING_FOLDER_FILE_KEY,
        PENDING_FOLDER_ID_KEY,
        PENDING_FOLDER_SEARCH_KEY,
        FOLDER_VIEW_KEY,
        FOLDER_BACK_STACK_KEY,
        FOLDER_SEARCH_QUERY_KEY,
        FOLDER_SHARED_DRIVES_KEY,
        FOLDER_TOKEN_KEY,
        FOLDER_TOKEN_COUNTER_KEY,
    ):
        context.user_data.pop(key, None)


def _shared_drive_from_payload(
    context: ContextTypes.DEFAULT_TYPE,
    payload: str,
) -> SharedDrive | None:
    if context.user_data is None:
        return None
    try:
        index = int(payload.removeprefix("sd"))
    except ValueError:
        return None
    drives = context.user_data.get(FOLDER_SHARED_DRIVES_KEY, ())
    if not isinstance(drives, tuple) or index < 0 or index >= len(drives):
        return None
    return cast(SharedDrive, drives[index])


def _shorten(value: str, limit: int = 34) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def _folder_list_error_message(exc: HttpError) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 403:
        return (
            "I cannot browse Google Drive with the current authorization. "
            "Re-authorize Google Drive with the full Drive scope and try again."
        )
    if status == 404:
        return "I could not find that Drive location. Go back and choose another folder."
    return "I could not load folders from Google Drive right now. Try again later."


def _extract_file_metadata(message: Message) -> FileMetadata | None:
    file_type: TelegramFileType
    telegram_file_id: str
    telegram_file_unique_id: str | None
    original_name: str | None = None
    mime_type: str | None = None
    size: int | None = None

    if message.document is not None:
        file_type = TelegramFileType.DOCUMENT
        telegram_file_id = message.document.file_id
        telegram_file_unique_id = getattr(message.document, "file_unique_id", None)
        original_name = message.document.file_name
        mime_type = message.document.mime_type
        size = message.document.file_size
    elif message.video is not None:
        file_type = TelegramFileType.VIDEO
        telegram_file_id = message.video.file_id
        telegram_file_unique_id = getattr(message.video, "file_unique_id", None)
        original_name = message.video.file_name
        mime_type = message.video.mime_type
        size = message.video.file_size
    elif message.audio is not None:
        file_type = TelegramFileType.AUDIO
        telegram_file_id = message.audio.file_id
        telegram_file_unique_id = getattr(message.audio, "file_unique_id", None)
        original_name = message.audio.file_name
        mime_type = message.audio.mime_type
        size = message.audio.file_size
    elif message.photo:
        file_type = TelegramFileType.PHOTO
        photo = message.photo[-1]
        telegram_file_id = photo.file_id
        telegram_file_unique_id = getattr(photo, "file_unique_id", None)
        original_name = f"photo-{message.message_id}.jpg"
        mime_type = "image/jpeg"
        size = photo.file_size
    elif message.animation is not None:
        file_type = TelegramFileType.ANIMATION
        telegram_file_id = message.animation.file_id
        telegram_file_unique_id = getattr(message.animation, "file_unique_id", None)
        original_name = message.animation.file_name
        mime_type = message.animation.mime_type
        size = message.animation.file_size
    elif message.voice is not None:
        file_type = TelegramFileType.VOICE
        telegram_file_id = message.voice.file_id
        telegram_file_unique_id = getattr(message.voice, "file_unique_id", None)
        original_name = f"voice-{message.message_id}.ogg"
        mime_type = message.voice.mime_type
        size = message.voice.file_size
    else:
        return None

    extension = _extension_from_metadata(original_name, mime_type)
    forward_origin_chat_id, forward_origin_message_id = _forward_origin_channel_ids(message)
    return FileMetadata(
        telegram_file_id=telegram_file_id,
        message_id=message.message_id,
        chat_id=message.chat_id,
        forward_origin_chat_id=forward_origin_chat_id,
        forward_origin_message_id=forward_origin_message_id,
        original_name=original_name,
        mime_type=mime_type,
        size=size,
        extension=extension,
        file_type=file_type,
        created_at=utc_now_iso(),
        telegram_file_unique_id=telegram_file_unique_id,
    )


def _forward_origin_channel_ids(message: Message) -> tuple[int | None, int | None]:
    forward_origin = getattr(message, "forward_origin", None)
    origin_type = getattr(forward_origin, "type", None)
    origin_type_value = getattr(origin_type, "value", origin_type)
    origin_type_name = getattr(origin_type, "name", "")
    if str(origin_type_value).lower() != "channel" and str(origin_type_name).lower() != "channel":
        return None, None
    chat = getattr(forward_origin, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(forward_origin, "message_id", None)
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None, None
    return chat_id, message_id


def _incoming_file_download_diagnostics(
    message: Message,
    metadata: FileMetadata,
) -> dict[str, object]:
    is_forwarded = _message_is_forwarded(message)
    has_forward_origin = getattr(message, "forward_origin", None) is not None
    forward_origin_type = _forward_origin_type(message)
    download_source = _detected_download_source(metadata)
    fallback_reason = _bot_api_fallback_reason(
        is_forwarded=is_forwarded,
        has_forward_origin=has_forward_origin,
        forward_origin_type=forward_origin_type,
        metadata=metadata,
    )
    return {
        "event": "incoming_file_download_source_diagnostic",
        "is_forwarded": is_forwarded,
        "has_forward_origin": has_forward_origin,
        "forward_origin_type": forward_origin_type,
        "detected_download_source": download_source,
        "bot_api_fallback_reason": fallback_reason,
        "bot_api_chat_id": metadata.chat_id,
        "bot_api_message_id": metadata.message_id,
        "forward_origin_chat_id": metadata.forward_origin_chat_id,
        "forward_origin_message_id": metadata.forward_origin_message_id,
        "forward_from_chat_id": getattr(getattr(message, "forward_from_chat", None), "id", None),
        "forward_sender_name_present": getattr(message, "forward_sender_name", None) is not None,
        "sender_chat_id": getattr(getattr(message, "sender_chat", None), "id", None),
        "file_type": metadata.file_type.value,
        "original_name": metadata.original_name,
        "size": metadata.size,
        "telegram_file_unique_id_present": metadata.telegram_file_unique_id is not None,
    }


def _message_is_forwarded(message: Message) -> bool:
    return any(
        getattr(message, attribute, None) is not None
        for attribute in (
            "forward_origin",
            "forward_date",
            "forward_from",
            "forward_from_chat",
            "forward_sender_name",
        )
    )


def _forward_origin_type(message: Message) -> str | None:
    forward_origin = getattr(message, "forward_origin", None)
    if forward_origin is None:
        return None
    origin_type = getattr(forward_origin, "type", None)
    origin_type_value = getattr(origin_type, "value", origin_type)
    if origin_type_value is None:
        return type(forward_origin).__name__
    return str(origin_type_value).lower()


def _detected_download_source(metadata: FileMetadata) -> str:
    if (
        metadata.forward_origin_chat_id is not None
        and metadata.forward_origin_message_id is not None
    ):
        return "pyrogram_forward_origin"
    return "bot_api_file_id"


def _bot_api_fallback_reason(
    *,
    is_forwarded: bool,
    has_forward_origin: bool,
    forward_origin_type: str | None,
    metadata: FileMetadata,
) -> str | None:
    if _detected_download_source(metadata) != "bot_api_file_id":
        return None
    if not is_forwarded:
        return "message_is_not_forwarded"
    if not has_forward_origin:
        return "forwarded_message_has_no_forward_origin"
    if forward_origin_type != "channel":
        return f"unsupported_forward_origin_type:{forward_origin_type or 'unknown'}"
    return "channel_forward_origin_missing_chat_or_message_id"


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
