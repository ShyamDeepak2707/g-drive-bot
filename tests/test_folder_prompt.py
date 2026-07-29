from __future__ import annotations

from app.models import Folder
from app.telegram_bot import DESTINATION_PROMPT_RECENT_LIMIT, _unique_recent_prompt_folders


def test_unique_recent_prompt_folders_excludes_last_and_limits_to_three() -> None:
    last = _folder("last", "Last")
    recent = [
        last,
        _folder("one", "One"),
        _folder("two", "Two"),
        _folder("three", "Three"),
        _folder("four", "Four"),
    ]

    folders = _unique_recent_prompt_folders(recent, last)

    assert [folder.id for folder in folders] == ["one", "two", "three"]
    assert len(folders) == DESTINATION_PROMPT_RECENT_LIMIT


def test_unique_recent_prompt_folders_deduplicates_recent_entries() -> None:
    recent = [
        _folder("one", "One"),
        _folder("one", "One duplicate"),
        _folder("two", "Two"),
    ]

    folders = _unique_recent_prompt_folders(recent, last_folder=None)

    assert [folder.id for folder in folders] == ["one", "two"]


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
