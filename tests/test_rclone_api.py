import os
import sys
import subprocess
import importlib
import pytest

sys.path.append(os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("APP_ADMIN_USER", "admin")
    monkeypatch.setenv("APP_ADMIN_PASS", "secret")
    monkeypatch.setenv("APP_SECRET_KEY", "test-key")
    monkeypatch.setenv("RCLONE_CONFIG", "/tmp/test-rclone.conf")
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
    yield app


def test_list_rclone_remotes(monkeypatch, app):
    calls = []

    class DummyResult:
        stdout = "gdrive:\nother:\n"

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.get("/rclone/remotes")
    assert resp.status_code == 200
    assert resp.get_json() == ["gdrive", "other"]
    config_path = os.getenv("RCLONE_CONFIG")
    assert calls == [["rclone", "--config", config_path, "listremotes"]]


def test_register_app_with_remote(monkeypatch, app):
    def fake_run(cmd, capture_output, text, check):
        class DummyResult:
            stdout = "gdrive:\n"
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    payload = {
        "name": "remoteapp",
        "url": "http://remoteapp",
        "token": "tok",
        "rclone_remote": "gdrive",
    }
    resp = client.post("/apps", json=payload)
    assert resp.status_code == 201
    resp = client.get("/apps")
    assert resp.status_code == 200
    apps = resp.get_json()
    assert any(a["name"] == "remoteapp" and a["rclone_remote"] == "gdrive:" for a in apps)


def test_list_rclone_remotes_missing_binary(monkeypatch, app):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.get("/rclone/remotes")
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "rclone is not installed"}


def test_validate_drive_token_with_custom_client(monkeypatch, app):
    calls: list[list[str]] = []

    class DummyResult:
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes/drive/validate",
        json={"token": "tok", "client_id": "cid", "client_secret": "sec"},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    cmd = calls[0]
    assert "--config" in cmd
    assert "client_id" in cmd
    assert cmd[cmd.index("client_id") + 1] == "cid"
    assert "client_secret" in cmd
    assert cmd[cmd.index("client_secret") + 1] == "sec"


def test_create_rclone_remote_missing_binary(monkeypatch, app):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {"mode": "custom", "token": "tok"},
        },
    )
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "rclone is not installed"}


def test_create_rclone_remote_unsupported_type(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post("/rclone/remotes", json={"name": "foo", "type": "s3"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "unsupported remote type"}


def test_create_rclone_remote_custom_success(monkeypatch, app):
    calls = []

    class DummyResult:
        stderr = ""
        stdout = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {
                "mode": "custom",
                "token": "tok",
                "client_id": "cid",
                "client_secret": "sec",
            },
        },
    )
    assert resp.status_code == 201
    assert resp.get_json() == {"status": "ok"}
    cmd = calls[0]
    assert cmd[0] == "rclone"
    config_path = os.getenv("RCLONE_CONFIG")
    assert "--config" in cmd
    assert cmd[cmd.index("--config") + 1] == config_path
    assert "--non-interactive" in cmd
    assert "config" in cmd
    assert "create" in cmd
    create_index = cmd.index("create")
    assert cmd[create_index - 1] == "config"
    assert cmd[create_index + 1] == "--non-interactive"
    assert cmd[create_index + 2] == "foo"
    assert "foo" in cmd
    assert "drive" in cmd
    assert "scope" in cmd
    assert "--no-auto-auth" in cmd
    assert "token" in cmd
    token_index = cmd.index("token")
    assert cmd[token_index + 1] == "tok"
    assert "client_id" in cmd
    assert cmd[cmd.index("client_id") + 1] == "cid"
    assert "client_secret" in cmd
    assert cmd[cmd.index("client_secret") + 1] == "sec"


