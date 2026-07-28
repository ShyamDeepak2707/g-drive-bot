FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    LOG_FILE=/logs/app.log \
    SQLITE_DB_PATH=/data/app.sqlite3 \
    GOOGLE_CREDENTIALS_FILE=/data/credentials.json \
    GOOGLE_TOKEN_FILE=/data/token.json \
    PYROGRAM_WORKDIR=/data/sessions \
    DOWNLOADS_DIR=/downloads \
    TEMP_DIR=/temp

WORKDIR /app

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app /data /downloads /temp /logs \
    && chown -R app:app /app /data /downloads /temp /logs

COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=app:app app ./app
COPY --chown=app:app main.py .

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "app.cli", "health"]

ENTRYPOINT ["python", "main.py"]
