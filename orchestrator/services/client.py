import io
import os
from typing import Iterable, Optional

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


class BackupClient:
    """Client for interacting with app backup endpoints and uploading to Drive."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def check_capabilities(self) -> bool:
        """Verify that the app is ready for backup."""
        resp = requests.get(
            f"{self.base_url}/backup/capabilities", headers=self._headers(), timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("ready", False)

    def export_backup(self, app_name: str, drive_folder_id: Optional[str] = None) -> None:
        """Request backup export and upload the result to Google Drive."""
        resp = requests.post(
            f"{self.base_url}/backup/export",
            headers=self._headers(),
            stream=True,
            timeout=300,
        )
        resp.raise_for_status()
        self._upload_stream_to_drive(
            resp.iter_content(64 * 1024), f"{app_name}.bak", drive_folder_id
        )
    def _upload_stream_to_drive(
        self, chunks: Iterable[bytes], filename: str, drive_folder_id: Optional[str]
    ) -> None:
        """Upload an iterable of bytes to Google Drive using service account credentials."""
        creds = service_account.Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        drive = build("drive", "v3", credentials=creds)
        buffer = io.BytesIO()
        for chunk in chunks:
            buffer.write(chunk)
        buffer.seek(0)
        media = MediaIoBaseUpload(buffer, mimetype="application/octet-stream")
        file_metadata = {"name": filename}
        if drive_folder_id:
            file_metadata["parents"] = [drive_folder_id]
        drive.files().create(body=file_metadata, media_body=media, fields="id").execute()
