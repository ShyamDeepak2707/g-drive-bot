from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, cast

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

from app import constants
from app.models import Folder

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
MY_DRIVE_ROOT_ID = "root"
MY_DRIVE_NAME = "My Drive"


@dataclass(frozen=True)
class FolderPage:
    folders: tuple[Folder, ...]
    parent_id: str
    parent_path: str
    drive_id: str | None
    next_page_token: str | None


@dataclass(frozen=True)
class SharedDrive:
    id: str
    name: str


@dataclass(frozen=True)
class FolderValidationResult:
    is_valid: bool
    folder: Folder | None = None
    error_message: str | None = None


@dataclass
class _CacheEntry[T]:
    value: T
    expires_at: float


class TTLCache[T]:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._items: dict[str, _CacheEntry[T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: T) -> None:
        self._items[key] = _CacheEntry(
            value=value,
            expires_at=time.monotonic() + self._ttl_seconds,
        )

    def invalidate(self, prefix: str | None = None) -> None:
        if prefix is None:
            self._items.clear()
            return
        for key in tuple(self._items):
            if key.startswith(prefix):
                self._items.pop(key, None)


class DriveFolderBrowser:
    def __init__(
        self,
        service: Resource,
        page_size: int = constants.FOLDER_BROWSER_PAGE_SIZE,
        cache_ttl_seconds: int = constants.FOLDER_BROWSER_CACHE_TTL_SECONDS,
    ) -> None:
        self._service = service
        self._page_size = page_size
        self._listing_cache: TTLCache[FolderPage] = TTLCache(cache_ttl_seconds)
        self._search_cache: TTLCache[FolderPage] = TTLCache(cache_ttl_seconds)
        self._folder_cache: TTLCache[Folder] = TTLCache(cache_ttl_seconds)
        self._shared_drives_cache: TTLCache[tuple[SharedDrive, ...]] = TTLCache(cache_ttl_seconds)

    def list_shared_drives(self, page_token: str | None = None) -> tuple[SharedDrive, ...]:
        cache_key = f"shared_drives:{page_token or ''}"
        cached = self._shared_drives_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            response = (
                self._service.drives()
                .list(
                    pageSize=self._page_size,
                    pageToken=page_token,
                    fields="drives(id,name),nextPageToken",
                )
                .execute()
            )
        except (AttributeError, HttpError):
            self._shared_drives_cache.set(cache_key, ())
            return ()

        drives = tuple(
            SharedDrive(id=str(item["id"]), name=str(item["name"]))
            for item in response.get("drives", [])
        )
        self._shared_drives_cache.set(cache_key, drives)
        return drives

    def list_folders(
        self,
        parent_id: str = MY_DRIVE_ROOT_ID,
        drive_id: str | None = None,
        page_token: str | None = None,
        parent_path: str | None = None,
        refresh: bool = False,
    ) -> FolderPage:
        parent_path = parent_path or (MY_DRIVE_NAME if parent_id == MY_DRIVE_ROOT_ID else parent_id)
        cache_key = self._list_cache_key(parent_id, drive_id, page_token, parent_path)
        if refresh:
            self._listing_cache.invalidate(cache_key)
        else:
            cached = self._listing_cache.get(cache_key)
            if cached is not None:
                return cached

        query = (
            f"'{_escape_query_value(parent_id)}' in parents "
            f"and mimeType = '{FOLDER_MIME_TYPE}' and trashed = false"
        )
        response = self._files_list(query=query, drive_id=drive_id, page_token=page_token)
        files = cast(list[dict[str, Any]], response.get("files", []))
        folders = tuple(
            self._folder_from_file(
                item,
                parent_id=parent_id,
                parent_path=parent_path,
                fallback_drive_id=drive_id,
            )
            for item in files
            if item.get("mimeType") == FOLDER_MIME_TYPE
        )
        page = FolderPage(
            folders=folders,
            parent_id=parent_id,
            parent_path=parent_path,
            drive_id=drive_id,
            next_page_token=cast(str | None, response.get("nextPageToken")),
        )
        self._listing_cache.set(cache_key, page)
        for folder in folders:
            self._folder_cache.set(folder.id, folder)
        return page

    def search_folders(
        self,
        query_text: str,
        drive_id: str | None = None,
        page_token: str | None = None,
        refresh: bool = False,
    ) -> FolderPage:
        term = query_text.strip()
        if term == "":
            return FolderPage((), MY_DRIVE_ROOT_ID, MY_DRIVE_NAME, drive_id, None)

        cache_key = f"search:{drive_id or ''}:{page_token or ''}:{term.casefold()}"
        if refresh:
            self._search_cache.invalidate(cache_key)
        else:
            cached = self._search_cache.get(cache_key)
            if cached is not None:
                return cached

        query = (
            f"mimeType = '{FOLDER_MIME_TYPE}' and trashed = false "
            f"and name contains '{_escape_query_value(term)}'"
        )
        response = self._files_list(query=query, drive_id=drive_id, page_token=page_token)
        files = cast(list[dict[str, Any]], response.get("files", []))
        lowered = term.casefold()
        folders = tuple(
            folder
            for folder in (
                self._folder_from_file(item, fallback_drive_id=drive_id)
                for item in files
                if item.get("mimeType") == FOLDER_MIME_TYPE
            )
            if lowered in folder.name.casefold()
        )
        page = FolderPage(
            folders=folders,
            parent_id=MY_DRIVE_ROOT_ID,
            parent_path=MY_DRIVE_NAME,
            drive_id=drive_id,
            next_page_token=cast(str | None, response.get("nextPageToken")),
        )
        self._search_cache.set(cache_key, page)
        for folder in folders:
            self._folder_cache.set(folder.id, folder)
        return page

    def get_folder(self, folder_id: str, path_hint: str | None = None) -> Folder | None:
        cached = self._folder_cache.get(folder_id)
        if cached is not None:
            return cached
        try:
            item = self._files_get(folder_id)
        except HttpError:
            return None
        if item.get("mimeType") != FOLDER_MIME_TYPE or item.get("trashed") is True:
            return None
        folder = self._folder_from_file(item, path_hint=path_hint)
        self._folder_cache.set(folder.id, folder)
        return folder

    def validate_folder_id(self, folder_id: str) -> FolderValidationResult:
        normalized = folder_id.strip()
        if normalized == "":
            return FolderValidationResult(False, error_message="Folder ID cannot be empty.")

        try:
            item = self._files_get(normalized)
        except HttpError as exc:
            return FolderValidationResult(False, error_message=_friendly_http_error(exc))

        if item.get("trashed") is True:
            return FolderValidationResult(
                False,
                error_message="That folder is in trash. Choose a different destination.",
            )
        if item.get("mimeType") != FOLDER_MIME_TYPE:
            return FolderValidationResult(
                False,
                error_message="That ID is not a Google Drive folder.",
            )
        capabilities = cast(dict[str, object], item.get("capabilities", {}))
        if capabilities.get("canAddChildren") is not True:
            return FolderValidationResult(
                False,
                error_message="I can access that folder, but I do not have permission to add files.",
            )

        folder = self._folder_from_file(item)
        self._folder_cache.set(folder.id, folder)
        return FolderValidationResult(True, folder=folder)

    def _files_list(
        self,
        query: str,
        drive_id: str | None,
        page_token: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, object] = {
            "q": query,
            "pageSize": self._page_size,
            "pageToken": page_token,
            "orderBy": "folder,name_natural",
            "spaces": "drive",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "fields": (
                "nextPageToken,files("
                "id,name,parents,driveId,createdTime,mimeType,trashed,capabilities/canAddChildren"
                ")"
            ),
        }
        if drive_id is None:
            kwargs["corpora"] = "user"
        else:
            kwargs["corpora"] = "drive"
            kwargs["driveId"] = drive_id
        return cast(dict[str, Any], self._service.files().list(**kwargs).execute())

    def _files_get(self, folder_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            (
                self._service.files()
                .get(
                    fileId=folder_id,
                    fields=(
                        "id,name,parents,driveId,createdTime,mimeType,trashed,"
                        "capabilities/canAddChildren"
                    ),
                    supportsAllDrives=True,
                )
                .execute()
            ),
        )

    def _folder_from_file(
        self,
        item: dict[str, Any],
        parent_id: str | None = None,
        parent_path: str | None = None,
        fallback_drive_id: str | None = None,
        path_hint: str | None = None,
    ) -> Folder:
        folder_id = str(item["id"])
        name = str(item["name"])
        parents = item.get("parents") or []
        resolved_parent_id = parent_id
        if resolved_parent_id is None and isinstance(parents, list) and parents:
            resolved_parent_id = str(parents[0])
        drive_id = item.get("driveId")
        resolved_drive_id = str(drive_id) if drive_id else fallback_drive_id
        path = path_hint or name
        if parent_path:
            path = f"{parent_path}/{name}"
        folder = Folder(
            id=folder_id,
            name=name,
            parent_id=resolved_parent_id,
            drive_id=resolved_drive_id,
            path=path,
            is_shared=resolved_drive_id is not None,
            created_at=str(item["createdTime"]) if item.get("createdTime") else None,
        )
        self._folder_cache.set(folder.id, folder)
        return folder

    @staticmethod
    def _list_cache_key(
        parent_id: str,
        drive_id: str | None,
        page_token: str | None,
        parent_path: str,
    ) -> str:
        return f"list:{drive_id or ''}:{parent_id}:{page_token or ''}:{parent_path}"


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _friendly_http_error(exc: HttpError) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 404:
        return "I could not find that folder. Check the ID and try again."
    if status == 403:
        return "I cannot access that folder. Share it with this Google account and try again."
    return "I could not validate that folder right now. Try again later."
