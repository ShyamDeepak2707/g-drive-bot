from __future__ import annotations

import asyncio
import logging

import pytest

from app.startup_recovery import run_startup_recovery
from app.upload_worker import UploadRecoverySummary


class FakeDownloadQueue:
    def __init__(self, recovered: int) -> None:
        self.recovered = recovered
        self.calls = 0

    def recover_interrupted_jobs(self) -> int:
        self.calls += 1
        return self.recovered


class FakeUploadWorker:
    def __init__(self, summary: UploadRecoverySummary) -> None:
        self.summary = summary
        self.calls = 0

    async def recover_interrupted_jobs(self) -> UploadRecoverySummary:
        self.calls += 1
        return self.summary


def test_startup_recovery_logs_no_recovery_needed(caplog: pytest.LogCaptureFixture) -> None:
    download_queue = FakeDownloadQueue(0)
    upload_worker = FakeUploadWorker(UploadRecoverySummary(0, 0))

    async def run() -> None:
        with caplog.at_level(logging.INFO, logger="test.startup_recovery"):
            summary = await run_startup_recovery(
                download_queue=download_queue,  # type: ignore[arg-type]
                upload_worker=upload_worker,  # type: ignore[arg-type]
                logger=logging.getLogger("test.startup_recovery"),
            )
        assert summary.total == 0

    asyncio.run(run())

    assert download_queue.calls == 1
    assert upload_worker.calls == 1
    record = _event(caplog, "startup_recovery_completed")
    assert getattr(record, "recovery_needed", None) is False
    assert "no recovery was necessary" in record.getMessage()


def test_startup_recovery_logs_recovered_counts(caplog: pytest.LogCaptureFixture) -> None:
    download_queue = FakeDownloadQueue(2)
    upload_worker = FakeUploadWorker(UploadRecoverySummary(1, 3))

    async def run() -> None:
        with caplog.at_level(logging.INFO, logger="test.startup_recovery"):
            summary = await run_startup_recovery(
                download_queue=download_queue,  # type: ignore[arg-type]
                upload_worker=upload_worker,  # type: ignore[arg-type]
                logger=logging.getLogger("test.startup_recovery"),
            )
        assert summary.downloads_recovered == 2
        assert summary.uploads_recovered == 1
        assert summary.cleanup_recovered == 3

    asyncio.run(run())

    record = _event(caplog, "startup_recovery_completed")
    assert getattr(record, "recovery_needed", None) is True
    assert getattr(record, "downloads_recovered", None) == 2
    assert getattr(record, "uploads_recovered", None) == 1
    assert getattr(record, "cleanup_recovered", None) == 3


def _event(caplog: pytest.LogCaptureFixture, event: str) -> logging.LogRecord:
    for record in caplog.records:
        if getattr(record, "event", None) == event:
            return record
    raise AssertionError(f"Missing log event: {event}")
