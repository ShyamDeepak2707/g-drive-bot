from __future__ import annotations

from typing import cast

from googleapiclient.errors import HttpError

from app.drive.browser import DriveFolderBrowser, TTLCache, extract_google_drive_id


class FakeRequest:
    def __init__(self, response: dict[str, object] | HttpError) -> None:
        self._response = response

    def execute(self) -> dict[str, object]:
        if isinstance(self._response, HttpError):
            raise self._response
        return self._response


class FakeFilesResource:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[str] = []
        self.list_responses: list[dict[str, object]] = []
        self.get_responses: dict[str, dict[str, object] | HttpError] = {}

    def list(self, **kwargs: object) -> FakeRequest:
        self.list_calls.append(kwargs)
        return FakeRequest(self.list_responses.pop(0))

    def get(self, fileId: str, **kwargs: object) -> FakeRequest:
        del kwargs
        self.get_calls.append(fileId)
        return FakeRequest(self.get_responses[fileId])


class FakeDrivesResource:
    def __init__(self) -> None:
        self.responses: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> FakeRequest:
        del kwargs
        return FakeRequest(self.responses.pop(0))


class FakeDriveService:
    def __init__(self) -> None:
        self.files_resource = FakeFilesResource()
        self.drives_resource = FakeDrivesResource()

    def files(self) -> FakeFilesResource:
        return self.files_resource

    def drives(self) -> FakeDrivesResource:
        return self.drives_resource


def test_browser_lists_only_folders_with_pagination_and_cache() -> None:
    service = FakeDriveService()
    service.files_resource.list_responses.append(
        {
            "nextPageToken": "next-token",
            "files": [
                _folder_item("folder-1", "Projects", ["root"]),
                {"id": "file-1", "name": "notes.txt", "mimeType": "text/plain"},
            ],
        }
    )
    browser = DriveFolderBrowser(service, page_size=2, cache_ttl_seconds=60)  # type: ignore[arg-type]

    page = browser.list_folders()
    cached = browser.list_folders()

    assert [folder.name for folder in page.folders] == ["Projects"]
    assert cached == page
    assert page.next_page_token == "next-token"
    assert len(service.files_resource.list_calls) == 1
    query = cast(str, service.files_resource.list_calls[0]["q"])
    assert "mimeType = 'application/vnd.google-apps.folder'" in query


def test_search_is_case_insensitive_partial_paginated_and_cached() -> None:
    service = FakeDriveService()
    service.files_resource.list_responses.append(
        {
            "nextPageToken": "page-2",
            "files": [
                _folder_item("folder-1", "Client Reports", ["root"]),
                _folder_item("folder-2", "Archive", ["root"]),
            ],
        }
    )
    browser = DriveFolderBrowser(service, page_size=10, cache_ttl_seconds=60)  # type: ignore[arg-type]

    page = browser.search_folders("report")
    cached = browser.search_folders("REPORT")

    assert [folder.name for folder in page.folders] == ["Client Reports"]
    assert cached == page
    assert page.next_page_token == "page-2"
    assert len(service.files_resource.list_calls) == 1


def test_validate_folder_id_requires_accessible_writable_folder() -> None:
    service = FakeDriveService()
    service.files_resource.get_responses["folder-1"] = _folder_item(
        "folder-1",
        "Uploads",
        ["root"],
        writable=True,
    )
    service.files_resource.get_responses["folder-2"] = _folder_item(
        "folder-2",
        "Read only",
        ["root"],
        writable=False,
    )
    service.files_resource.get_responses["missing"] = _http_error(404)
    browser = DriveFolderBrowser(service, cache_ttl_seconds=60)  # type: ignore[arg-type]

    ok = browser.validate_folder_id("folder-1")
    readonly = browser.validate_folder_id("folder-2")
    missing = browser.validate_folder_id("missing")

    assert ok.is_valid
    assert ok.folder is not None
    assert ok.folder.name == "Uploads"
    assert not readonly.is_valid
    assert readonly.error_message is not None
    assert "permission" in readonly.error_message
    assert not missing.is_valid
    assert missing.error_message is not None
    assert "find" in missing.error_message


def test_validate_folder_id_accepts_google_drive_folder_links() -> None:
    service = FakeDriveService()
    service.files_resource.get_responses["folder-1"] = _folder_item(
        "folder-1",
        "Uploads",
        ["root"],
        writable=True,
    )
    browser = DriveFolderBrowser(service, cache_ttl_seconds=60)  # type: ignore[arg-type]

    result = browser.validate_folder_id(
        "https://drive.google.com/drive/folders/folder-1?usp=sharing"
    )

    assert result.is_valid
    assert result.folder is not None
    assert result.folder.id == "folder-1"
    assert service.files_resource.get_calls == ["folder-1"]


def test_extract_google_drive_id_supports_common_link_formats() -> None:
    assert extract_google_drive_id("folder-1") == "folder-1"
    assert (
        extract_google_drive_id("https://drive.google.com/drive/folders/folder-1?usp=sharing")
        == "folder-1"
    )
    assert extract_google_drive_id("https://drive.google.com/open?id=folder-2") == "folder-2"
    assert extract_google_drive_id("https://example.com/not-drive") == ""


def test_ttl_cache_can_invalidate_by_prefix() -> None:
    cache: TTLCache[str] = TTLCache(ttl_seconds=60)
    cache.set("list:a", "one")
    cache.set("search:a", "two")

    cache.invalidate("list:")

    assert cache.get("list:a") is None
    assert cache.get("search:a") == "two"


def _folder_item(
    folder_id: str,
    name: str,
    parents: list[str],
    writable: bool = True,
) -> dict[str, object]:
    return {
        "id": folder_id,
        "name": name,
        "parents": parents,
        "mimeType": "application/vnd.google-apps.folder",
        "createdTime": "2026-07-24T00:00:00.000Z",
        "capabilities": {"canAddChildren": writable},
    }


def _http_error(status: int) -> HttpError:
    class Response(dict[str, str]):
        reason = "error"

        def __init__(self, status_code: int) -> None:
            super().__init__({"status": str(status_code), "reason": "error"})
            self.status = status_code

    return HttpError(Response(status), b"{}")
