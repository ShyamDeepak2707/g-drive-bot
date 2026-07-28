from __future__ import annotations

from app.download_queue import (
    CurrentDownloadSnapshot,
    DownloadQueueSnapshot,
)
from app.telegram_bot import _format_system_status, _status_display_mode
from app.upload_worker import CurrentUploadSnapshot, UploadWorkerSnapshot


def test_format_system_status_shows_complete_snapshot() -> None:
    message = _format_system_status(
        download_snapshot=DownloadQueueSnapshot(
            current_download=CurrentDownloadSnapshot(
                file_record_id=1,
                filename="movie.mkv",
                progress_percent=42,
                speed_bytes_per_second=2_097_152,
            ),
            queue_length=3,
            completed_since_startup=4,
            failed_since_startup=1,
        ),
        upload_snapshot=UploadWorkerSnapshot(
            current_upload=CurrentUploadSnapshot(
                file_record_id=2,
                filename="archive.zip",
                progress_percent=None,
                status="uploading",
            ),
            completed_since_startup=5,
            failed_since_startup=2,
            is_busy=True,
        ),
    )
    text = message.text

    assert message.parse_mode == "HTML"
    assert "📊 <b>System Status</b>" in text
    assert "📊 <b>System Status</b>\n\n━━━━━━━━━━━━━━━━━━━━\n\n\n" in text
    assert "⬇️ <b>Current download</b>: <b>movie.mkv (42%, 2.0 MB/s)</b>" in text
    assert (
        "⬆️ <b>Current upload</b>: "
        "<b>archive.zip (Progress Unavailable, Uploading)</b>"
    ) in text
    assert "📦 <b>Queue</b>: <b>3 jobs</b>" in text
    assert "🏁 <b>Completed jobs since startup</b>: <b>9</b>" in text
    assert "🔴 <b>Failed jobs</b>: <b>3</b>" in text
    assert "⚙️ <b>Upload worker</b>: <b>🔵 Uploading</b>" in text
    assert (
        "⬇️ <b>Current download</b>: <b>movie.mkv (42%, 2.0 MB/s)</b>\n\n"
        "⬆️ <b>Current upload</b>: "
        "<b>archive.zip (Progress Unavailable, Uploading)</b>"
    ) in text
    assert (
        "⚙️ <b>Upload worker</b>: <b>🔵 Uploading</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n🕒 Updated just now"
    ) in text
    assert "🕒 Updated just now" in text
    assert "<i>Updated just now</i>" not in text
    assert "Telegram Drive Manager" not in text


def test_format_system_status_handles_idle_workers() -> None:
    text = _format_system_status(download_snapshot=None, upload_snapshot=None).text

    assert "⬇️ <b>Current download</b>: <b>None</b>" in text
    assert "⬆️ <b>Current upload</b>: <b>None</b>" in text
    assert "📦 <b>Queue</b>: <b>Empty</b>" in text
    assert "🏁 <b>Completed jobs since startup</b>: <b>0</b>" in text
    assert "🔴 <b>Failed jobs</b>: <b>0</b>" in text
    assert "⚙️ <b>Upload worker</b>: <b>🟢 Idle</b>" in text


def test_format_system_status_supports_desktop_divider() -> None:
    text = _format_system_status(
        download_snapshot=None,
        upload_snapshot=None,
        display_mode="desktop",
    ).text

    assert "📊 <b>System Status</b>\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n\n" in text
    assert "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n🕒 Updated just now" in text


def test_status_display_mode_defaults_to_mobile() -> None:
    class Context:
        args: list[str] = []

    assert _status_display_mode(Context()) == "mobile"  # type: ignore[arg-type]


def test_status_display_mode_supports_desktop_argument() -> None:
    class Context:
        args = ["desktop"]

    assert _status_display_mode(Context()) == "desktop"  # type: ignore[arg-type]


def test_format_system_status_shows_upload_worker_error_state() -> None:
    text = _format_system_status(
        download_snapshot=None,
        upload_snapshot=UploadWorkerSnapshot(
            current_upload=None,
            completed_since_startup=0,
            failed_since_startup=1,
            is_busy=False,
        ),
    ).text

    assert "⚙️ <b>Upload worker</b>: <b>🔴 Error</b>" in text


def test_format_system_status_shows_upload_worker_retrying_state() -> None:
    text = _format_system_status(
        download_snapshot=None,
        upload_snapshot=UploadWorkerSnapshot(
            current_upload=CurrentUploadSnapshot(
                file_record_id=2,
                filename="archive.zip",
                progress_percent=None,
                status="retrying",
            ),
            completed_since_startup=0,
            failed_since_startup=0,
            is_busy=True,
        ),
    ).text

    assert "⚙️ <b>Upload worker</b>: <b>🟡 Retrying</b>" in text
