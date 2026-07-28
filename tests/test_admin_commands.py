from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.admin_commands import AdminCommandService
from app.admin_service import CancelJobSummary, QueueInspection, RetryFailedSummary
from app.health import HealthSnapshot, QueueHealth
from app.shutdown_control import ShutdownRequestResult
from app.startup_recovery import StartupRecoverySummary
from app.temp_files import TempCleanupSummary


@dataclass
class FakeHealthService:
    snapshot_calls: int = 0

    def snapshot(self) -> HealthSnapshot:
        self.snapshot_calls += 1
        return HealthSnapshot(
            startup_time=datetime(2026, 7, 28, 10, 0, 0, tzinfo=UTC),
            uptime_seconds=3661,
            telegram_bot_connected=True,
            pyrogram_connected=True,
            google_drive_authenticated=True,
            database_connected=True,
            queues=QueueHealth(
                pending_downloads=2,
                active_downloads=1,
                pending_uploads=3,
                active_uploads=0,
            ),
            startup_recovery=StartupRecoverySummary(0, 0, 0),
        )


class FakeAdminService:
    def __init__(self, *, fail_queues: bool = False) -> None:
        self.queue_inspection_calls = 0
        self.retry_failed_jobs_calls = 0
        self.cleanup_temp_calls: list[bool] = []
        self.cancel_job_calls: list[tuple[int, str | None]] = []
        self.fail_queues = fail_queues

    def queue_inspection(self) -> QueueInspection:
        self.queue_inspection_calls += 1
        if self.fail_queues:
            raise RuntimeError("database unavailable")
        return QueueInspection(
            pending_downloads=0,
            active_download=None,
            pending_uploads=0,
            active_upload=None,
            upload_worker_busy=False,
        )

    def retry_failed_jobs(self) -> RetryFailedSummary:
        self.retry_failed_jobs_calls += 1
        return RetryFailedSummary(
            eligible_jobs=5,
            requeued_jobs=4,
            skipped_jobs=1,
        )

    def cleanup_temp(self, *, confirm: bool = False) -> TempCleanupSummary:
        self.cleanup_temp_calls.append(confirm)
        return TempCleanupSummary(
            confirmed=confirm,
            total_files=2,
            total_bytes=3072,
            deleted_files=2 if confirm else 0,
            deleted_bytes=3072 if confirm else 0,
            files=(),
        )

    def cancel_job(self, job_id: int, job_type: str | None = None) -> CancelJobSummary:
        self.cancel_job_calls.append((job_id, job_type))
        if job_id == 500:
            raise RuntimeError("database unavailable")
        return CancelJobSummary(
            job_id=job_id,
            job_type=job_type or "download",
            filename="example.txt",
            status="Cancelled",
            message="Cancelled",
            cancelled=True,
        )


class FakeClock:
    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def __call__(self) -> float:
        return self._values.pop(0)


class FakeShutdownController:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self._requested = False

    def request_shutdown(self, *, source: str) -> ShutdownRequestResult:
        self.calls.append(source)
        if self.fail:
            raise RuntimeError("shutdown unavailable")
        if self._requested:
            return ShutdownRequestResult(initiated=False, source=source)
        self._requested = True
        return ShutdownRequestResult(initiated=True, source=source)


