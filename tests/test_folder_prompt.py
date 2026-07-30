from __future__ import annotations

from app.models import Folder
from app.telegram_bot import _unique_recent_prompt_folders


def test_unique_recent_prompt_folders_excludes_last_and_uses_available_slots() -> None:
    last = _folder("last", "Last")
    recent = [
        last,
        _folder("one", "One"),
        _folder("two", "Two"),
        _folder("three", "Three"),
        _folder("four", "Four"),
    ]

    folders = _unique_recent_prompt_folders(recent, last, max_count=2)

    assert [folder.id for folder in folders] == ["one", "two"]


def test_unique_recent_prompt_folders_deduplicates_recent_entries() -> None:
    recent = [
        _folder("one", "One"),
        _folder("one", "One duplicate"),
        _folder("two", "Two"),
    ]

    folders = _unique_recent_prompt_folders(recent, last_folder=None)

    assert [folder.id for folder in folders] == ["one", "two"]


def test_unique_recent_prompt_folders_returns_none_when_no_slots_available() -> None:
    folders = _unique_recent_prompt_folders(
        [_folder("one", "One")],
        last_folder=_folder("last", "Last"),
        max_count=0,
    )

    assert folders == ()


def _folder(folder_id: str, name: str) -> Folder:
    return Folder(
        id=folder_id,
        name=name,
        parent_id="root",
        drive_id=None,
        path=f"My Drive/{name}",
        is_shared=False,
        created_at=None,
    )
