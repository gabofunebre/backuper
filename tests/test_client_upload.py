import os
import subprocess
import sys
import tracemalloc
import requests

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from orchestrator.services.client import BackupClient


def test_upload_stream_large_file_memory(monkeypatch):
    client = BackupClient("http://example", "token")

    written_sizes = []

    class DummyStdin:
        def write(self, data):
            written_sizes.append(len(data))
        def close(self):
            pass

    class DummyProcess:
        def __init__(self):
            self.stdin = DummyStdin()
        def wait(self):
            return 0

    def fake_popen(cmd, stdin, **kwargs):
        assert cmd[:2] == ["rclone", "rcat"]
        assert stdin == subprocess.PIPE
        return DummyProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    def big_generator():
        for _ in range(50):  # 50 MB total
            yield b"x" * (1024 * 1024)

    tracemalloc.start()
    client._upload_stream_to_drive(big_generator(), "big.bak")
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 10 * 1024 * 1024  # peak memory under 10MB
    assert sum(written_sizes) == 50 * 1024 * 1024
    assert max(written_sizes) <= client.upload_buffer


def test_upload_stream_remote_path(monkeypatch):
    client = BackupClient("http://example", "token")
    cmds: list[list[str]] = []

    class DummyStdin:
        def write(self, data):
            pass

        def close(self):
            pass

    class DummyProcess:
        def __init__(self):
            self.stdin = DummyStdin()

        def wait(self):
            return 0

    def fake_popen(cmd, stdin, **kwargs):
        cmds.append(cmd)
        return DummyProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    client._upload_stream_to_drive([b"x"], "test.bak", "folder")
    assert cmds[-1][2] == "drive:folder/test.bak"

    monkeypatch.setenv("RCLONE_REMOTE", "drive:base/")
    client._upload_stream_to_drive([b"x"], "root.bak")
    assert cmds[-1][2] == "drive:base/root.bak"


def test_export_backup_passes_folder(monkeypatch):
    client = BackupClient("http://example", "token")

    def fake_post(url, headers, stream, timeout):
        class Resp:
            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"data"

        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)

    called = {}

    def fake_upload(chunks, filename, drive_folder_id):
        called["filename"] = filename
        called["drive_folder_id"] = drive_folder_id

    monkeypatch.setattr(client, "_upload_stream_to_drive", fake_upload)

    client.export_backup("myapp", "folderX")

    assert called["filename"] == "myapp.bak"
    assert called["drive_folder_id"] == "folderX"
