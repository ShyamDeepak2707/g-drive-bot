from __future__ import annotations

import logging
from dataclasses import dataclass

from app.download_queue import DownloadQueue
from app.upload_worker import UploadWorker


@dataclass(frozen=True)
class StartupRecoverySummary:
    downloads_recovered: int
    uploads_recovered: int
    cleanup_recovered: int

    @property
    def total(self) -> int:
        return self.downloads_recovered + self.uploads_recovered + self.cleanup_recovered


async def run_startup_recovery(
    *,
    download_queue: DownloadQueue,
    upload_worker: UploadWorker,
    logger: logging.Logger,
) -> StartupRecoverySummary:
    downloads_recovered = download_queue.recover_interrupted_jobs()
    upload_summary = await upload_worker.recover_interrupted_jobs()
    summary = StartupRecoverySummary(
        downloads_recovered=downloads_recovered,
        uploads_recovered=upload_summary.uploads_recovered,
        cleanup_recovered=upload_summary.cleanup_recovered,
    )

    if summary.total == 0:
        logger.info(
            "startup recovery completed; no recovery was necessary",
            extra={
                "event": "startup_recovery_completed",
                "downloads_recovered": 0,
                "uploads_recovered": 0,
                "cleanup_recovered": 0,
                "recovery_needed": False,
            },
        )
        return summary

    logger.info(
        "startup recovery completed: "
        f"downloads={summary.downloads_recovered}, "
        f"uploads={summary.uploads_recovered}, "
        f"cleanup={summary.cleanup_recovered}",
        extra={
            "event": "startup_recovery_completed",
            "downloads_recovered": summary.downloads_recovered,
            "uploads_recovered": summary.uploads_recovered,
            "cleanup_recovered": summary.cleanup_recovered,
            "recovery_needed": True,
        },
    )
    return summary
