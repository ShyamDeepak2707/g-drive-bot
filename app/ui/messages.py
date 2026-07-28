from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from html import escape
from typing import Final


@dataclass(frozen=True)
class TelegramMessage:
    text: str
    parse_mode: str = "HTML"
    disable_web_page_preview: bool = True


@dataclass(frozen=True)
class Icons:
    success: str = "✅"
    error: str = "❌"
    warning: str = "⚠️"
    info: str = "ℹ️"
    status: str = "📊"
    progress: str = "⏳"
    download: str = "⬇️"
    upload: str = "⬆️"
    file: str = "📄"
    folder: str = "📂"
    storage: str = "💾"
    speed: str = "🚀"
    eta: str = "⏱"
    queue: str = "📦"
    completed: str = "🏁"
    failed: str = "🔴"
    worker: str = "⚙️"
    healthy: str = "🟢"
    active: str = "🔵"
    retrying: str = "🟡"
    updated: str = "🕒"


@dataclass(frozen=True)
class UIStyle:
    separator: str = "━━━━━━━━━━━━"
    progress_separator: str = "━━━━━━━━━━━━━━━━━━━━"
    footer: str = "Telegram Drive Manager"
    icons: Icons = Icons()


DEFAULT_STYLE = UIStyle()
_FOOTER_UNSET: Final = object()


def _resolve_footer(footer: object, style: UIStyle) -> str | None:
    if footer is _FOOTER_UNSET:
        return style.footer
    if footer is None:
        return None
    return str(footer)


def success_card(
    title: str,
    body: str | None = None,
    fields: Mapping[str, object] | None = None,
    *,
    footer: object = _FOOTER_UNSET,
    style: UIStyle = DEFAULT_STYLE,
) -> TelegramMessage:
    return _card(
        style.icons.success,
        title,
        body,
        fields,
        footer=_resolve_footer(footer, style),
        style=style,
    )


def error_card(
    title: str,
    body: str | None = None,
    fields: Mapping[str, object] | None = None,
    *,
    footer: object = _FOOTER_UNSET,
    style: UIStyle = DEFAULT_STYLE,
) -> TelegramMessage:
    return _card(
        style.icons.error,
        title,
        body,
        fields,
        footer=_resolve_footer(footer, style),
        style=style,
    )


def warning_card(
    title: str,
    body: str | None = None,
    fields: Mapping[str, object] | None = None,
    *,
    footer: object = _FOOTER_UNSET,
    style: UIStyle = DEFAULT_STYLE,
) -> TelegramMessage:
    return _card(
        style.icons.warning,
        title,
        body,
        fields,
        footer=_resolve_footer(footer, style),
        style=style,
    )


def info_card(
    title: str,
    body: str | None = None,
    fields: Mapping[str, object] | None = None,
    *,
    footer: object = _FOOTER_UNSET,
    style: UIStyle = DEFAULT_STYLE,
) -> TelegramMessage:
    return _card(
        style.icons.info,
        title,
        body,
        fields,
        footer=_resolve_footer(footer, style),
        style=style,
    )


def progress_card(
    title: str,
    *,
    filename: str | None = None,
    current: int | None = None,
    total: int | None = None,
    percent: int | None = None,
    transferred_size: object | None = None,
    total_size: object | None = None,
    speed: str | None = None,
    eta: str | None = None,
    status_text: str | None = None,
    bar_segments: int = 20,
    max_filename_length: int = 36,
    fields: Mapping[str, object] | None = None,
    footer: object = _FOOTER_UNSET,
    style: UIStyle = DEFAULT_STYLE,
) -> TelegramMessage:
    if filename is not None:
        return _modern_progress_card(
            title=title,
            filename=filename,
            percent=percent if percent is not None else _percent(current, total),
            transferred_size=transferred_size if transferred_size is not None else current,
            total_size=total_size if total_size is not None else total,
            speed=speed,
            eta=eta,
            status_text=status_text,
            bar_segments=bar_segments,
            max_filename_length=max_filename_length,
            footer=_resolve_progress_footer(footer, style),
            style=style,
        )

    progress_fields: dict[str, object] = {}
    resolved_percent = percent if percent is not None else _percent(current, total)
    if resolved_percent is not None:
        progress_fields["Progress"] = f"{resolved_percent}%"
    if current is not None and total is not None:
        progress_fields["Transferred"] = f"{current} / {total}"
    elif current is not None:
        progress_fields["Transferred"] = current
    if speed is not None:
        progress_fields["Speed"] = speed
    if eta is not None:
        progress_fields["ETA"] = eta
    if fields:
        progress_fields.update(fields)
    return _card(
        style.icons.progress,
        title,
        fields=progress_fields,
        footer=_resolve_footer(footer, style),
        style=style,
    )


