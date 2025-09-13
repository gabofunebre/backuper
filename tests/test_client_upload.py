import os
import subprocess
import sys
import tracemalloc

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
