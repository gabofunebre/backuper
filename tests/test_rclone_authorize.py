import os
import queue
import sys
from types import SimpleNamespace

import pytest

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_ADMIN_USER"] = "admin"
os.environ["APP_ADMIN_PASS"] = "secret"
os.environ["APP_SECRET_KEY"] = "test-key"

from orchestrator.app import create_app
from orchestrator.services import rclone as rclone_service


class FakeStdout:
    def __init__(self, proc: "FakeProcess") -> None:
        self.proc = proc

    def readline(self) -> str:
        try:
            return self.proc.stdout_queue.get(timeout=0.05)
        except queue.Empty:
            return ""

    def close(self) -> None:  # pragma: no cover - nothing to clean
        pass


class FakeStdin:
    def __init__(self, proc: "FakeProcess") -> None:
        self.proc = proc
        self.writes: list[str] = []

    def write(self, data: str) -> int:
        self.writes.append(data)
        if data.endswith("\n") and not self.proc.json_sent:
            self.proc.stdout_queue.put(self.proc.token_json + "\n")
            self.proc.json_sent = True
            self.proc.returncode = 0
        return len(data)

    def flush(self) -> None:  # pragma: no cover - nothing to flush
        pass

    def close(self) -> None:  # pragma: no cover - nothing to clean
        pass


class FakeProcess:
    def __init__(self, url: str, token_json: str) -> None:
        self.url = url
        self.token_json = token_json
        self.stdout_queue: "queue.Queue[str]" = queue.Queue()
        self.stdout_queue.put(f"Visit {url}\n")
        self.stdout = FakeStdout(self)
        self.stdin = FakeStdin(self)
        self.returncode: int | None = None
        self.json_sent = False
        self.command: list[str] | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:  # pragma: no cover - not triggered in tests
        self.returncode = -15

    def kill(self) -> None:  # pragma: no cover - not triggered in tests
        self.returncode = -9


@pytest.fixture(autouse=True)
def clear_sessions() -> None:
    rclone_service._AUTH_SESSIONS.clear()
    yield
    rclone_service._AUTH_SESSIONS.clear()


def login(client) -> None:
    client.post("/login", data={"username": "admin", "password": "secret"})


def test_authorize_returns_url_and_session(monkeypatch):
    monkeypatch.setattr("orchestrator.app.start_scheduler", lambda: None)
    fake_proc = FakeProcess("http://auth", "{\"token\": \"value\"}")

    def fake_popen(cmd, **kwargs):
        fake_proc.command = cmd
        return fake_proc

    monkeypatch.setattr(rclone_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        rclone_service.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="session123"),
    )
    app = create_app()
    client = app.test_client()
    login(client)
    resp = client.get("/rclone/remotes/foo/authorize")
    assert resp.status_code == 200
    assert resp.get_json() == {"url": "http://auth", "session_id": "session123"}
    assert fake_proc.command == [
        "rclone",
        "authorize",
        "drive",
        "--auth-no-open-browser",
        "--manual",
    ]
    session = rclone_service.get_authorization_session("session123")
    assert session is not None
    assert session.process is fake_proc


def test_authorize_flow_updates_remote(monkeypatch):
    monkeypatch.setattr("orchestrator.app.start_scheduler", lambda: None)
    fake_proc = FakeProcess("http://auth", "{\"access_token\": \"abc\"}")

    def fake_popen(cmd, **kwargs):
        fake_proc.command = cmd
        return fake_proc

    monkeypatch.setattr(rclone_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        rclone_service.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="session456"),
    )
    recorded: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("orchestrator.app.subprocess.run", fake_run)
    app = create_app()
    client = app.test_client()
    login(client)

    resp = client.get("/rclone/remotes/foo/authorize")
    session_id = resp.get_json()["session_id"]

    resp = client.post(
        "/rclone/remotes/foo/authorize",
        json={"session_id": session_id, "code": "the-code"},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    assert fake_proc.stdin.writes[-1] == "the-code\n"
    assert recorded["cmd"] == [
        "rclone",
        "--config",
        "/config/rclone/rclone.conf",
        "config",
        "update",
        "foo",
        "token",
        "{\"access_token\": \"abc\"}",
    ]
    assert recorded["kwargs"]["check"] is True
    assert recorded["kwargs"]["capture_output"] is True
    assert recorded["kwargs"]["text"] is True
    assert rclone_service.get_authorization_session(session_id) is None
