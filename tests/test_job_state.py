from __future__ import annotations

import pytest

from app.job_state import (
    InvalidJobStateTransition,
    JobState,
    can_transition,
    is_terminal,
    recovery_state,
    require_transition,
    should_start_upload,
)


def test_job_state_defines_complete_download_and_upload_lifecycle() -> None:
    assert JobState.QUEUED.value == "queued"
    assert JobState.DOWNLOADING.value == "downloading"
    assert JobState.DOWNLOADED.value == "downloaded"
    assert JobState.UPLOADING.value == "uploading"
    assert JobState.UPLOADED.value == "uploaded"
    assert JobState.COMPLETED.value == "completed"
    assert JobState.FAILED.value == "failed"
    assert JobState.CANCELLED.value == "cancelled"


def test_job_state_allows_valid_lifecycle_transitions() -> None:
    assert can_transition(JobState.QUEUED, JobState.DOWNLOADING)
    assert can_transition(JobState.DOWNLOADING, JobState.DOWNLOADED)
    assert can_transition(JobState.DOWNLOADED, JobState.AWAITING_RENAME)
    assert can_transition(JobState.AWAITING_RENAME, JobState.AWAITING_FOLDER)
    assert can_transition(JobState.AWAITING_FOLDER, JobState.READY_FOR_UPLOAD)
    assert can_transition(JobState.READY_FOR_UPLOAD, JobState.UPLOADING)
    assert can_transition(JobState.UPLOADING, JobState.UPLOADED)
    assert can_transition(JobState.UPLOADED, JobState.COMPLETED)


def test_job_state_rejects_invalid_transitions() -> None:
    assert not can_transition(JobState.COMPLETED, JobState.UPLOADING)
    with pytest.raises(InvalidJobStateTransition):
        require_transition(JobState.COMPLETED, JobState.UPLOADING)


def test_job_state_marks_terminal_states() -> None:
    assert is_terminal(JobState.COMPLETED)
    assert is_terminal(JobState.CANCELLED)
    assert is_terminal(JobState.SKIPPED)
    assert not is_terminal(JobState.UPLOADING)


def test_job_state_crash_recovery_keeps_retries_idempotent() -> None:
    assert recovery_state(JobState.DOWNLOADING) == JobState.QUEUED
    assert recovery_state(JobState.UPLOADING) == JobState.READY_FOR_UPLOAD
    assert (
        recovery_state(JobState.UPLOADING, google_drive_file_id="drive-id")
        == JobState.UPLOADED
    )
    assert recovery_state(JobState.UPLOADED) == JobState.READY_FOR_UPLOAD
    assert (
        recovery_state(JobState.UPLOADED, google_drive_file_id="drive-id")
        == JobState.UPLOADED
    )


def test_job_state_upload_start_policy_prevents_duplicate_uploads() -> None:
    assert should_start_upload(JobState.READY_FOR_UPLOAD)
    assert should_start_upload(JobState.UPLOADING)
    assert not should_start_upload(JobState.UPLOADING, google_drive_file_id="drive-id")
    assert not should_start_upload(JobState.UPLOADED, google_drive_file_id="drive-id")