def test_admin_command_service_logs_success_and_preserves_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    health_service = FakeHealthService()
    service = AdminCommandService(
        health_service=health_service,  # type: ignore[arg-type]
        admin_service=FakeAdminService(),  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(1.0, 1.125),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.health()

    assert health_service.snapshot_calls == 1
    assert "📊 <b>System Health</b>" in message.text
    assert "⏳ <b>Uptime</b>: <b>1h 01m</b>" in message.text
    started = _event(caplog, "admin_command_started")
    completed = _event(caplog, "admin_command_completed")
    assert started.__dict__["admin_command"] == "health"
    assert completed.__dict__["admin_command"] == "health"
    assert completed.__dict__["elapsed_ms"] == 125


def test_admin_command_service_converts_unexpected_exceptions_to_error_card(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    admin_service = FakeAdminService(fail_queues=True)
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=admin_service,  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(2.0, 2.25),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.queues()

    assert admin_service.queue_inspection_calls == 1
    assert "❌ <b>Admin Command Failed</b>" in message.text
    assert "The command failed unexpectedly. Check the logs for details." in message.text
    assert "<b>Command</b>: queues" in message.text
    failed = _event(caplog, "admin_command_failed")
    assert failed.__dict__["admin_command"] == "queues"
    assert failed.__dict__["elapsed_ms"] == 250
    assert failed.exc_info is not None


def test_admin_command_service_emits_retry_failed_audit_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    admin_service = FakeAdminService()
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=admin_service,  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(3.0, 3.01),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.retry_failed(admin_user_id=123456789)

    assert admin_service.retry_failed_jobs_calls == 1
    assert "📊 <b>Retry Failed Jobs</b>" in message.text
    audit = _event(caplog, "admin_retry_failed")
    assert audit.__dict__["admin_user_id"] == 123456789
    assert audit.__dict__["eligible"] == 5
    assert audit.__dict__["requeued"] == 4
    assert audit.__dict__["skipped"] == 1


def test_admin_command_service_emits_cleanup_temp_audit_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    admin_service = FakeAdminService()
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=admin_service,  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(4.0, 4.03),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.cleanup_temp(admin_user_id=123456789, confirm=True)

    assert admin_service.cleanup_temp_calls == [True]
    assert "📊 <b>Temporary Cleanup</b>" in message.text
    audit = _event(caplog, "admin_cleanup_temp")
    assert audit.__dict__["admin_user_id"] == 123456789
    assert audit.__dict__["confirmed"] is True
    assert audit.__dict__["orphaned_files"] == 2
    assert audit.__dict__["deleted_files"] == 2


def test_admin_command_service_shutdown_initiates_graceful_shutdown_and_audit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    shutdown_controller = FakeShutdownController()
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=FakeAdminService(),  # type: ignore[arg-type]
        shutdown_controller=shutdown_controller,  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(5.0, 5.02),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.shutdown(admin_user_id=123456789)

    assert shutdown_controller.calls == ["telegram_admin"]
    assert "📊 <b>Shutdown</b>" in message.text
    assert "⚙️ <b>Status</b>: <b>Graceful shutdown started</b>" in message.text
    audit = _event(caplog, "admin_shutdown")
    assert audit.__dict__["admin_user_id"] == 123456789
    assert audit.__dict__["shutdown_source"] == "telegram_admin"


def test_admin_command_service_shutdown_is_idempotent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    shutdown_controller = FakeShutdownController()
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=FakeAdminService(),  # type: ignore[arg-type]
        shutdown_controller=shutdown_controller,  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(6.0, 6.01, 7.0, 7.01),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        first = service.shutdown(admin_user_id=123456789)
        second = service.shutdown(admin_user_id=123456789)

    assert "Graceful shutdown started" in first.text
    assert "Shutdown already in progress" in second.text
    assert shutdown_controller.calls == ["telegram_admin", "telegram_admin"]
    assert _event_count(caplog, "admin_shutdown") == 1


def test_admin_command_service_shutdown_exception_uses_standard_error_card(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    shutdown_controller = FakeShutdownController(fail=True)
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=FakeAdminService(),  # type: ignore[arg-type]
        shutdown_controller=shutdown_controller,  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(8.0, 8.25),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.shutdown(admin_user_id=123456789)

    assert "❌ <b>Admin Command Failed</b>" in message.text
    assert "<b>Command</b>: shutdown" in message.text
    failed = _event(caplog, "admin_command_failed")
    assert failed.__dict__["admin_command"] == "shutdown"
    assert _event_count(caplog, "admin_shutdown") == 0


def test_admin_command_service_cancel_job_formats_summary_and_emits_audit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    admin_service = FakeAdminService()
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=admin_service,  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(9.0, 9.01),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.cancel_job(
            admin_user_id=123456789,
            job_id=42,
            job_type="download",
        )

    assert admin_service.cancel_job_calls == [(42, "download")]
    assert "🛑 <b>Cancel Job</b>" in message.text
    assert "📄 <b>Job</b>: <b>example.txt</b>" in message.text
    assert "ℹ️ <b>Status</b>: <b>Cancelled</b>" in message.text
    audit = _event(caplog, "admin_cancel_job")
    assert audit.__dict__["admin_user_id"] == 123456789
    assert audit.__dict__["job_id"] == 42
    assert audit.__dict__["job_type"] == "download"


def test_admin_command_service_cancel_job_does_not_audit_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class NoopAdminService(FakeAdminService):
        def cancel_job(self, job_id: int, job_type: str | None = None) -> CancelJobSummary:
            self.cancel_job_calls.append((job_id, job_type))
            return CancelJobSummary(
                job_id=job_id,
                job_type=job_type,
                filename="done.txt",
                status="Completed",
                message="Job already completed.",
                cancelled=False,
            )

    logger = logging.getLogger("test.admin_commands")
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=NoopAdminService(),  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(10.0, 10.01),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.cancel_job(admin_user_id=123456789, job_id=42)

    assert "Job already completed." in message.text
    assert _event_count(caplog, "admin_cancel_job") == 0


def test_admin_command_service_cancel_job_exception_uses_standard_error_card(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.admin_commands")
    admin_service = FakeAdminService()
    service = AdminCommandService(
        health_service=FakeHealthService(),  # type: ignore[arg-type]
        admin_service=admin_service,  # type: ignore[arg-type]
        logger=logger,
        monotonic=FakeClock(11.0, 11.25),
    )

    with caplog.at_level(logging.INFO, logger="test.admin_commands"):
        message = service.cancel_job(admin_user_id=123456789, job_id=500)

    assert "❌ <b>Admin Command Failed</b>" in message.text
    assert "<b>Command</b>: cancel" in message.text
    assert _event_count(caplog, "admin_cancel_job") == 0


def _event(caplog: pytest.LogCaptureFixture, event: str) -> logging.LogRecord:
    for record in caplog.records:
        if getattr(record, "event", None) == event:
            return record
    raise AssertionError(f"Missing log event: {event}")


def _event_count(caplog: pytest.LogCaptureFixture, event: str) -> int:
    return sum(1 for record in caplog.records if getattr(record, "event", None) == event)
