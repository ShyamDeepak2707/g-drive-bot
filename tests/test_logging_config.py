from __future__ import annotations

import logging

from app.logging_config import ConsoleFormatter


def test_console_formatter_includes_safe_diagnostic_fields() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="download attempt failed",
        args=(),
        exc_info=None,
    )
    record.event = "download_failed"
    record.error = "Telegram Bot API download failed"
    record.detected_download_source = "bot_api_file_id"
    record.bot_api_fallback_reason = "metadata_has_no_forward_origin_ids"
    record.telegram_file_id = "secret-file-id"

    formatted = ConsoleFormatter().format(record)

    assert "event=download_failed" in formatted
    assert "error=Telegram Bot API download failed" in formatted
    assert "detected_download_source=bot_api_file_id" in formatted
    assert "bot_api_fallback_reason=metadata_has_no_forward_origin_ids" in formatted
    assert "secret-file-id" not in formatted
