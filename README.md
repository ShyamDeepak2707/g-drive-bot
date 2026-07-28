# Telegram Drive Manager

[![CI](https://github.com/ShyamDeepak2707/g-drive-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ShyamDeepak2707/g-drive-bot/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)
![Ruff](https://img.shields.io/badge/lint-ruff-46aaf7.svg)
![mypy](https://img.shields.io/badge/type%20checked-mypy-blue.svg)
![Pytest](https://img.shields.io/badge/tests-pytest-0a9edc.svg)

Telegram Drive Manager is a personal Telegram bot that receives files, downloads them from Telegram, lets the user rename them, lets the user choose a Google Drive destination folder, uploads the file to Google Drive, verifies the uploaded size, and finalizes local cleanup.

The project is designed as a production-oriented async Python service with clear state transitions, crash recovery, structured logging, Docker deployment, and admin-only operational commands.

## Overview

This bot is intended for a personal workflow:

1. Forward or send a file to the Telegram bot.
2. The bot downloads the media.
3. The bot asks whether to keep or change the filename.
4. The bot asks for a Google Drive destination folder.
5. The upload worker uploads the local file to Google Drive.
6. The upload is verified against Telegram metadata.
7. Local downloaded and temporary files are cleaned up.

The application uses two Telegram integrations:

- `python-telegram-bot` handles bot polling, commands, inline keyboards, and user interaction.
- `Pyrogram` uses a Telegram user session for high-performance downloads when channel forward-origin metadata is available.

When a Pyrogram source cannot be resolved, the download manager can fall back to the Telegram Bot API file identifier for bot-received media.

## Features

- Telegram polling bot using `python-telegram-bot`.
- Pyrogram user-session download path for forwarded channel media.
- Telegram Bot API file fallback when Pyrogram source metadata is unavailable.
- Download queue with retries, cancellation, progress throttling, and partial-file cleanup.
- Google Drive folder browser with My Drive, folder navigation, pagination, refresh, favorites, recent folders, folder ID validation, and search.
- Rename workflow before upload.
- Google Drive upload worker with recoverable state transitions.
- Upload verification using Drive metadata before marking a job uploaded.
- Finalization step that removes local downloaded and temporary files.
- Startup validation for configuration, directories, SQLite, Telegram Bot API, Pyrogram, and Google Drive.
- Startup recovery for interrupted downloads, interrupted uploads, and pending cleanup.
- Read-only health and admin service layers.
- Admin-only Telegram commands for health, queues, failures, stats, retry, temp cleanup, shutdown, settings, and cancellation.
- Structured logging to console and rotating log file.
- Docker and Docker Compose deployment.
- GitHub Actions CI for linting, formatting, type checking, and tests.

## Architecture

High-level runtime flow:

```text
+------------------+       +-----------------------+
| Telegram user    | ----> | python-telegram-bot   |
+------------------+       +-----------+-----------+
                                      |
                                      v
                         +------------+-------------+
                         | Handlers and UI services |
                         +------------+-------------+
                                      |
                                      v
          +---------------------------+----------------------------+
          | SQLite repositories and file/job state machine         |
          +---------------------------+----------------------------+
                         |                              |
                         v                              v
              +----------+----------+        +----------+----------+
              | DownloadQueue       |        | UploadWorker         |
              | DownloadManager     |        | Google Drive service |
              +----------+----------+        +----------+----------+
                         |                              |
                         v                              v
              +----------+----------+        +----------+----------+
              | Telegram media      |        | Google Drive files   |
              | Bot API / Pyrogram  |        | and folder metadata  |
              +---------------------+        +---------------------+
```

### Job State Machine

The file lifecycle is explicit and persisted:

```text
RECEIVED
  |
  v
QUEUED -> DOWNLOADING -> DOWNLOADED -> AWAITING_RENAME
                                      |
                                      v
                               AWAITING_FOLDER
                                      |
                                      v
                              READY_FOR_UPLOAD
                                      |
                                      v
                                UPLOADING
                                      |
                                      v
                                 UPLOADED
                                      |
                                      v
                                COMPLETED

Any recoverable processing state may move to FAILED.
Queued or running work may move to CANCELLED.
COMPLETED, FAILED, and CANCELLED are terminal for normal processing.
```

Upload retries use exponential backoff for transient errors. Permanent errors are not retried automatically.

### Download Flow

Downloads persist source metadata, queue a job, resolve the original Pyrogram message when forward-origin metadata is available, and fall back to the Bot API file identifier when necessary. Progress updates are throttled to avoid slowing the transfer path.

### Upload Flow

Uploads process `READY_FOR_UPLOAD` jobs one at a time, transition to `UPLOADING`, upload to Google Drive, verify the Drive metadata size, persist `google_drive_file_id`, transition to `UPLOADED`, and then finalize local cleanup as `COMPLETED`.

If a process crashes after a Drive upload succeeds, recovery detects the existing Drive file ID and continues with verification or cleanup instead of uploading the same file again.

## Tech Stack

- Python 3.12
- `python-telegram-bot`
- Pyrogram
- TgCrypto
- Google Drive API client
- SQLite
- `python-dotenv`
- Ruff
- Black
- mypy
- Pytest
- Docker
- GitHub Actions

## Project Structure

```text
.
|-- app/
|   |-- admin_commands.py       # Admin command coordinator and formatting
|   |-- admin_service.py        # Read-only and operational admin summaries
|   |-- cli.py                  # Lightweight operational CLI commands
|   |-- config.py               # Environment-backed settings
|   |-- download_manager.py     # Telegram download orchestration
|   |-- download_queue.py       # Download queue and retry handling
|   |-- health_service.py       # Read-only runtime health aggregation
|   |-- lifecycle.py            # Startup, validation, recovery, shutdown
|   |-- models.py               # Domain models
|   |-- progress.py             # Progress snapshots and formatting data
|   |-- pyrogram_client.py      # Pyrogram session manager
|   |-- shutdown_control.py     # Graceful shutdown coordination
|   |-- startup_recovery.py     # Idempotent recovery phase
|   |-- startup_validation.py   # Startup validation phase
|   |-- telegram_bot.py         # Telegram handlers and command registration
|   |-- temp_file_service.py    # Temporary-file scanning and cleanup
|   |-- upload_worker.py        # Google Drive upload worker
|   |-- database/
|   |   |-- connection.py
|   |   `-- repository.py
|   |-- drive/
|   |   |-- auth.py
|   |   |-- browser.py
|   |   `-- uploader.py
|   |-- ui/
|   |   `-- cards.py
|   `-- utils/
|-- docs/
|   |-- adr/
|   `-- deployment.md
|-- scripts/
|   `-- authorize_pyrogram.py
|-- tests/
|-- Dockerfile
|-- docker-compose.yml
|-- main.py
|-- requirements.txt
|-- requirements-dev.txt
`-- .env.example
```

## Installation

Use Python 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

For production-only installs, use `requirements.txt`.

## Configuration (.env)

Copy the example file:

```powershell
Copy-Item .env.example .env
```

On macOS or Linux:

```bash
cp .env.example .env
```

Then edit `.env` with your own values. Do not commit `.env`, session files, OAuth tokens, downloaded files, or credentials.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `APP_ENV` | No | `development` | Runtime environment name. Production enables stricter Google credential validation. |
| `LOG_LEVEL` | No | `INFO` | Logging level. |
| `LOG_FILE` | No | `logs/app.log` | File log destination. |
| `TELEGRAM_BOT_TOKEN` | Yes | None | Telegram Bot API token from BotFather. |
| `TELEGRAM_ADMIN_USER_IDS` | Recommended | Empty | Comma-separated Telegram user IDs allowed to use admin-only commands. |
| `PYROGRAM_API_ID` | Yes for startup validation | None | Telegram API ID for the Pyrogram user session. |
| `PYROGRAM_API_HASH` | Yes for startup validation | None | Telegram API hash for the Pyrogram user session. |
| `PYROGRAM_SESSION_NAME` | No | `g_drive_bot` | Pyrogram session name. |
| `PYROGRAM_WORKDIR` | No | `sessions` | Directory where the Pyrogram session is stored. |
| `PYROGRAM_MAX_CONCURRENT_TRANSMISSIONS` | No | `4` | Pyrogram transfer concurrency. |
| `GOOGLE_CREDENTIALS_FILE` | Yes in production | `credentials.json` | Google OAuth client credentials file. |
| `GOOGLE_TOKEN_FILE` | Yes after auth | `data/token.json` | Google OAuth token cache. |
| `GOOGLE_SCOPES` | No | `https://www.googleapis.com/auth/drive.file` | Comma-separated Google OAuth scopes. |
| `GOOGLE_AUTO_AUTH` | No | `false` | Enables interactive OAuth flow when generating a token locally. |
| `SQLITE_DB_PATH` | No | `data/app.sqlite3` | SQLite database path. |
| `DOWNLOADS_DIR` | No | `downloads` | Directory for completed local downloads pending upload/finalization. |
| `TEMP_DIR` | No | `tmp` | Directory for temporary and partial files. |
| `FOLDER_BROWSER_PAGE_SIZE` | No | `8` | Number of folders shown per Telegram page. |
| `FOLDER_BROWSER_CACHE_TTL_SECONDS` | No | `300` | Google Drive folder-listing cache TTL. |
| `FOLDER_RECENT_LIMIT` | No | `5` | Maximum recent folders remembered per Telegram user. |

Setup notes:

- Create a bot with BotFather and set `TELEGRAM_BOT_TOKEN`.
- Get your Telegram user ID with `/id`, then add it to `TELEGRAM_ADMIN_USER_IDS`.
- Authorize Pyrogram once with `python .\scripts\authorize_pyrogram.py`.
- Use a Pyrogram user session, not a bot token.
- Create Google OAuth credentials, generate `GOOGLE_TOKEN_FILE` locally with `GOOGLE_AUTO_AUTH=true`, then set `GOOGLE_AUTO_AUTH=false`.

```env
TELEGRAM_ADMIN_USER_IDS=123456789
```

## Running Locally

Activate the virtual environment and start the bot:

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

The startup sequence validates dependencies before workers or Telegram polling start:

```text
Startup validation
  Configuration
  Database
  Directories
  Telegram Bot
  Pyrogram
  Google Drive

Startup recovery
  Interrupted downloads
  Interrupted uploads
  Pending cleanup
```

If validation fails, the application logs an actionable error and exits.

## Docker Deployment

Build and run with Docker Compose:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f bot
```

Stop gracefully:

```bash
docker compose down
```

Compose mounts persistent runtime directories for `/data`, `/downloads`, `/temp`, and `/logs`. The image uses Python 3.12 slim, runs as a non-root user, and includes a lightweight healthcheck command.

## Telegram Admin Commands

Admin commands require the caller's Telegram user ID to be listed in `TELEGRAM_ADMIN_USER_IDS`.

| Command | Access | Description |
| --- | --- | --- |
| `/health` | Admin | Show uptime, subsystem health, queue counts, and latest startup recovery summary. |
| `/queues` | Admin | Inspect pending and active downloads/uploads. |
| `/failed` | Admin | Show failed downloads and uploads with failure reasons. |
| `/stats` | Admin | Show runtime statistics, totals, failures, retries, and uptime. |
| `/retry_failed` | Admin | Requeue retry-eligible failed jobs using the existing retry policy. |
| `/cleanup_temp` | Admin | Preview orphaned temporary files without deleting them. |
| `/cleanup_temp --confirm` | Admin | Delete only orphaned temporary files not associated with active jobs. |
| `/cancel <job_id> [download\|upload]` | Admin | Cancel a queued or running job cooperatively. |
| `/settings` | Admin | Show safe runtime settings without secrets. |
| `/shutdown` | Admin | Start graceful application shutdown. |

Operational admin commands emit structured audit logs for retry, cleanup, shutdown, and cancellation outcomes.

## User Commands

| Command | Description |
| --- | --- |
| `/start` | Start the bot and show the initial response. |
| `/help` | Show available user and admin commands. |
| `/ping` | Check whether the bot is responsive. |
| `/id` | Show Telegram user and chat IDs. |
| `/status` | Show current download, upload, queue, and worker status. |
| `/cancel` | Cancel the current user action or active download flow. |

The main user workflow is driven by inline keyboards after a file is received.

## Development

Install development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Useful commands:

```powershell
ruff check
black --check .
mypy app tests
pytest -q
```

Format code before committing:

```powershell
black .
```

Run a targeted test:

```powershell
pytest tests/test_upload_worker.py -q
```

The codebase uses async orchestration, repositories for persistence, dependency injection through the application container, structured logging, and reusable HTML Telegram UI helpers.

## Running Tests

Run the full suite:

```powershell
pytest -q
```

Run quality checks:

```powershell
ruff check
black --check .
mypy app tests
```

The test suite covers configuration, startup validation, recovery, downloads, folder management, uploads, admin commands, and UI formatting.

## CI

GitHub Actions runs on `push` and `pull_request`.

The CI workflow uses Ubuntu latest and Python 3.12, caches pip dependencies, installs development requirements, and runs:

```text
ruff check
black --check .
mypy app tests
pytest -q
```

The workflow fails if any step fails.

## Troubleshooting

| Problem | Checks |
| --- | --- |
| Startup fails on Google Drive | Verify `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, OAuth scopes, and Docker mount paths. |
| Pyrogram cannot access a file | Verify the user session account, channel access, session file path, and whether the message has forward-origin metadata. |
| Hidden sender forwards use fallback | Telegram may copy/re-send without forward-origin metadata, so Pyrogram cannot reconstruct the original message. |
| Downloads are slow | Confirm TgCrypto is installed with `pip show tgcrypto`, then check transfer concurrency and network conditions. |
| Upload retries are delayed | Transient Drive failures use exponential backoff; permanent failures are not retried automatically. |
| Temporary files remain | Run `/cleanup_temp` for a preview, then `/cleanup_temp --confirm` to delete only orphaned files. |

## Contributing

Contributions should keep handlers thin, put orchestration in services, keep persistence in repositories, preserve idempotent worker behavior, add focused tests, and avoid logging secrets or session data.

Before opening a pull request, run:

```powershell
ruff check
black --check .
mypy app tests
pytest -q
```

## License

No license file is currently included. Add an explicit license before public distribution.
