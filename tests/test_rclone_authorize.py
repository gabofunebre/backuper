import os
import sys
import subprocess

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_ADMIN_USER"] = "admin"
os.environ["APP_ADMIN_PASS"] = "secret"
os.environ["APP_SECRET_KEY"] = "test-key"

from orchestrator.app import create_app


def test_authorize_returns_url(monkeypatch):
    monkeypatch.setattr("orchestrator.app.start_scheduler", lambda: None)
    monkeypatch.setattr("orchestrator.app.authorize_drive", lambda: "http://auth")
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post("/rclone/remotes/foo/authorize", json={})
    assert resp.status_code == 200
    assert resp.get_json() == {"url": "http://auth"}


def test_authorize_updates_config(monkeypatch):
    monkeypatch.setattr("orchestrator.app.start_scheduler", lambda: None)

    def fail():
        raise AssertionError("authorize_drive should not be called")

    monkeypatch.setattr("orchestrator.app.authorize_drive", fail)
    called: dict[str, list[str]] = {}

    def fake_run(cmd, check):
        called["cmd"] = cmd

    monkeypatch.setattr(subprocess, "run", fake_run)
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post("/rclone/remotes/foo/authorize", json={"token": "tkn"})
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    assert called["cmd"] == ["rclone", "config", "update", "foo", "token", "tkn"]
