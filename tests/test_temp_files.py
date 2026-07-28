from __future__ import annotations

from pathlib import Path

from app.temp_files import TempFileService


def test_temp_file_service_preview_reports_orphaned_files(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    downloads_dir = tmp_path / "downloads"
    temp_dir.mkdir()
    downloads_dir.mkdir()
    orphan = temp_dir / "orphan.part"
    orphan.write_bytes(b"abc")
    ignored = downloads_dir / "complete.mkv"
    ignored.write_bytes(b"not temporary")
    service = TempFileService(temp_dir=temp_dir, downloads_dir=downloads_dir)

    summary = service.preview_orphaned_temp_files()

    assert summary.confirmed is False
    assert summary.total_files == 1
    assert summary.total_bytes == 3
    assert summary.deleted_files == 0
    assert summary.files[0].path == orphan.resolve()
    assert ignored.exists()
    assert orphan.exists()


def test_temp_file_service_confirm_deletes_only_orphaned_temp_files(
    tmp_path: Path,
) -> None:
    temp_dir = tmp_path / "temp"
    downloads_dir = tmp_path / "downloads"
    temp_dir.mkdir()
    downloads_dir.mkdir()
    orphan = temp_dir / "orphan.part"
    orphan.write_bytes(b"abc")
    protected = downloads_dir / "active.mkv.part"
    protected.write_bytes(b"active")
    service = TempFileService(temp_dir=temp_dir, downloads_dir=downloads_dir)

    summary = service.cleanup_orphaned_temp_files(protected_paths=(protected,))

    assert summary.confirmed is True
    assert summary.total_files == 1
    assert summary.total_bytes == 3
    assert summary.deleted_files == 1
    assert summary.deleted_bytes == 3
    assert not orphan.exists()
    assert protected.exists()


def test_temp_file_service_handles_empty_directories(tmp_path: Path) -> None:
    service = TempFileService(
        temp_dir=tmp_path / "missing-temp",
        downloads_dir=tmp_path / "missing-downloads",
    )

    summary = service.preview_orphaned_temp_files()

    assert summary.total_files == 0
    assert summary.total_bytes == 0
    assert summary.files == ()


def test_temp_file_service_cleanup_is_idempotent(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    downloads_dir = tmp_path / "downloads"
    temp_dir.mkdir()
    downloads_dir.mkdir()
    orphan = temp_dir / "orphan.part.temp"
    orphan.write_bytes(b"abc")
    service = TempFileService(temp_dir=temp_dir, downloads_dir=downloads_dir)

    first = service.cleanup_orphaned_temp_files()
    second = service.cleanup_orphaned_temp_files()

    assert first.deleted_files == 1
    assert first.deleted_bytes == 3
    assert second.total_files == 0
    assert second.deleted_files == 0
    assert not orphan.exists()
