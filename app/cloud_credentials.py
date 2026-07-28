from __future__ import annotations

import base64
import binascii
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.exceptions import StartupValidationError

GOOGLE_CREDENTIALS_BASE64_ENV = "GOOGLE_CREDENTIALS_BASE64"
PYROGRAM_SESSION_BASE64_ENV = "PYROGRAM_SESSION_BASE64"


@dataclass(frozen=True)
class CloudCredentialMaterializationSummary:
    google_credentials_written: bool
    pyrogram_session_written: bool


def materialize_cloud_credentials(
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    logger: logging.Logger | None = None,
) -> CloudCredentialMaterializationSummary:
    values = env or os.environ
    google_credentials_written = _materialize_if_present(
        env=values,
        env_name=GOOGLE_CREDENTIALS_BASE64_ENV,
        destination=settings.google_credentials_file,
        label="Google credentials file",
        logger=logger,
    )
    pyrogram_session_written = _materialize_if_present(
        env=values,
        env_name=PYROGRAM_SESSION_BASE64_ENV,
        destination=settings.pyrogram_workdir / f"{settings.pyrogram_session_name}.session",
        label="Pyrogram session file",
        logger=logger,
    )
    return CloudCredentialMaterializationSummary(
        google_credentials_written=google_credentials_written,
        pyrogram_session_written=pyrogram_session_written,
    )


def _materialize_if_present(
    *,
    env: Mapping[str, str],
    env_name: str,
    destination: Path,
    label: str,
    logger: logging.Logger | None,
) -> bool:
    encoded = env.get(env_name)
    if encoded is None or encoded.strip() == "":
        return False

    decoded = _decode_base64_env_var(encoded, env_name)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(decoded)
    except OSError as exc:
        raise StartupValidationError(
            f"{label} could not be written to '{destination}'. Check persistent disk "
            "configuration and directory permissions."
        ) from exc

    if logger is not None:
        logger.info(
            "%s materialized from environment",
            label,
            extra={
                "event": "cloud_credential_materialized",
                "credential_type": label,
                "destination": str(destination),
            },
        )
    return True


def _decode_base64_env_var(value: str, env_name: str) -> bytes:
    normalized = "".join(value.split())
    try:
        return base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StartupValidationError(
            f"{env_name} is not valid Base64. Regenerate the value and update the "
            "deployment environment variable."
        ) from exc
