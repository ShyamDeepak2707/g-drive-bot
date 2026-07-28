from __future__ import annotations

from typing import cast

from telegram import Message

from app.models import FileMetadata, TelegramFileType
from app.telegram_bot import _forward_origin_channel_ids, _incoming_file_download_diagnostics


class FakeOriginType:
    value = "channel"


class FakeChat:
    id = -100123


class FakeForwardOrigin:
    type = FakeOriginType()
    chat = FakeChat()
    message_id = 99


class FakeMessage:
    forward_origin = FakeForwardOrigin()


class FakeNonForwardedMessage:
    forward_origin = None


class FakeHiddenOriginType:
    value = "hidden_user"


class FakeHiddenForwardOrigin:
    type = FakeHiddenOriginType()


class FakeHiddenForwardedMessage:
    forward_origin = FakeHiddenForwardOrigin()
    forward_from_chat = None
    forward_sender_name = "Hidden Sender"
    sender_chat = None


def test_forward_origin_channel_ids_are_extracted() -> None:
    assert _forward_origin_channel_ids(cast(Message, FakeMessage())) == (-100123, 99)


def test_forward_origin_channel_ids_are_optional() -> None:
    assert _forward_origin_channel_ids(cast(Message, FakeNonForwardedMessage())) == (None, None)


def test_incoming_file_diagnostics_explain_hidden_sender_bot_api_fallback() -> None:
    diagnostics = _incoming_file_download_diagnostics(
        cast(Message, FakeHiddenForwardedMessage()),
        _metadata(),
    )

    assert diagnostics["event"] == "incoming_file_download_source_diagnostic"
    assert diagnostics["is_forwarded"] is True
    assert diagnostics["has_forward_origin"] is True
    assert diagnostics["forward_origin_type"] == "hidden_user"
    assert diagnostics["detected_download_source"] == "bot_api_file_id"
    assert diagnostics["bot_api_fallback_reason"] == ("unsupported_forward_origin_type:hidden_user")


def test_incoming_file_diagnostics_identify_pyrogram_source() -> None:
    diagnostics = _incoming_file_download_diagnostics(
        cast(Message, FakeMessage()),
        _metadata(forward_origin_chat_id=-100123, forward_origin_message_id=99),
    )

    assert diagnostics["is_forwarded"] is True
    assert diagnostics["has_forward_origin"] is True
    assert diagnostics["forward_origin_type"] == "channel"
    assert diagnostics["detected_download_source"] == "pyrogram_forward_origin"
    assert diagnostics["bot_api_fallback_reason"] is None


def _metadata(
    *,
    forward_origin_chat_id: int | None = None,
    forward_origin_message_id: int | None = None,
) -> FileMetadata:
    return FileMetadata(
        telegram_file_id="bot-api-file-id",
        message_id=10,
        chat_id=20,
        forward_origin_chat_id=forward_origin_chat_id,
        forward_origin_message_id=forward_origin_message_id,
        original_name="example.txt",
        mime_type="text/plain",
        size=5,
        extension=".txt",
        file_type=TelegramFileType.DOCUMENT,
        created_at="2026-07-24T00:00:00+00:00",
    )
