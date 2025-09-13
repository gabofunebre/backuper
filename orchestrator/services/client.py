import os
import subprocess
from typing import Iterable, Optional
import requests


class BackupClient:
    """Client for interacting with app backup endpoints and uploading to Drive."""

    def __init__(self, base_url: str, token: str, upload_buffer: int = 8 * 1024 * 1024):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.upload_buffer = upload_buffer

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
        self._upload_stream_to_drive(resp.iter_content(64 * 1024), f"{app_name}.bak")

    def _upload_stream_to_drive(self, chunks: Iterable[bytes], filename: str) -> None:
        """Upload an iterable of bytes to Google Drive using rclone rcat."""
        remote = os.environ.get("RCLONE_REMOTE", "drive:")
        cmd = ["rclone", "rcat", f"{remote}{filename}"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        if proc.stdin is None:
            raise RuntimeError("Failed to open rclone stdin")
        try:
            for chunk in chunks:
                for i in range(0, len(chunk), self.upload_buffer):
                    proc.stdin.write(chunk[i : i + self.upload_buffer])
        finally:
            proc.stdin.close()
            returncode = proc.wait()
        if returncode != 0:
            raise RuntimeError(f"rclone exited with status {returncode}")