def _modern_progress_card(
    *,
    title: str,
    filename: str,
    percent: int | None,
    transferred_size: object | None,
    total_size: object | None,
    speed: str | None,
    eta: str | None,
    status_text: str | None,
    bar_segments: int,
    max_filename_length: int,
    footer: str | None,
    style: UIStyle,
) -> TelegramMessage:
    resolved_percent = _clamp_percent(percent)
    separator = style.progress_separator
    lines = [
        f"<b>{_html(title)}</b>",
        "",
        separator,
        "",
        f"{style.icons.file} <b>{_html(_truncate_filename(filename, max_filename_length))}</b>",
    ]
    total_line = _format_total_size(total_size)
    if total_line is not None:
        lines.append(f"{style.icons.storage} {_html(total_line)}")
    lines.extend(
        ("", f"{_progress_bar(resolved_percent, bar_segments)} <b>{resolved_percent}%</b>")
    )
    transferred = _format_transfer(transferred_size, total_size)
    if transferred is not None:
        lines.append(f"{style.icons.storage} {_html(transferred)}")
    if speed:
        lines.append(f"{style.icons.speed} {_html(speed)}")
    if eta:
        lines.append(f"{style.icons.eta} {_html(eta)} remaining")
    if status_text:
        lines.append(_html(status_text))
    if footer:
        lines.extend(("", separator, _html(footer)))
    return TelegramMessage(text="\n".join(lines))


def status_card(
    title: str,
    rows: Iterable[tuple[str, str, object]],
    *,
    footer: object = _FOOTER_UNSET,
    style: UIStyle = DEFAULT_STYLE,
) -> TelegramMessage:
    row_values = tuple(rows)
    lines = [
        f"{style.icons.status} <b>{_html(title)}</b>",
        "",
        style.separator,
        "",
        "",
    ]
    for index, (icon, label, value) in enumerate(row_values):
        if index > 0:
            lines.append("")
        lines.append(_format_status_row(icon, label, value))
    resolved_footer = _resolve_footer(footer, style)
    if resolved_footer:
        lines.extend(("", style.separator, _html(resolved_footer)))
    return TelegramMessage(text="\n".join(lines))


def format_field(label: str, value: object, icon: str | None = None) -> str:
    prefix = f"{icon} " if icon else ""
    return f"{prefix}<b>{_html(label)}</b>: {_html(_stringify(value))}"


def _card(
    icon: str,
    title: str,
    body: str | None = None,
    fields: Mapping[str, object] | None = None,
    *,
    footer: str | None,
    style: UIStyle,
) -> TelegramMessage:
    lines = [
        f"{icon} <b>{_html(title)}</b>",
        style.separator,
    ]
    if body:
        lines.extend(("", _html(body)))
    field_lines = _format_fields(fields or {})
    if field_lines:
        lines.extend(("", *field_lines))
    if footer:
        lines.extend(("", f"<i>{_html(footer)}</i>"))
    return TelegramMessage(text="\n".join(lines))


def _format_fields(fields: Mapping[str, object]) -> list[str]:
    return [format_field(label, value) for label, value in fields.items()]


def _format_status_row(icon: str, label: str, value: object) -> str:
    return f"{icon} <b>{_html(label)}</b>: <b>{_html(_stringify(value))}</b>"


def _resolve_progress_footer(footer: object, style: UIStyle) -> str | None:
    if footer is _FOOTER_UNSET:
        return f"{style.icons.updated} Updated just now"
    if footer is None:
        return None
    return str(footer)


def _progress_bar(percent: int, segments: int = 20) -> str:
    safe_segments = max(1, segments)
    filled_segments = round((percent / 100) * safe_segments)
    return f"{'█' * filled_segments}{'─' * (safe_segments - filled_segments)}"


def _clamp_percent(percent: int | None) -> int:
    if percent is None:
        return 0
    return min(100, max(0, int(percent)))


def _format_transfer(transferred_size: object | None, total_size: object | None) -> str | None:
    if transferred_size is None and total_size is None:
        return None
    if transferred_size is None:
        return f"? / {_stringify(total_size)}"
    if total_size is None:
        return _stringify(transferred_size)
    return f"{_stringify(transferred_size)} / {_stringify(total_size)}"


def _format_total_size(total_size: object | None) -> str | None:
    if total_size is None:
        return None
    return f"Total: {_stringify(total_size)}"


def _truncate_filename(filename: str, max_length: int) -> str:
    if len(filename) <= max_length:
        return filename
    marker = "..."
    if max_length <= len(marker):
        return filename[:max_length]
    name_part, separator, extension = filename.rpartition(".")
    suffix = f"{separator}{extension}" if separator and name_part else ""
    if suffix and len(suffix) <= max_length - len(marker) - 8:
        extension_marker = ".."
        stem_length = max_length - len(extension_marker) - len(suffix)
        return f"{filename[:stem_length]}{extension_marker}{suffix}"
    return f"{filename[: max_length - len(marker)]}{marker}"


def _html(value: str) -> str:
    return escape(value, quote=False)


def _stringify(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Iterable) and not isinstance(value, bytes | bytearray | Mapping):
        return ", ".join(_stringify(item) for item in value)
    return str(value)


def _percent(current: int | None, total: int | None) -> int | None:
    if current is None or not total:
        return None
    return min(100, max(0, int((current / total) * 100)))
