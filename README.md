# Telegram to Google Drive Manager

Production-ready foundation for a Python 3.12 Telegram bot that will manage Google Drive workflows. The bot receives Telegram files, stores metadata, downloads media through the Telegram Bot API or Pyrogram channel forward-origin metadata, and prepares a rename and folder-selection decision. Google Drive uploads are not implemented yet.

## Features

- `python-telegram-bot` v22 polling bot with `/start`
- Google Drive OAuth authentication using `credentials.json`
- SQLite connection and repository layer
- Application container and dependency registry
- Explicit startup/shutdown lifecycle
- Structured console and rotating file logging
- Configuration validation with friendly errors
- Async task manager for future upload queues
- Telegram file reception for documents, videos, audio, photos, animations, and voice messages
- Hybrid download manager with Bot API `file_id` fallback and Pyrogram channel forward-origin support
- Rename workflow: keep original filename, rename, or skip
- Docker, Docker Compose, and Koyeb worker support
- Ruff, Black, mypy, pre-commit, and tests

## Project Structure

```text
.
├── app/
│   ├── constants.py
│   ├── config.py
│   ├── exceptions.py
│   ├── lifecycle.py
│   ├── logging_config.py
│   ├── download_manager.py
│   ├── download_queue.py
│   ├── models.py
│   ├── progress.py
│   ├── pyrogram_client.py
│   ├── services.py
│   ├── task_manager.py
│   ├── telegram_bot.py
│   ├── database/
│   │   ├── connection.py
│   │   └── repository.py
│   ├── drive/
│   │   └── auth.py
│   └── utils/
├── tests/
├── data/
├── downloads/
├── sessions/
├── logs/
├── main.py
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
├── koyeb.yaml
├── Procfile
└── .env.example
```

## Local Installation

Use Python 3.12 for production parity.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If Python 3.12 is not installed, install it first or use Docker.

## Environment

Create `.env`:

```powershell
Copy-Item .env.example .env
```

Set the required Telegram bot token:

```env
TELEGRAM_BOT_TOKEN=123456789:replace-with-your-token
```

Optional Pyrogram credentials for forwarded channel-origin downloads:

```env
PYROGRAM_API_ID=
PYROGRAM_API_HASH=
PYROGRAM_SESSION_NAME=g_drive_bot
PYROGRAM_WORKDIR=sessions
```

`PYROGRAM_API_ID` and `PYROGRAM_API_HASH` must be configured together. Use a Telegram user session, not a bot token.

Authorize the user session once before relying on channel forward-origin downloads:

```powershell
python .\scripts\authorize_pyrogram.py
```

## Google OAuth

Create an OAuth client in Google Cloud Console and save it as `credentials.json` in the project root.

The bot does not launch an interactive Google OAuth flow unless explicitly enabled:

```env
GOOGLE_AUTO_AUTH=true
```

Run once locally to generate `data/token.json`, then set:

```env
GOOGLE_AUTO_AUTH=false
```

In production, `credentials.json` must be available or startup fails with a configuration error.

## Running Locally

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

Send `/start` to the bot in Telegram. The app initializes SQLite, optional Pyrogram channel-origin support, Google Drive auth state, and Telegram polling.

## File Reception Pipeline

Supported Telegram media:

- document
- video
- audio
- photo
- animation
- voice

When a supported file is received, the bot:

1. Extracts file metadata.
2. Stores user, file, and download records in SQLite.
3. Acknowledges receipt.
4. Queues a single-worker async download job.
5. Downloads via Pyrogram when channel forward-origin metadata is available, otherwise via the Telegram Bot API `file_id`, into a temporary `.part` file.
6. Moves the completed file into `downloads/` with a unique filename.
7. Updates progress every few seconds.
8. Prompts the user to keep the original filename, rename, or skip.
9. Marks the file `ready_for_upload` for a future Google Drive milestone.

No Google Drive upload is performed in this milestone.

## Job state machine

The complete download-to-upload lifecycle is defined in `app/job_state.py`. The future
Google Drive uploader must use these states instead of inventing ad-hoc status strings:

`QUEUED -> DOWNLOADING -> DOWNLOADED -> READY_FOR_UPLOAD -> UPLOADING -> UPLOADED -> COMPLETED`

The current rename and folder-selection pauses are represented as `AWAITING_RENAME` and
`AWAITING_FOLDER`. Terminal states are `COMPLETED`, `CANCELLED`, and `SKIPPED`.

Crash recovery rules are explicit:

- `DOWNLOADING` recovers to `QUEUED` after stale partial files are cleaned.
- `UPLOADING` recovers to `READY_FOR_UPLOAD` when no Drive file ID was persisted.
- `UPLOADING` recovers to `UPLOADED` when a Drive file ID was already persisted.
- `UPLOADED` must finish as `COMPLETED` instead of uploading again.

These rules make retries idempotent: once `google_drive_file_id` is stored, a retry must
finalize the existing Drive file record instead of creating a duplicate.

## Docker

```powershell
docker compose up --build
```

Mount or provide `credentials.json` if Google Drive authentication should be available. Runtime data is stored under `data/`.

## Koyeb

The repo includes `Dockerfile`, `koyeb.yaml`, and `Procfile`. Deploy it as a worker service because Telegram polling does not expose an HTTP port.

Configure runtime secrets:

- `TELEGRAM_BOT_TOKEN`
- `PYROGRAM_API_ID` and `PYROGRAM_API_HASH` if channel forward-origin downloads are required
Provide `credentials.json` and, later, `data/token.json` through your chosen Koyeb secret or persistent storage setup before enabling upload features.

## Quality Checks

Install development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Run checks:

```powershell
ruff check .
black --check .
mypy app tests
pytest
```

Install pre-commit hooks:

```powershell
pre-commit install
```

## Troubleshooting

- `TELEGRAM_BOT_TOKEN has an invalid format`: check the token copied from BotFather.
- `PYROGRAM_API_ID must be an integer`: use the numeric API ID from my.telegram.org.
- `PYROGRAM_API_ID and PYROGRAM_API_HASH must be configured together`: set both or leave both blank.
- `TgCrypto is missing`: Pyrogram still works, but channel-origin downloads will be slower.
- `Google credentials file was not found`: place `credentials.json` in the project root or update `GOOGLE_CREDENTIALS_FILE`.
- Bot does not reply: keep `python main.py` running and confirm Telegram can reach `getMe` with the configured token.
