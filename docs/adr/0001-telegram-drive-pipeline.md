# ADR 0001: Telegram to Google Drive Pipeline

Status: Accepted

Date: 2026-07-27

## Context

This application is a personal Telegram to Google Drive manager. It receives media through a `python-telegram-bot` bot, downloads the media through either the Telegram Bot API or a Pyrogram user session, lets the Telegram user rename the local file and choose a Google Drive folder, then uploads the file to Google Drive.

The codebase uses asynchronous orchestration, a repository layer over SQLite, dependency injection through the application container, structured logging, and explicit job state transitions.

## Overall Architecture

The pipeline is split into these main components:

- `telegram_bot.py`: Telegram UI, media metadata extraction, rename flow, folder selection UI, and handoff to workers.
- `download_queue.py`: Single-worker async download queue with cancellation, throttled progress persistence, Telegram progress edits, retry/backoff, and restart recovery for interrupted downloads.
- `download_manager.py`: Download execution. It chooses Pyrogram for forwarded channel-origin messages when origin metadata is available, otherwise falls back to Bot API `file_id`.
- `drive/browser.py`: Google Drive folder browsing, folder search, validation, favorites, recent folders, and listing cache support.
- `upload_worker.py`: Single-upload worker that drains eligible upload jobs one at a time, uploads to Google Drive, verifies uploaded size, persists the Drive file ID, finalizes local cleanup, and applies upload retry/backoff.
- `database/repository.py`: Repository API for users, files, downloads, folder preferences, state transitions, retry scheduling, and crash recovery selectors.
- `job_state.py`: Explicit lifecycle states, valid transitions, terminal states, and recovery rules.
- `lifecycle.py`: Startup/shutdown wiring, dependency creation, interrupted download recovery, worker startup, and runtime temp cleanup.

SQLite is the source of truth for job state. Local files in `downloads/` and temporary files in `tmp/` are runtime artifacts tied back to file and download records.

## Job State Machine

The job lifecycle is modeled by `JobState`:

- `pending`
- `received`
- `queued`
- `downloading`
- `downloaded`
- `awaiting_rename`
- `awaiting_folder`
- `ready_for_upload`
- `uploading`
- `uploaded`
- `completed`
- `failed`
- `cancelled`
- `skipped`

The intended successful path is:

```text
received
  -> queued
  -> downloading
  -> downloaded
  -> awaiting_rename
  -> awaiting_folder
  -> ready_for_upload
  -> uploading
  -> uploaded
  -> completed
```

`completed`, `cancelled`, and `skipped` are terminal states. State changes that cross lifecycle boundaries should go through repository transition helpers so invalid transitions are rejected.

## Download Flow

1. The Telegram bot receives a supported media message.
2. The bot extracts `FileMetadata`, including:
   - Bot API `telegram_file_id`
   - Bot API `chat_id` and `message_id`
   - optional forwarded channel origin `chat_id` and `message_id`
   - filename, MIME type, size, extension, and media type
3. The repository creates or updates the Telegram user and creates a file record.
4. The bot sends an acknowledgement message and enqueues a `DownloadJob`.
5. `DownloadQueue` marks the file and download records as queued/running, starts one download task, and starts a throttled progress flush task.
6. `DownloadManager` chooses the download source:
   - If channel forward-origin metadata exists, it uses Pyrogram:
     - start the user session
     - fetch the origin message with `get_messages(origin_chat_id, origin_message_id)`
     - verify the resolved chat and expected media type
     - call `download_media(message, file_name=<part path>)`
   - Otherwise it uses Bot API:
     - resolve `telegram_file_id`
     - call `download_to_drive(custom_path=<part path>)`
7. The download is written to a unique `.part` path under `tmp/`.
8. The downloaded size is verified against Telegram metadata when size is known.
9. The completed temp file is moved into `downloads/` using a unique final filename.
10. The download is marked complete, the file transitions to `downloaded`, then to `awaiting_rename`.
11. The bot prompts the user to keep the filename, rename it, or skip.

Cancellation is task-based. `/cancel` marks the active job's cancellation event, cancels the active download task, removes partial files, marks the download and file as cancelled, and lets the worker continue with later queued jobs.

## Upload Flow

1. After rename, the bot prompts for a destination folder.
2. Folder selection can use last folder, browse, search, favorites, recent folders, or pasted folder ID.
3. The selected folder is validated for existence, accessibility, folder MIME type, not trashed, and writable capability.
4. The repository stores the destination folder metadata and marks the file `ready_for_upload`.
5. `UploadWorker.start()` starts `run_until_idle()`.
6. The worker drains eligible jobs one at a time in this priority order:
   - recoverable `uploading` with an existing `google_drive_file_id`
   - `uploaded` jobs needing final cleanup
   - due `ready_for_upload` jobs
7. A due `ready_for_upload` job is claimed by transitioning it to `uploading`.
8. The worker resolves local path, filename, MIME type, destination folder, and expected size.
9. The Google Drive uploader calls `files().create(...)`.
10. The uploader fetches Drive metadata for the returned file ID.
11. The worker verifies the Drive file size against the expected Telegram size when known.
12. On verification success, the repository persists `google_drive_file_id` and transitions the job to `uploaded`.
13. The finalizer deletes the local downloaded file and known temp variants.
14. If cleanup succeeds or the files are already gone, the job transitions to `completed`.
15. If cleanup fails, the job remains `uploaded` with the Drive file ID preserved so a later worker run can retry cleanup without uploading again.

No parallel uploads are currently performed.

## Crash Recovery Strategy

Recovery is based on persisted state:

- `downloading` recovers to `queued`; stale partial files in `tmp/` are cleaned on startup/shutdown.
- queued or running downloads with Telegram status message metadata are re-enqueued on startup.
- `uploading` without a Drive file ID recovers to `ready_for_upload`.
- `uploading` with a Drive file ID recovers to `uploaded`, then finalization runs.
- `uploaded` always finalizes cleanup and transitions to `completed`; it must never upload again.
- missing local files during finalization are treated as cleanup success.

Graceful shutdown waits for an active upload task rather than cancelling it. This avoids the common failure mode where `asyncio.to_thread()` keeps the blocking Google Drive upload running after the asyncio task has been cancelled, which could create a remote file without repository persistence.

The remaining hard-crash window is a process or machine failure after Google Drive creates a file but before the returned Drive file ID is persisted. That case cannot be fully eliminated without an additional Drive-side idempotency strategy.

## Retry and Backoff Policy

Download retries:

- Downloads have a bounded retry limit.
- Retries use capped exponential backoff.
- Cancellation never triggers retries.
- The queue survives cancellation and continues with later jobs.

Upload retries:

- Upload failures are classified as transient or permanent.
- Transient upload failures remain `ready_for_upload` but are scheduled with `upload_retry_after`.
- The worker only selects `ready_for_upload` jobs when `upload_retry_after` is null or due.
- Upload retry attempts are persisted in `upload_retry_count`.
- Retry delay defaults to exponential backoff starting at 60 seconds, doubling each attempt, capped at 15 minutes.
- Retry scheduling logs the retry reason, retry count, delay, and next scheduled retry time.
- Retry exhaustion marks the job `failed`.

Transient upload failures include:

- timeout errors
- connection/network errors
- HTTP 408
- HTTP 429
- HTTP 5xx
- Google Drive 403 rate-limit reasons such as `rateLimitExceeded` and `userRateLimitExceeded`

Permanent upload failures include:

- missing local downloaded file
- missing destination folder
- uploader not configured
- invalid upload response without a Drive file ID
- upload verification size mismatch
- authentication failures
- invalid requests
- unexpected non-transient errors

Permanent failures transition to `failed` and are not retried automatically.

## Idempotency Guarantees

The pipeline guarantees:

- A completed download is moved from a unique `.part` temp path to a unique final local path.
- Cancelled or failed downloads clean known partial temp paths.
- Once `google_drive_file_id` is persisted, the worker never uploads that file again.
- `uploaded` jobs are finalized by cleanup only.
- Finalization is idempotent: missing local files count as success.
- `uploading` with a persisted Drive file ID recovers to `uploaded` and finalizes without a duplicate upload.
- Retry schedules are persisted, so process restarts do not cause immediate retry storms.

Known idempotency limitation:

- If Google Drive successfully creates a file and the process crashes before `google_drive_file_id` is persisted, the next retry cannot know that the remote file already exists. This can create duplicates.

## Security Considerations

Secrets and credentials:

- Telegram bot token, Pyrogram API hash, Google OAuth credentials, Google refresh token, and Pyrogram session files must be treated as secrets.
- These values should be supplied through environment variables or secret storage in production.
- `credentials.json`, `data/token.json`, and `sessions/*.session` should not be committed.
- Token and session files should use restrictive filesystem permissions where the deployment platform allows it.

Logging:

- Logs should not include Telegram bot tokens, Google OAuth tokens, Pyrogram API hashes, or full credential files.
- Logs should avoid Bot API `file_id` unless needed for short-term troubleshooting because it may grant file access through the bot.
- Logs should avoid message text and captions by default because forwarded or saved-message content may contain personal or sensitive data.
- Structured logs should prefer internal database IDs, state names, retry counts, sizes, and error classes over raw user content.

Data handling:

- Downloaded files remain on local disk until upload finalization succeeds.
- Failed upload jobs preserve local files for recovery and manual inspection.
- Runtime cleanup should only delete known temp patterns and known local paths from repository records.

Access model:

- Pyrogram must use a Telegram user session, not a bot session, for channel-origin downloads.
- Google Drive folder validation checks write capability before selecting a destination.
- The current Drive scope is intended to minimize broad access, but deployment should still treat the token as sensitive.

## Known Limitations

- Uploads are single-worker and single-file at a time.
- There is no Drive-side idempotency key yet, so a hard crash after remote creation but before local persistence can create duplicates.
- Upload retry scheduling is persisted but there is no long-running scheduler that sleeps until the next retry; workers are triggered by startup or folder selection.
- Upload failures do not currently notify the Telegram user with a final failure or next retry time.
- The upload worker drains eligible jobs but exits when none are due; a future retry needs another worker trigger or process restart.
- Folder listing/search caches are in-memory only and are lost on restart.
- Telegram folder token mappings live in Telegram user context and may expire across restarts.
- Pyrogram channel-origin downloads require the user session to have access to the origin peer.
- Startup temp cleanup only removes known temp patterns.
- Some existing troubleshooting logs may be too verbose for production privacy unless log level and fields are tightened.

## Future Improvements

- Add Drive-side idempotency by pre-creating a Drive file ID, using app properties, or searching for a per-job idempotency marker before retrying upload.
- Add an upload scheduler that wakes when the next `upload_retry_after` is due.
- Add Telegram notifications for upload retry scheduling, permanent upload failure, and final completion.
- Add configurable upload concurrency after idempotency and rate-limit handling are stronger.
- Add repository-level transactional claim operations for stronger multi-process safety.
- Add stricter secret redaction in logging formatters or logging helpers.
- Add retention policy and administrative cleanup commands for failed local files.
- Add metrics for queue depth, retry counts, throughput, Drive API error classes, and cleanup failures.
- Add integration tests with real small and large files across Bot API, Pyrogram origin, folder selection, upload, restart, cancellation, and retry paths.
