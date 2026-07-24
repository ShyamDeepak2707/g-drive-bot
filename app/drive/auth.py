from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from app.config import Settings
from app.exceptions import GoogleDriveError
from app.logging_config import get_logger

logger = get_logger(__name__)


def get_drive_service(settings: Settings) -> Resource | None:
    credentials = load_credentials(
        credentials_file=settings.google_credentials_file,
        token_file=settings.google_token_file,
        scopes=settings.google_scopes,
        allow_interactive_auth=settings.google_auto_auth,
    )
    if credentials is None:
        return None
    try:
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise GoogleDriveError("Failed to initialize Google Drive service.") from exc
    logger.info("google drive authenticated", extra={"event": "google_authenticated"})
    return service


def load_credentials(
    credentials_file: Path,
    token_file: Path,
    scopes: tuple[str, ...],
    allow_interactive_auth: bool,
) -> Credentials | None:
    credentials: Credentials | None = None

    if token_file.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_file), list(scopes))
        except ValueError as exc:
            raise GoogleDriveError(f"Google token file is invalid at '{token_file}'.") from exc

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise GoogleDriveError("Failed to refresh Google Drive token.") from exc
        _save_token(token_file, credentials)
        return credentials

    if not credentials_file.exists():
        logger.warning(
            "Google credentials file not found at %s; Drive authentication is pending",
            credentials_file,
        )
        return None

    if not allow_interactive_auth:
        logger.warning(
            "Google token is missing or invalid; set GOOGLE_AUTO_AUTH=true locally to authorize with %s",
            credentials_file,
        )
        return None

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), list(scopes))
    credentials = flow.run_local_server(port=0)
    _save_token(token_file, credentials)
    return credentials


def _save_token(token_file: Path, credentials: Credentials) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    logger.info("Google Drive token saved to %s", token_file)