def test_create_rclone_remote_shared_success(monkeypatch, app):
    calls: list[list[str]] = []

    class DummyResult:
        stderr = ""
        stdout = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return DummyResult()

    monkeypatch.setenv("RCLONE_REMOTE", "gdrive")
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {"mode": "shared", "email": "user@example.com"},
        },
    )
    assert resp.status_code == 201
    assert resp.get_json() == {"status": "ok"}
    assert len(calls) == 3
    config_path = os.getenv("RCLONE_CONFIG")
    mkdir_cmd, share_cmd, alias_cmd = calls
    assert mkdir_cmd[:3] == ["rclone", "--config", config_path]
    assert mkdir_cmd[3] == "mkdir"
    assert mkdir_cmd[4] == "gdrive:foo"
    assert share_cmd[:3] == ["rclone", "--config", config_path]
    assert share_cmd[3:6] == ["backend", "command", "gdrive:foo"]
    share_index = share_cmd.index("share")
    assert share_cmd[share_index + 1 : share_index + 4] == [
        "--share-with",
        "user@example.com",
        "--type",
    ]
    assert share_cmd[share_index + 4 : share_index + 6] == ["user", "--role"]
    assert share_cmd[share_index + 6] == "writer"
    assert alias_cmd[:3] == ["rclone", "--config", config_path]
    assert alias_cmd[3:9] == [
        "config",
        "create",
        "--non-interactive",
        "foo",
        "alias",
        "remote",
    ]
    assert alias_cmd[9] == "gdrive:foo"


def test_create_rclone_remote_local_success(monkeypatch, app):
    calls: list[list[str]] = []

    class DummyResult:
        stderr = ""
        stdout = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return DummyResult()

    monkeypatch.setenv("RCLONE_LOCAL_DIRECTORIES", "/backups")
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "localbackup",
            "type": "local",
            "settings": {"path": "/backups"},
        },
    )
    assert resp.status_code == 201
    assert resp.get_json() == {"status": "ok"}
    assert len(calls) == 1
    cmd = calls[0]
    config_path = os.getenv("RCLONE_CONFIG")
    assert cmd[:3] == ["rclone", "--config", config_path]
    assert cmd[3:9] == [
        "config",
        "create",
        "--non-interactive",
        "localbackup",
        "alias",
        "remote",
    ]
    assert cmd[9] == "/backups"


def test_create_rclone_remote_nested_config_path(monkeypatch, app, tmp_path):
    calls: list[list[str]] = []
    nested_config = tmp_path / "deep" / "nested" / "rclone.conf"
    default_config = tmp_path / "default" / "nested" / "rclone.conf"
    assert not nested_config.parent.exists()
    assert not default_config.parent.exists()

    class DummyResult:
        stderr = ""
        stdout = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})

    monkeypatch.setenv("RCLONE_CONFIG", str(nested_config))
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {"mode": "custom", "token": "tok"},
        },
    )
    assert resp.status_code == 201
    assert resp.get_json() == {"status": "ok"}
    assert nested_config.parent.is_dir()
    cmd = calls[-1]
    assert "--config" in cmd
    config_index = cmd.index("--config")
    assert cmd[config_index + 1] == str(nested_config)

    calls.clear()
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    app_module = importlib.import_module("orchestrator.app")
    monkeypatch.setattr(app_module, "DEFAULT_RCLONE_CONFIG", str(default_config))
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "bar",
            "type": "drive",
            "settings": {"mode": "custom", "token": "tok"},
        },
    )
    assert resp.status_code == 201
    assert resp.get_json() == {"status": "ok"}
    assert default_config.parent.is_dir()
    cmd = calls[-1]
    assert "--config" in cmd
    config_index = cmd.index("--config")
    assert cmd[config_index + 1] == str(default_config)


def test_create_rclone_remote_failure(monkeypatch, app):
    def fake_run(cmd, capture_output, text, check):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {"mode": "custom", "token": "tok"},
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "boom"}


def test_create_rclone_remote_shared_invalid_email(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {"mode": "shared", "email": "not-an-email"},
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid email"}


def test_create_rclone_remote_shared_missing_email(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={"name": "foo", "type": "drive", "settings": {"mode": "shared"}},
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "email is required"}


def test_create_rclone_remote_shared_share_failure(monkeypatch, app):
    class DummyResult:
        stderr = ""
        stdout = ""

    def fake_run(cmd, capture_output, text, check):
        if "mkdir" in cmd:
            return DummyResult()
        if "share" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="share failed")
        raise AssertionError("unexpected command execution order")

    monkeypatch.setenv("RCLONE_REMOTE", "gdrive")
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {"mode": "shared", "email": "user@example.com"},
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "share failed"}


def test_create_rclone_remote_invalid_drive_mode(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {"mode": "unknown", "token": "tok"},
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid drive mode"}
