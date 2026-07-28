from __future__ import annotations

from enum import StrEnum


class InvalidJobStateTransition(ValueError):
    def __init__(self, current: JobState, target: JobState) -> None:
        super().__init__(f"Invalid job state transition: {current.value} -> {target.value}.")
        self.current = current
        self.target = target


class JobState(StrEnum):
    PENDING = "pending"
    RECEIVED = "received"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    AWAITING_RENAME = "awaiting_rename"
    AWAITING_FOLDER = "awaiting_folder"
    READY_FOR_UPLOAD = "ready_for_upload"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


VALID_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PENDING: frozenset(
        {
            JobState.RECEIVED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.RECEIVED: frozenset(
        {
            JobState.QUEUED,
            JobState.READY_FOR_UPLOAD,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.QUEUED: frozenset(
        {
            JobState.DOWNLOADING,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.DOWNLOADING: frozenset(
        {
            JobState.QUEUED,
            JobState.DOWNLOADED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.DOWNLOADED: frozenset(
        {
            JobState.AWAITING_RENAME,
            JobState.AWAITING_FOLDER,
            JobState.READY_FOR_UPLOAD,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.AWAITING_RENAME: frozenset(
        {
            JobState.AWAITING_FOLDER,
            JobState.READY_FOR_UPLOAD,
            JobState.SKIPPED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.AWAITING_FOLDER: frozenset(
        {
            JobState.READY_FOR_UPLOAD,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.READY_FOR_UPLOAD: frozenset(
        {
            JobState.UPLOADING,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.UPLOADING: frozenset(
        {
            JobState.READY_FOR_UPLOAD,
            JobState.UPLOADED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.UPLOADED: frozenset(
        {
            JobState.COMPLETED,
            JobState.FAILED,
        }
    ),
    JobState.FAILED: frozenset(
        {
            JobState.QUEUED,
            JobState.READY_FOR_UPLOAD,
            JobState.CANCELLED,
        }
    ),
    JobState.COMPLETED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.SKIPPED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {
        JobState.COMPLETED,
        JobState.CANCELLED,
        JobState.SKIPPED,
    }
)


def can_transition(current: JobState, target: JobState) -> bool:
    return target in VALID_TRANSITIONS[current]


def require_transition(current: JobState, target: JobState) -> None:
    if not can_transition(current, target):
        raise InvalidJobStateTransition(current, target)


def is_terminal(state: JobState) -> bool:
    return state in TERMINAL_STATES


def recovery_state(state: JobState, *, google_drive_file_id: str | None = None) -> JobState:
    if state == JobState.DOWNLOADING:
        return JobState.QUEUED
    if state == JobState.UPLOADING:
        return JobState.UPLOADED if google_drive_file_id else JobState.READY_FOR_UPLOAD
    if state == JobState.UPLOADED and google_drive_file_id is None:
        return JobState.READY_FOR_UPLOAD
    return state


def should_start_upload(state: JobState, *, google_drive_file_id: str | None = None) -> bool:
    if google_drive_file_id is not None:
        return False
    return state in {JobState.READY_FOR_UPLOAD, JobState.UPLOADING}
