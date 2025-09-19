import os
import json
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


def test_remote_options_local_strips_quotes(monkeypatch, tmp_path):
    base_dir = tmp_path / "quoted"
    base_dir.mkdir()
    app, _ = make_app(
        monkeypatch,
        RCLONE_LOCAL_DIRECTORIES=f'"{base_dir}"',
    )
    client = app.test_client()
    login(client)
    resp = client.get("/rclone/remotes/options/local")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "directories": [
            {"label": str(base_dir), "path": str(base_dir)},
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


def test_create_local_remote(monkeypatch, tmp_path):
    base_dir = tmp_path / "backups"
    base_dir.mkdir()
    app, app_module = make_app(
        monkeypatch,
        RCLONE_LOCAL_DIRECTORIES=f"Backups|{base_dir}",
    )
    commands: list[list[str]] = []
    config_entries: dict[str, dict[str, str]] = {}

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        if len(cmd) >= 5 and cmd[3] == "config" and cmd[4] == "dump":
            return SimpleNamespace(stdout=json.dumps(config_entries), stderr="")
        if len(cmd) >= 8 and cmd[3] == "config" and cmd[4] == "create":
            name = cmd[6]
            remote_type = cmd[7]
            options: dict[str, str] = {}
            for idx in range(8, len(cmd), 2):
                if idx + 1 >= len(cmd):
                    break
                options[cmd[idx]] = cmd[idx + 1]
            config_entries[name] = {"type": remote_type, **options}
            return SimpleNamespace(stdout="", stderr="")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    payload = {
        "name": "local1",
        "type": "local",
        "settings": {"path": str(base_dir)},
    }
    resp = client.post("/rclone/remotes", json=payload)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "local1"
    expected_path = base_dir / "local1"
    assert data["route"] == str(expected_path)
    assert data["share_url"] == str(expected_path)
    assert "id" in data and isinstance(data["id"], int)
    create_cmd = next(
        cmd
        for cmd in commands
        if len(cmd) > 8
        and cmd[3] == "config"
        and cmd[4] == "create"
        and cmd[6] == "local1"
    )
    assert create_cmd[:3] == ["rclone", "--config", "/tmp/test-rclone.conf"]
    assert "--non-interactive" in create_cmd
    assert "alias" in create_cmd
    alias_index = create_cmd.index("alias")
    assert create_cmd[alias_index + 1] == "remote"
    assert create_cmd[alias_index + 2] == str(expected_path)
    from orchestrator.app.models import RcloneRemote

    with app_module.SessionLocal() as db:  # type: ignore[attr-defined]
        stored = db.query(RcloneRemote).filter_by(name="local1").one()
        assert json.loads(stored.config) == {
            "type": "alias",
            "remote": str(expected_path),
        }

def test_create_local_remote_with_quoted_directory(monkeypatch, tmp_path):
    base_dir = tmp_path / "backups"
    base_dir.mkdir()
    app, app_module = make_app(
        monkeypatch,
        RCLONE_LOCAL_DIRECTORIES=f'Backups|"{base_dir}"',
    )
    commands: list[list[str]] = []
    config_entries: dict[str, dict[str, str]] = {}

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        if len(cmd) >= 5 and cmd[3] == "config" and cmd[4] == "dump":
            return SimpleNamespace(stdout=json.dumps(config_entries), stderr="")
        if len(cmd) >= 8 and cmd[3] == "config" and cmd[4] == "create":
            name = cmd[6]
            remote_type = cmd[7]
            options: dict[str, str] = {}
            for idx in range(8, len(cmd), 2):
                if idx + 1 >= len(cmd):
                    break
                options[cmd[idx]] = cmd[idx + 1]
            config_entries[name] = {"type": remote_type, **options}
            return SimpleNamespace(stdout="", stderr="")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = app.test_client()
    login(client)
    payload = {
        "name": "local1",
        "type": "local",
        "settings": {"path": str(base_dir)},
    }
    resp = client.post("/rclone/remotes", json=payload)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "local1"
    expected_path = base_dir / "local1"
    assert data["route"] == str(expected_path)
    assert data["share_url"] == str(expected_path)
    assert "id" in data and isinstance(data["id"], int)
    create_cmd = next(
        cmd
        for cmd in commands
        if len(cmd) > 8
        and cmd[3] == "config"
        and cmd[4] == "create"
        and cmd[6] == "local1"
    )
    assert create_cmd[:3] == ["rclone", "--config", "/tmp/test-rclone.conf"]
    assert "--non-interactive" in create_cmd
    assert "alias" in create_cmd
    alias_index = create_cmd.index("alias")
    assert create_cmd[alias_index + 1] == "remote"
    assert create_cmd[alias_index + 2] == str(expected_path)
    from orchestrator.app.models import RcloneRemote

    with app_module.SessionLocal() as db:  # type: ignore[attr-defined]
        stored = db.query(RcloneRemote).filter_by(name="local1").one()
        assert json.loads(stored.config) == {
            "type": "alias",
            "remote": str(expected_path),
        }


def test_create_local_remote_invalid_path(monkeypatch):
    app, app_module = make_app(
        monkeypatch,
        RCLONE_LOCAL_DIRECTORIES="Backups|/data/backups",
    )
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
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
    config_path = "/tmp/test-rclone.conf"
    assert commands == [["rclone", "--config", config_path, "listremotes"]]


def test_create_sftp_remote_success(monkeypatch):
    app, app_module = make_app(monkeypatch)
    calls: list[dict[str, object]] = []
    config_entries: dict[str, dict[str, str]] = {}

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        if "obscure" in cmd:
            return SimpleNamespace(stdout="obscured-secret\n", stderr="")
        if len(cmd) >= 5 and cmd[3] == "config" and cmd[4] == "dump":
            return SimpleNamespace(stdout=json.dumps(config_entries), stderr="")
        if len(cmd) >= 8 and cmd[3] == "config" and cmd[4] == "create":
            name = cmd[6]
            remote_type = cmd[7]
            options: dict[str, str] = {}
            for idx in range(8, len(cmd), 2):
                if idx + 1 >= len(cmd):
                    break
                options[cmd[idx]] = cmd[idx + 1]
            config_entries[name] = {"type": remote_type, **options}
            return SimpleNamespace(stdout="", stderr="")
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
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "sftp1"
    assert data["route"] == "/srv/backups"
    assert data["share_url"] == "/srv/backups"
    assert "id" in data and isinstance(data["id"], int)

    config_path = "/tmp/test-rclone.conf"
    list_cmds = [call["cmd"] for call in calls if call["cmd"][-1] == "listremotes"]
    assert list_cmds == [["rclone", "--config", config_path, "listremotes"]]
    obscure_cmd = next(call["cmd"] for call in calls if "obscure" in call["cmd"])
    create_cmd = next(
        call["cmd"]
        for call in calls
        if call["cmd"][3:7] == ["config", "create", "--non-interactive", "sftp1"]
    )
    assert obscure_cmd[0] == "rclone"
    assert "obscure" in obscure_cmd
    assert obscure_cmd[-1] == "secret"
    assert create_cmd[0] == "rclone"
    assert "--non-interactive" in create_cmd
    assert "sftp" in create_cmd
    assert "host" in create_cmd and create_cmd[create_cmd.index("host") + 1] == "sftp.internal"
    assert "user" in create_cmd and create_cmd[create_cmd.index("user") + 1] == "backup"
    assert "pass" in create_cmd and create_cmd[create_cmd.index("pass") + 1] == "obscured-secret"
    assert "port" in create_cmd and create_cmd[create_cmd.index("port") + 1] == "2222"
    path_index = create_cmd.index("path")
    assert create_cmd[path_index + 1] == "/srv/backups/sftp1"
    mkdir_cmd = next(call["cmd"] for call in calls if call["cmd"][3:] == ["mkdir", "sftp1:"])
    lsd_cmd = next(call["cmd"] for call in calls if call["cmd"][3:] == ["lsd", "sftp1:"])
    from orchestrator.app.models import RcloneRemote

    with app_module.SessionLocal() as db:  # type: ignore[attr-defined]
        stored = db.query(RcloneRemote).filter_by(name="sftp1").one()
        assert json.loads(stored.config) == {
            "type": "sftp",
            "host": "sftp.internal",
            "user": "backup",
            "port": "2222",
            "pass": "obscured-secret",
            "path": "/srv/backups/sftp1",
        }


def test_create_sftp_remote_missing_credentials(monkeypatch):
    app, app_module = make_app(monkeypatch)
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
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
    config_path = "/tmp/test-rclone.conf"
    assert commands == [["rclone", "--config", config_path, "listremotes"]]


def test_create_sftp_remote_invalid_port(monkeypatch):
    app, app_module = make_app(monkeypatch)
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
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
    config_path = "/tmp/test-rclone.conf"
    assert commands == [["rclone", "--config", config_path, "listremotes"]]


def test_create_sftp_remote_connection_failure(monkeypatch):
    app, app_module = make_app(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "obscure" in cmd:
            return SimpleNamespace(stdout="obscured-secret\n", stderr="")
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
    config_path = "/tmp/test-rclone.conf"
    assert any(cmd == ["rclone", "--config", config_path, "listremotes"] for cmd in calls)
    assert any(cmd[-3:-1] == ["config", "delete"] for cmd in calls)
