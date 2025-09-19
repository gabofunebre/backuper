import os
import sys
import importlib
import subprocess
from types import SimpleNamespace

import pytest

sys.path.append(os.path.dirname(os.path.dirname(__file__)))


def make_app(monkeypatch, **extra_env):
    base_env = {
        "DATABASE_URL": "sqlite://",
        "APP_ADMIN_USER": "admin",
        "APP_ADMIN_PASS": "secret",
        "APP_SECRET_KEY": "test-key",
        "RCLONE_CONFIG": "/tmp/test-rclone.conf",
    }
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    app_module = importlib.import_module("orchestrator.app")
    db_module = importlib.import_module("orchestrator.app.database")
    models_module = importlib.import_module("orchestrator.app.models")
    importlib.reload(db_module)
    importlib.reload(models_module)
    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "schedule_app_backups", lambda: None)
    app = app_module.create_app()
    app.config.update(TESTING=True)
    return app, app_module


def login(client) -> None:
    client.post("/login", data={"username": "admin", "password": "secret"})


def test_remote_options_local(monkeypatch):
    app, _ = make_app(
        monkeypatch,
        RCLONE_LOCAL_DIRECTORIES="Local A|/data/a;/data/b",
    )
    client = app.test_client()
    login(client)
    resp = client.get("/rclone/remotes/options/local")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "directories": [
            {"label": "Local A", "path": "/data/a"},
            {"label": "/data/b", "path": "/data/b"},
        ]
    }


def test_remote_options_sftp(monkeypatch):
    app, _ = make_app(monkeypatch)
    client = app.test_client()
    login(client)
    resp = client.get("/rclone/remotes/options/sftp")
    assert resp.status_code == 200
    assert resp.get_json() == {"requires_credentials": True}


def test_drive_validate_success(monkeypatch):
    app, app_module = make_app(monkeypatch)
    recorded: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    resp = client.post(
        "/rclone/remotes/drive/validate", json={"token": "token-json"}
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    cmd = recorded["cmd"]
    assert cmd[0] == "rclone"
    assert "--config" in cmd
    config_index = cmd.index("--config")
    assert cmd[config_index + 1]
    assert "config" in cmd
    assert "create" in cmd
    assert "__validate__" in cmd
    token_index = cmd.index("token")
    assert cmd[token_index + 1] == "token-json"
    assert "--no-auto-auth" in cmd
    assert "--non-interactive" in cmd
    kwargs = recorded["kwargs"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is True


def test_drive_validate_failure(monkeypatch):
    app, app_module = make_app(monkeypatch)

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="bad token")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    resp = client.post("/rclone/remotes/drive/validate", json={"token": "tok"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "bad token"}


def test_drive_validate_requires_token(monkeypatch):
    app, _ = make_app(monkeypatch)
    client = app.test_client()
    login(client)
    resp = client.post("/rclone/remotes/drive/validate", json={})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "token is required"}


def test_create_local_remote(monkeypatch):
    app, app_module = make_app(
        monkeypatch,
        RCLONE_LOCAL_DIRECTORIES="Backups|/data/backups",
    )
    recorded: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    payload = {
        "name": "local1",
        "type": "local",
        "settings": {"path": "/data/backups"},
    }
    resp = client.post("/rclone/remotes", json=payload)
    assert resp.status_code == 201
    assert resp.get_json() == {
        "status": "ok",
        "route": "/data/backups",
        "share_url": "/data/backups",
    }
    cmd = recorded["cmd"]
    assert cmd[:3] == ["rclone", "--config", "/tmp/test-rclone.conf"]
    assert "--non-interactive" in cmd
    assert "alias" in cmd
    alias_index = cmd.index("alias")
    assert cmd[alias_index + 1] == "remote"
    assert cmd[alias_index + 2] == "/data/backups"


def test_create_local_remote_invalid_path(monkeypatch):
    app, app_module = make_app(
        monkeypatch,
        RCLONE_LOCAL_DIRECTORIES="Backups|/data/backups",
    )
    called = False

    def fake_run(cmd, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    payload = {
        "name": "local2",
        "type": "local",
        "settings": {"path": "/other"},
    }
    resp = client.post("/rclone/remotes", json=payload)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid path"}
    assert called is False


def test_create_sftp_remote_success(monkeypatch):
    app, app_module = make_app(monkeypatch)
    calls: list[dict[str, object]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    payload = {
        "name": "sftp1",
        "type": "sftp",
        "settings": {
            "host": "sftp.internal",
            "port": "2222",
            "username": "backup",
            "password": "secret",
            "base_path": "/srv/backups",
        },
    }
    resp = client.post("/rclone/remotes", json=payload)
    assert resp.status_code == 201
    assert resp.get_json() == {
        "status": "ok",
        "route": "/srv/backups",
        "share_url": "/srv/backups",
    }
    assert len(calls) == 3
    create_cmd = calls[0]["cmd"]
    assert create_cmd[0] == "rclone"
    assert "--non-interactive" in create_cmd
    assert "sftp" in create_cmd
    assert "host" in create_cmd and create_cmd[create_cmd.index("host") + 1] == "sftp.internal"
    assert "user" in create_cmd and create_cmd[create_cmd.index("user") + 1] == "backup"
    assert "pass" in create_cmd and create_cmd[create_cmd.index("pass") + 1] == "secret"
    assert "port" in create_cmd and create_cmd[create_cmd.index("port") + 1] == "2222"
    path_index = create_cmd.index("path")
    assert create_cmd[path_index + 1] == "/srv/backups/sftp1"
    mkdir_cmd = calls[1]["cmd"]
    assert mkdir_cmd[-2:] == ["mkdir", "sftp1:"]
    lsd_cmd = calls[2]["cmd"]
    assert lsd_cmd[-2:] == ["lsd", "sftp1:"]


def test_create_sftp_remote_missing_credentials(monkeypatch):
    app, app_module = make_app(monkeypatch)
    called = False

    def fake_run(cmd, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    payload = {
        "name": "sftp2",
        "type": "sftp",
        "settings": {
            "host": "sftp.internal",
            "username": "backup",
        },
    }
    resp = client.post("/rclone/remotes", json=payload)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "password is required"}
    assert called is False


def test_create_sftp_remote_invalid_port(monkeypatch):
    app, app_module = make_app(monkeypatch)
    called = False

    def fake_run(cmd, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    payload = {
        "name": "sftp3",
        "type": "sftp",
        "settings": {
            "host": "sftp.internal",
            "username": "backup",
            "password": "secret",
            "port": "invalid",
        },
    }
    resp = client.post("/rclone/remotes", json=payload)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid port"}
    assert called is False


def test_create_sftp_remote_connection_failure(monkeypatch):
    app, app_module = make_app(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "config" in cmd and "create" in cmd:
            return SimpleNamespace(stdout="", stderr="")
        if cmd[-2:] == ["lsd", "sftp1:"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="auth failed")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    payload = {
        "name": "sftp1",
        "type": "sftp",
        "settings": {
            "host": "sftp.internal",
            "username": "backup",
            "password": "secret",
            "base_path": "/srv/backups",
        },
    }
    resp = client.post("/rclone/remotes", json=payload)
    assert resp.status_code == 400
    assert resp.get_json() == {
        "error": "No se pudo autenticar en el servidor SFTP. Verificá el usuario y la contraseña.",
    }
    # Ensure cleanup attempted after failure
    assert any(cmd[-3:-1] == ["config", "delete"] for cmd in calls)
