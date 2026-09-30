import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _path(env_name: str, default: str) -> Path:
    p = Path(os.getenv(env_name, default)).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def client_secrets_path() -> Path:
    return _path("GOOGLE_CLIENT_SECRETS_FILE", ".local/google/client_secret.json")


def token_path(source: str) -> Path:
    if source == "calendar":
        return _path("GOOGLE_CALENDAR_TOKEN_FILE", ".local/google/calendar_token.json")
    if source == "drive":
        return _path("GOOGLE_DRIVE_TOKEN_FILE", ".local/google/drive_token.json")
    raise ValueError("source must be calendar or drive")


def scope_for(source: str) -> str:
    if source == "calendar":
        return CALENDAR_SCOPE
    if source == "drive":
        return DRIVE_SCOPE
    raise ValueError("source must be calendar or drive")


def _save_token(path: Path, credentials: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json())
    path.chmod(0o600)


def load_credentials(source: str) -> Credentials:
    """Use an existing OAuth token; MCP tool calls never launch a browser."""
    path = token_path(source)
    if not path.exists():
        raise RuntimeError(f"Google {source} is not authenticated. Run: python authenticate_google.py {source}")
    credentials = Credentials.from_authorized_user_file(str(path), [scope_for(source)])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _save_token(path, credentials)
    if not credentials.valid:
        raise RuntimeError(f"Google {source} token is invalid. Run: python authenticate_google.py {source}")
    return credentials


def authenticate(source: str) -> None:
    """Run the standard installed-app browser flow once from the local terminal."""
    secrets = client_secrets_path()
    if not secrets.exists():
        raise RuntimeError(f"OAuth client secrets file is missing: {secrets}. Create a Desktop OAuth client in Google Cloud and save its downloaded JSON here.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), [scope_for(source)])
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    _save_token(token_path(source), credentials)
