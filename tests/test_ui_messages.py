from __future__ import annotations

from app.ui import (
    Icons,
    UIStyle,
    error_card,
    format_field,
    info_card,
    progress_card,
    status_card,
    success_card,
    warning_card,
)


def test_cards_use_html_and_escape_dynamic_text() -> None:
    message = success_card(
        "Saved <file>",
        "Uploaded A & B",
        {"Folder": "Movies > 2026"},
    )

    assert message.parse_mode == "HTML"
    assert "✅ <b>Saved &lt;file&gt;</b>" in message.text
    assert "Uploaded A &amp; B" in message.text
    assert "<b>Folder</b>: Movies &gt; 2026" in message.text
    assert "<i>Telegram Drive Manager</i>" in message.text


def test_card_helpers_share_style_and_icons() -> None:
    style = UIStyle(separator="---", footer="Custom Footer", icons=Icons(info="i"))

    assert error_card("Problem", style=style).text.splitlines()[1] == "---"
    assert warning_card("Careful", style=style).text.splitlines()[1] == "---"
    assert info_card("Details", style=style).text.startswith("i <b>Details</b>")
    assert "Custom Footer" in info_card("Details", style=style).text


def test_progress_card_formats_progress_fields() -> None:
    message = progress_card(
        "Downloading",
        current=25,
        total=100,
        speed="1.0 MB/s",
        eta="10s",
        fields={"File": "movie.mkv"},
    )

    assert "<b>Progress</b>: 25%" in message.text
    assert "<b>Transferred</b>: 25 / 100" in message.text
    assert "<b>Speed</b>: 1.0 MB/s" in message.text
    assert "<b>ETA</b>: 10s" in message.text
    assert "<b>File</b>: movie.mkv" in message.text


def test_modern_progress_card_formats_zero_percent() -> None:
    message = progress_card(
        "📥 Downloading",
        filename="ubuntu.iso",
        percent=0,
        transferred_size="0 MB",
        total_size="1.36 GB",
        speed="2.4 MB/s",
        eta="4m 12s",
        status_text="Starting",
    )

    assert message.parse_mode == "HTML"
    assert "<b>📥 Downloading</b>" in message.text
    assert "━━━━━━━━━━━━━━━━━━━━" in message.text
    assert "📄 <b>ubuntu.iso</b>" in message.text
    assert "💾 Total: 1.36 GB" in message.text
    assert "──────────────────── <b>0%</b>" in message.text
    assert "💾 0 MB / 1.36 GB" in message.text
    assert "🚀 2.4 MB/s" in message.text
    assert "⏱ 4m 12s remaining" in message.text
    assert "Starting" in message.text
    assert "🕒 Updated just now" in message.text


def test_modern_progress_card_formats_half_percent() -> None:
    message = progress_card(
        "📥 Downloading",
        filename="ubuntu.iso",
        percent=50,
        transferred_size="720 MB",
        total_size="1.36 GB",
    )

    assert "██████████────────── <b>50%</b>" in message.text


def test_modern_progress_card_formats_complete_percent() -> None:
    message = progress_card(
        "⬆️ Uploading",
        filename="archive.zip",
        percent=100,
        transferred_size="1.36 GB",
        total_size="1.36 GB",
    )

    assert "████████████████████ <b>100%</b>" in message.text


def test_modern_progress_card_omits_missing_speed() -> None:
    message = progress_card(
        "📥 Downloading",
        filename="ubuntu.iso",
        percent=25,
        transferred_size="340 MB",
        total_size="1.36 GB",
        eta="6m",
    )

    assert "🚀" not in message.text
    assert "⏱ 6m remaining" in message.text


def test_modern_progress_card_omits_missing_eta() -> None:
    message = progress_card(
        "📥 Downloading",
        filename="ubuntu.iso",
        percent=25,
        transferred_size="340 MB",
        total_size="1.36 GB",
        speed="2.4 MB/s",
    )

    assert "🚀 2.4 MB/s" in message.text
    assert "⏱" not in message.text


def test_modern_progress_card_truncates_long_filenames_cleanly() -> None:
    filename = "very-long-linux-distribution-installer-release-candidate-video.mkv"
    message = progress_card(
        "📥 Downloading",
        filename=filename,
        percent=10,
        transferred_size="100 MB",
        total_size="1 GB",
        max_filename_length=36,
    )

    assert filename not in message.text
    assert "....mkv" not in message.text
    assert "very-long-linux-distribution-i...mkv" in message.text
    assert "📄 <b>very-long-linux-distribution-i...mkv</b>" in message.text


def test_status_card_bolds_labels_and_values() -> None:
    message = status_card(
        "Status",
        (
            ("⬇️", "Short", "none"),
            ("⬆️", "Longer label", "busy"),
        ),
        footer="Updated just now",
    )

    assert message.parse_mode == "HTML"
    assert message.text.startswith("📊 <b>Status</b>\n\n━━━━━━━━━━━━\n\n\n")
    assert "⬇️ <b>Short</b>: <b>none</b>" in message.text
    assert "⬆️ <b>Longer label</b>: <b>busy</b>" in message.text
    assert "⬇️ <b>Short</b>: <b>none</b>\n\n⬆️ <b>Longer label</b>: <b>busy</b>" in message.text
    assert "<code>" not in message.text
    assert "⬆️ <b>Longer label</b>: <b>busy</b>\n\n━━━━━━━━━━━━\nUpdated just now" in message.text
    assert "<i>Updated just now</i>" not in message.text


def test_format_field_supports_icons_and_iterables() -> None:
    assert format_field("Files", ["a.txt", "b.txt"], icon="📋") == ("📋 <b>Files</b>: a.txt, b.txt")
