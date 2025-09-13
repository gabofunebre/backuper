import subprocess
from types import SimpleNamespace

from orchestrator.services.client import BackupClient


def test_apply_retention_deletes_old(monkeypatch):
    client = BackupClient("http://url", "token")
    monkeypatch.setenv("RCLONE_REMOTE", "drive:")
    deleted: list[str] = []

    def fake_run(cmd, capture_output=False, text=False, check=False):
        if cmd[:2] == ["rclone", "lsl"]:
            return SimpleNamespace(
                stdout=(
                    "100 2024-01-01 00:00:00 app_20240101.bak\n"
                    "100 2024-01-02 00:00:00 app_20240102.bak\n"
                    "100 2024-01-03 00:00:00 app_20240103.bak\n"
                ),
                returncode=0,
            )
        elif cmd[:2] == ["rclone", "delete"]:
            deleted.append(cmd[2])
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError("unexpected command")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client.apply_retention("app", 2)
    assert deleted == ["drive:app_20240101.bak"]


def test_apply_retention_no_delete(monkeypatch):
    client = BackupClient("http://url", "token")
    monkeypatch.setenv("RCLONE_REMOTE", "drive:")
    deleted: list[str] = []

    def fake_run(cmd, capture_output=False, text=False, check=False):
        if cmd[:2] == ["rclone", "lsl"]:
            return SimpleNamespace(
                stdout=(
                    "100 2024-01-01 00:00:00 app_20240101.bak\n"
                    "100 2024-01-02 00:00:00 app_20240102.bak\n"
                ),
                returncode=0,
            )
        elif cmd[:2] == ["rclone", "delete"]:
            deleted.append(cmd[2])
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError("unexpected command")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client.apply_retention("app", 5)
    assert deleted == []
