# Deployment

This project ships as a single Docker service for the Telegram to Google Drive bot.

## Prerequisites

- Docker with Docker Compose support.
- A valid `.env` file based on `.env.example`.
- A Pyrogram user session already authorized under the configured `PYROGRAM_WORKDIR`.
- Google Drive OAuth files in `./data`:
  - `credentials.json`
  - `token.json`

Startup validation is strict. If Google Drive authentication is missing or invalid, the bot exits before workers or Telegram polling start.

## First Setup

Create the persistent host directories:

```bash
mkdir -p data downloads temp logs
```

Create `.env`:

```bash
cp .env.example .env
```

Edit `.env` and set:

- `TELEGRAM_BOT_TOKEN`
- `PYROGRAM_API_ID`
- `PYROGRAM_API_HASH`
- Google Drive paths, if you are not using the defaults

Place Google Drive OAuth files at:

```text
./data/credentials.json
./data/token.json
```

## Start

Build and start the bot:

```bash
docker compose up -d --build
```

The startup order is:

1. Load environment configuration.
2. Initialize logging and SQLite.
3. Validate Telegram Bot API, Pyrogram, Google Drive, directories, and database.
4. Run startup recovery.
5. Start Telegram polling and workers.

## Logs

Follow container logs:

```bash
docker compose logs -f bot
```

Read application logs from the mounted log directory:

```bash
tail -f logs/app.log
```

Docker log rotation is configured in `docker-compose.yml` using the `json-file` driver with `10m` files and `5` retained files.

## Graceful Shutdown

Stop the bot:

```bash
docker compose stop bot
```

Stop and remove the container:

```bash
docker compose down
```

The image uses an exec-form entrypoint, so Docker stop signals are delivered directly to the Python process.

## Healthcheck

The Docker image runs:

```bash
python -m app.cli health
```

The command is intentionally lightweight. It does not reconnect Telegram, Pyrogram, or Google Drive. It checks local runtime evidence and exits non-zero when a critical subsystem appears unavailable.
