"""Google Drive upload helper for TradingAgents PDF exports."""

from __future__ import annotations

import os
from pathlib import Path

_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _build_service():
    import json
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    # Prefer inline JSON content (safe for env vars / Render secrets)
    json_content = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON_CONTENT", "").strip()
    if json_content:
        info = json.loads(json_content)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    # Fall back to file path (local dev)
    key_path = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not key_path:
        raise RuntimeError(
            "Set either GDRIVE_SERVICE_ACCOUNT_JSON_CONTENT (JSON text) "
            "or GDRIVE_SERVICE_ACCOUNT_JSON (file path)"
        )
    resolved = Path(key_path)
    if not resolved.is_absolute():
        resolved = Path(__file__).resolve().parent.parent / key_path
    if not resolved.is_file():
        raise RuntimeError(f"Service account key not found: {resolved}")

    creds = service_account.Credentials.from_service_account_file(str(resolved), scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _get_or_create_folder(service, name: str, parent_id: str) -> str:
    """Return the Drive folder ID for `name` under `parent_id`, creating it if absent."""
    query = (
        f"name='{name}' "
        f"and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    res = service.files().list(q=query, fields="files(id,name)", spaces="drive").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def upload_pdf(pdf_path: Path, ticker: str) -> str:
    """Upload *pdf_path* to Drive under Investment/<TICKER>/, return the file URL."""
    from googleapiclient.http import MediaFileUpload

    investment_folder_id = os.getenv("GDRIVE_INVESTMENT_FOLDER_ID", "")
    if not investment_folder_id:
        raise RuntimeError("GDRIVE_INVESTMENT_FOLDER_ID is not set")

    service = _build_service()

    ticker_folder_id = _get_or_create_folder(
        service, ticker.strip().upper(), investment_folder_id
    )

    file_meta = {"name": pdf_path.name, "parents": [ticker_folder_id]}
    media = MediaFileUpload(str(pdf_path), mimetype="application/pdf", resumable=False)
    uploaded = (
        service.files()
        .create(body=file_meta, media_body=media, fields="id,webViewLink")
        .execute()
    )
    return uploaded.get("webViewLink", f"https://drive.google.com/file/d/{uploaded['id']}/view")
