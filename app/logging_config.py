from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import override

_RESERVED_LOG_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__) | frozenset(
    {"asctime", "message"}
)
_CONSOLE_EXTRA_KEYS = (
    "download_source",
    "detected_download_source",
    "bot_api_fallback_reason",
    "error",
    "file_record_id",
    "attempt",
    "next_attempt",
    "delay_seconds",
    "forward_origin_type",
    "has_forward_origin",
    "forward_origin_chat_id",
    "forward_origin_message_id",
    "metadata_chat_id",
    "metadata_message_id",
    "file_type",
    "original_name",
    "size",
    "file_name",
    "file_size",
    "expected_file_name",
    "resolved_file_name",
    "expected_size",
    "resolved_size",
    "telegram_file_unique_id_present",
    "expected_unique_id_present",
    "resolved_unique_id_present",
    "resolved_file_unique_id_present",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = str(event)
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_KEYS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)


class ConsoleFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        parts = [f"event={event}"] if event else []
        for key in _CONSOLE_EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                parts.append(f"{key}={value}")
        suffix = f" {' '.join(parts)}" if parts else ""
        return (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"{record.levelname:<8} {record.name} {record.getMessage()}{suffix}"
        )


def configure_logging(level: str, log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ConsoleFormatter())

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())

    logging.basicConfig(
        level=numeric_level,
        handlers=[console_handler, file_handler],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
