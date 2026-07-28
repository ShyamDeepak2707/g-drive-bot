from __future__ import annotations

import logging
import time
from collections.abc import Callable

from app import constants
from app.admin_service import (
    AdminService,
    CancelJobSummary,
    FailedJobSummary,
    QueueInspection,
    RetryFailedSummary,
    RuntimeStatistics,
)
from app.health import HealthService, HealthSnapshot
from app.progress import format_bytes
from app.shutdown_control import ShutdownController, ShutdownRequestResult
from app.temp_files import TempCleanupSummary
from app.ui import Icons, TelegramMessage, UIStyle, error_card, status_card

ADMIN_COMMAND_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━"


class AdminCommandService:
    def __init__(
        self,
        *,
        health_service: HealthService,
        admin_service: AdminService,
        shutdown_controller: ShutdownController | None = None,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._health_service = health_service
        self._admin_service = admin_service
        self._shutdown_controller = shutdown_controller
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic or time.perf_counter

    def health(self) -> TelegramMessage:
        return self._execute_admin_command(
            "health",
            lambda: _format_health_snapshot(self._health_service.snapshot()),
        )

    def queues(self) -> TelegramMessage:
        return self._execute_admin_command(
            "queues",
            lambda: _format_queue_inspection(self._admin_service.queue_inspection()),
        )

    def failed(self) -> TelegramMessage:
        return self._execute_admin_command(
            "failed",
            lambda: _format_failed_jobs(self._admin_service.failed_job_summaries()),
        )

    def stats(self) -> TelegramMessage:
        return self._execute_admin_command(
            "stats",
            lambda: _format_runtime_statistics(self._admin_service.runtime_statistics()),
        )

    def retry_failed(self, admin_user_id: int) -> TelegramMessage:
        return self._execute_admin_command(
            "retry_failed",
            lambda: self._retry_failed(admin_user_id),
        )

    def cleanup_temp(self, *, admin_user_id: int, confirm: bool = False) -> TelegramMessage:
        return self._execute_admin_command(
            "cleanup_temp",
            lambda: self._cleanup_temp(admin_user_id=admin_user_id, confirm=confirm),
        )

    def shutdown(self, *, admin_user_id: int) -> TelegramMessage:
        return self._execute_admin_command(
            "shutdown",
            lambda: self._shutdown(admin_user_id=admin_user_id),
        )

    def cancel_job(
        self,
        *,
        admin_user_id: int,
        job_id: int,
        job_type: str | None = None,
    ) -> TelegramMessage:
        return self._execute_admin_command(
            "cancel",
            lambda: self._cancel_job(
                admin_user_id=admin_user_id,
                job_id=job_id,
                job_type=job_type,
            ),
        )

    def _retry_failed(self, admin_user_id: int) -> TelegramMessage:
        summary = self._admin_service.retry_failed_jobs()
        self._logger.info(
            "admin retry failed completed",
            extra={
                "event": "admin_retry_failed",
                "admin_user_id": admin_user_id,
                "eligible": summary.eligible_jobs,
                "requeued": summary.requeued_jobs,
                "skipped": summary.skipped_jobs,
            },
        )
        return _format_retry_failed_summary(summary)

    def _cleanup_temp(self, *, admin_user_id: int, confirm: bool) -> TelegramMessage:
        summary = self._admin_service.cleanup_temp(confirm=confirm)
        self._logger.info(
            "admin cleanup temp completed",
            extra={
                "event": "admin_cleanup_temp",
                "admin_user_id": admin_user_id,
                "confirmed": summary.confirmed,
                "orphaned_files": summary.total_files,
                "orphaned_bytes": summary.total_bytes,
                "deleted_files": summary.deleted_files,
                "deleted_bytes": summary.deleted_bytes,
            },
        )
        return _format_cleanup_temp_summary(summary)

    def _shutdown(self, *, admin_user_id: int) -> TelegramMessage:
        if self._shutdown_controller is None:
            raise RuntimeError("Shutdown controller is not configured.")

        result = self._shutdown_controller.request_shutdown(source="telegram_admin")
        if result.initiated:
            self._logger.info(
                "admin shutdown initiated",
                extra={
                    "event": "admin_shutdown",
                    "admin_user_id": admin_user_id,
                    "shutdown_source": result.source,
                },
            )
        return _format_shutdown_result(result)

    def _cancel_job(
        self,
        *,
        admin_user_id: int,
        job_id: int,
        job_type: str | None,
    ) -> TelegramMessage:
        summary = self._admin_service.cancel_job(job_id=job_id, job_type=job_type)
        if summary.cancelled:
            self._logger.info(
                "admin cancel job completed",
                extra={
                    "event": "admin_cancel_job",
                    "admin_user_id": admin_user_id,
                    "job_id": summary.job_id,
                    "job_type": summary.job_type,
                },
            )
        return _format_cancel_job_summary(summary)

    def _execute_admin_command(
        self,
        command_name: str,
        operation: Callable[[], TelegramMessage],
    ) -> TelegramMessage:
        started_at = self._monotonic()
        self._logger.info(
            "admin command started",
            extra={
                "event": "admin_command_started",
                "admin_command": command_name,
            },
        )
        try:
            message = operation()
        except Exception:
            elapsed_ms = _elapsed_ms(started_at, self._monotonic())
            self._logger.exception(
                "admin command failed",
                extra={
                    "event": "admin_command_failed",
                    "admin_command": command_name,
                    "elapsed_ms": elapsed_ms,
                },
            )
            return _format_admin_command_error(command_name)

        elapsed_ms = _elapsed_ms(started_at, self._monotonic())
        self._logger.info(
            "admin command completed",
            extra={
                "event": "admin_command_completed",
                "admin_command": command_name,
                "elapsed_ms": elapsed_ms,
            },
        )
        return message


def _admin_style() -> UIStyle:
    icons = Icons()
    return UIStyle(
        separator=ADMIN_COMMAND_SEPARATOR,
        footer=f"{icons.updated} Updated just now",
        icons=icons,
    )


def _format_admin_command_error(command_name: str) -> TelegramMessage:
    return error_card(
        "Admin Command Failed",
        "The command failed unexpectedly. Check the logs for details.",
        {"Command": command_name},
        style=_admin_style(),
    )


def _elapsed_ms(started_at: float, finished_at: float) -> int:
    return max(0, round((finished_at - started_at) * 1000))


def _format_health_snapshot(snapshot: HealthSnapshot) -> TelegramMessage:
    icons = Icons()
    style = _admin_style()
    return status_card(
        "System Health",
        (
            (icons.progress, "Uptime", _format_uptime(snapshot.uptime_seconds)),
            (
                icons.storage,
                "Database",
                _format_connected(snapshot.database_connected, "Connected"),
            ),
            (
                icons.info,
                "Telegram Bot",
                _format_connected(snapshot.telegram_bot_connected, "Connected"),
            ),
            (
                icons.download,
                "Pyrogram",
                _format_connected(snapshot.pyrogram_connected, "Connected"),
            ),
            (
                icons.folder,
                "Google Drive",
                _format_connected(snapshot.google_drive_authenticated, "Authenticated"),
            ),
            (
                icons.download,
                "Downloads",
                _format_queue_counts(
                    active=snapshot.queues.active_downloads,
                    pending=snapshot.queues.pending_downloads,
                ),
            ),
            (
                icons.upload,
                "Uploads",
                _format_queue_counts(
                    active=snapshot.queues.active_uploads,
                    pending=snapshot.queues.pending_uploads,
                ),
            ),
            (icons.worker, "Recovery", _format_recovery_summary(snapshot)),
        ),
        style=style,
    )


def _format_queue_inspection(inspection: QueueInspection, active_limit: int = 5) -> TelegramMessage:
    icons = Icons()
    style = _admin_style()
    rows: list[tuple[str, str, object]] = [
        (icons.download, "Pending downloads", _format_queue_count(inspection.pending_downloads)),
    ]
    active_downloads = tuple(item for item in (inspection.active_download,) if item is not None)
    if active_downloads:
        rows.extend(
            _active_job_rows(
                icon=icons.download,
                label="Active downloads",
                jobs=tuple(
                    _format_active_job(
                        job_id=download.file_record_id,
                        filename=download.filename,
                        progress_percent=download.progress_percent,
                        state="Downloading",
                    )
                    for download in active_downloads
                ),
                limit=active_limit,
            )
        )
    else:
        rows.append((icons.download, "Active downloads", "None"))

    rows.append((icons.upload, "Pending uploads", _format_queue_count(inspection.pending_uploads)))
    active_uploads = tuple(item for item in (inspection.active_upload,) if item is not None)
    if active_uploads:
        rows.extend(
            _active_job_rows(
                icon=icons.upload,
                label="Active uploads",
                jobs=tuple(
                    _format_active_job(
                        job_id=upload.file_record_id,
                        filename=upload.filename,
                        progress_percent=upload.progress_percent,
                        state=upload.status.title(),
                    )
                    for upload in active_uploads
                ),
                limit=active_limit,
            )
        )
    else:
        rows.append((icons.upload, "Active uploads", "None"))

    if (
        inspection.pending_downloads == 0
        and inspection.pending_uploads == 0
        and not active_downloads
        and not active_uploads
    ):
        rows.append((icons.healthy, "Work", "No queued or active jobs"))

    return status_card("Queues", rows, style=style)


def _format_failed_jobs(
    summaries: tuple[FailedJobSummary, ...],
    *,
    per_section_limit: int = 5,
) -> TelegramMessage:
    icons = Icons()
    style = _admin_style()
    failed_downloads = tuple(
        summary
        for summary in summaries
        if summary.download_status == constants.DOWNLOAD_STATUS_FAILED
    )
    failed_uploads = tuple(
        summary
        for summary in summaries
        if summary.download_status != constants.DOWNLOAD_STATUS_FAILED
    )
    rows: list[tuple[str, str, object]] = [
        (icons.failed, "Total failed", _format_queue_count(len(summaries))),
    ]
    if not summaries:
        rows.append((icons.healthy, "Failures", "No failed jobs"))
        return status_card("Failed Jobs", rows, style=style)

    rows.append((icons.download, "Failed downloads", _format_queue_count(len(failed_downloads))))
    rows.extend(
        _failed_job_rows(
            icon=icons.download,
            label="Download",
            jobs=failed_downloads,
            limit=per_section_limit,
        )
    )
    rows.append((icons.upload, "Failed uploads", _format_queue_count(len(failed_uploads))))
    rows.extend(
        _failed_job_rows(
            icon=icons.upload,
            label="Upload",
            jobs=failed_uploads,
            limit=per_section_limit,
        )
    )
    return status_card("Failed Jobs", rows, style=style)


def _format_runtime_statistics(stats: RuntimeStatistics) -> TelegramMessage:
    icons = Icons()
    style = _admin_style()
    return status_card(
        "Statistics",
        (
            (icons.progress, "Uptime", _format_uptime(stats.uptime_seconds)),
            (icons.file, "Total files", _format_queue_count(stats.total_tracked_files)),
            (icons.download, "Completed downloads", stats.completed_downloads),
            (icons.upload, "Completed uploads", stats.completed_uploads),
            (
                icons.failed,
                "Failed jobs",
                f"{stats.failed_downloads} Downloads / {stats.failed_uploads} Uploads",
            ),
            (
                icons.retrying,
                "Retry attempts",
                (
                    f"{stats.download_retry_attempts} Downloads / "
                    f"{stats.upload_retry_attempts} Uploads"
                ),
            ),
            (icons.retrying, "Scheduled retries", stats.scheduled_upload_retries),
            (
                icons.queue,
                "Queue",
                f"{stats.pending_downloads} Downloads / {stats.pending_uploads} Uploads",
            ),
            (
                icons.active,
                "Active",
                f"{stats.active_downloads} Downloads / {stats.active_uploads} Uploads",
            ),
        ),
        style=style,
    )


def _format_retry_failed_summary(summary: RetryFailedSummary) -> TelegramMessage:
    icons = Icons()
    style = _admin_style()
    rows: list[tuple[str, str, object]] = [
        (icons.retrying, "Eligible jobs", _format_queue_count(summary.eligible_jobs)),
        (icons.queue, "Requeued jobs", _format_queue_count(summary.requeued_jobs)),
        (icons.warning, "Skipped jobs", _format_queue_count(summary.skipped_jobs)),
    ]
    if summary.eligible_jobs == 0:
        rows.append((icons.healthy, "Result", "No retry-eligible failed jobs"))
    return status_card("Retry Failed Jobs", rows, style=style)


def _format_cleanup_temp_summary(summary: TempCleanupSummary) -> TelegramMessage:
    icons = Icons()
    style = _admin_style()
    mode = "Confirmed" if summary.confirmed else "Dry Run"
    rows: list[tuple[str, str, object]] = [
        (icons.worker, "Mode", mode),
        (icons.file, "Orphaned files", _format_queue_count(summary.total_files)),
        (icons.storage, "Orphaned size", format_bytes(summary.total_bytes)),
    ]
    if summary.confirmed:
        rows.extend(
            (
                (icons.completed, "Deleted files", _format_queue_count(summary.deleted_files)),
                (icons.storage, "Deleted size", format_bytes(summary.deleted_bytes)),
            )
        )
        result = "Cleanup complete"
    elif summary.total_files == 0:
        result = "No orphaned temporary files"
    else:
        result = "Preview only; rerun with --confirm"
    rows.append(
        (
            icons.healthy if summary.total_files == 0 or summary.confirmed else icons.warning,
            "Result",
            result,
        )
    )
    return status_card("Temporary Cleanup", rows, style=style)


def _format_shutdown_result(result: ShutdownRequestResult) -> TelegramMessage:
    icons = Icons()
    style = _admin_style()
    if result.initiated:
        rows: tuple[tuple[str, str, object], ...] = (
            (icons.worker, "Status", "Graceful shutdown started"),
            (icons.info, "Mode", "Graceful"),
        )
    else:
        rows = (
            (icons.warning, "Status", "Shutdown already in progress"),
            (icons.info, "Mode", "Graceful"),
        )
    return status_card("Shutdown", rows, style=style)


def _format_cancel_job_summary(summary: CancelJobSummary) -> TelegramMessage:
    icons = Icons()
    style = UIStyle(
        separator=ADMIN_COMMAND_SEPARATOR,
        footer=f"{icons.updated} Updated just now",
        icons=Icons(status="🛑"),
    )
    rows: list[tuple[str, str, object]] = []
    if summary.filename is not None:
        rows.append((icons.file, "Job", _shorten(summary.filename, 36)))
    else:
        rows.append((icons.file, "Job", f"#{summary.job_id}"))
    rows.append(
        (icons.info, "Status", summary.message if not summary.cancelled else summary.status)
    )
    if summary.job_type is not None:
        rows.append((icons.worker, "Type", summary.job_type.title()))
    return status_card("Cancel Job", rows, style=style)


def _failed_job_rows(
    *,
    icon: str,
    label: str,
    jobs: tuple[FailedJobSummary, ...],
    limit: int,
) -> list[tuple[str, str, object]]:
    visible_jobs = jobs[: max(0, limit)]
    rows: list[tuple[str, str, object]] = [
        (
            icon,
            label,
            _format_failed_job(job),
        )
        for job in visible_jobs
    ]
    remaining = len(jobs) - len(visible_jobs)
    if remaining > 0:
        rows.append((icon, "", f"+{remaining} more"))
    return rows


def _format_failed_job(summary: FailedJobSummary) -> str:
    reason = summary.error_message or "Unknown failure"
    return f"#{summary.file_record_id} {_shorten(summary.filename, 24)} - {_shorten(reason, 38)}"


def _active_job_rows(
    *,
    icon: str,
    label: str,
    jobs: tuple[str, ...],
    limit: int,
) -> list[tuple[str, str, object]]:
    visible_jobs = jobs[: max(0, limit)]
    rows: list[tuple[str, str, object]] = [
        (
            icon,
            label if index == 0 else "",
            job,
        )
        for index, job in enumerate(visible_jobs)
    ]
    remaining = len(jobs) - len(visible_jobs)
    if remaining > 0:
        rows.append((icon, "", f"+{remaining} more"))
    return rows


def _format_active_job(
    *,
    job_id: int,
    filename: str,
    progress_percent: int | None,
    state: str,
) -> str:
    progress = f"{progress_percent}%" if progress_percent is not None else "Progress Unknown"
    return f"#{job_id} {_shorten(filename, 28)} ({progress}, {state})"


def _format_queue_count(count: int) -> str:
    if count == 0:
        return "Empty"
    suffix = "job" if count == 1 else "jobs"
    return f"{count} {suffix}"


def _format_connected(is_connected: bool, healthy_label: str) -> str:
    icons = Icons()
    if is_connected:
        return f"{icons.healthy} {healthy_label}"
    return f"{icons.failed} Unhealthy"


def _format_queue_counts(*, active: int, pending: int) -> str:
    return f"{active} Active / {pending} Pending"


def _format_recovery_summary(snapshot: HealthSnapshot) -> str:
    recovery = snapshot.startup_recovery
    if recovery.total == 0:
        return "None"
    return (
        f"Downloads {recovery.downloads_recovered}, "
        f"Uploads {recovery.uploads_recovered}, "
        f"Cleanup {recovery.cleanup_recovered}"
    )


def _format_uptime(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _shorten(value: str, limit: int = 34) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."
