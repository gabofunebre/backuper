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

    from orchestrator.app import SessionLocal
    from orchestrator.app.models import RcloneRemote

    with SessionLocal() as db:
        db.add(RcloneRemote(name="gdrive", type="drive", route="gdrive:backups"))
        db.commit()

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.get("/rclone/remotes")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["name"] == "gdrive"
    assert entry["type"] == "drive"
    assert entry["route"] == "gdrive:backups"
    assert "id" in entry and isinstance(entry["id"], int)
    assert "created_at" in entry
    config_path = os.getenv("RCLONE_CONFIG")
    assert calls == [["rclone", "--config", config_path, "listremotes"]]


def test_list_rclone_remotes_with_metadata(monkeypatch, app):
    class DummyResult:
        def __init__(self, stdout: str = "foo:\n") -> None:
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, capture_output, text, check):
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    from orchestrator.app import SessionLocal
    from orchestrator.app.models import RcloneRemote

    with SessionLocal() as db:
        db.add(
            RcloneRemote(
                name="foo",
                type="drive",
                route="gdrive:demo",
                share_url="https://drive.google.com/drive/folders/demo",
            )
        )
        db.commit()

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.get("/rclone/remotes")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["name"] == "foo"
    assert entry["type"] == "drive"
    assert entry["route"] == "gdrive:demo"
    assert entry["share_url"] == "https://drive.google.com/drive/folders/demo"
    assert "id" in entry and isinstance(entry["id"], int)
    assert "created_at" in entry


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
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "foo"
    assert "id" in data and isinstance(data["id"], int)
    assert "route" not in data
    assert "share_url" not in data
    assert len(calls) >= 2
    config_path = os.getenv("RCLONE_CONFIG")
    assert calls[0] == ["rclone", "--config", config_path, "listremotes"]
    cmd = calls[-1]
    assert cmd[0] == "rclone"
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


def test_create_rclone_remote_custom_retries_without_no_auto_auth(monkeypatch, app):
    calls = []

    class DummyResult:
        stderr = ""
        stdout = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(list(cmd))
        if "--no-auto-auth" in cmd:
            raise subprocess.CalledProcessError(
                1, cmd, stderr="Error: unknown flag: --no-auto-auth"
            )
        return DummyResult()

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
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "foo"
    assert "id" in data and isinstance(data["id"], int)
    assert len(calls) >= 2
    assert "--no-auto-auth" in calls[1]
    assert "--no-auto-auth" not in calls[-1]


def test_create_rclone_remote_shared_success(monkeypatch, app):
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)

        class DummyResult:
            def __init__(self, stdout: str = "", stderr: str = "") -> None:
                self.stdout = stdout
                self.stderr = stderr

        if cmd[-1] == "listremotes":
            return DummyResult(stdout="gdrive:\n")
        if "link" in cmd:
            return DummyResult(
                stdout="https://drive.google.com/drive/folders/abc123\n"
            )
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
            "settings": {"mode": "shared"},
        },
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "foo"
    assert data["route"] == "gdrive:foo"
    assert data["share_url"] == "https://drive.google.com/drive/folders/abc123"
    assert "id" in data and isinstance(data["id"], int)
    config_path = os.getenv("RCLONE_CONFIG")
    assert any(cmd == ["rclone", "--config", config_path, "listremotes"] for cmd in calls)
    mkdir_cmd = next(cmd for cmd in calls if len(cmd) > 3 and cmd[3] == "mkdir")
    assert mkdir_cmd[4] == "gdrive:foo"
    alias_cmd = next(cmd for cmd in calls if len(cmd) > 5 and cmd[3:9] == [
        "config",
        "create",
        "--non-interactive",
        "foo",
        "alias",
        "remote",
    ])
    assert alias_cmd[9] == "gdrive:foo"
    assert any(
        len(cmd) > 4 and cmd[3] == "link" and "--create-link" in cmd and "gdrive:foo" in cmd
        for cmd in calls
    )
    assert any(len(cmd) > 4 and cmd[3] == "link" and "gdrive:foo" in cmd for cmd in calls)

    from orchestrator.app import SessionLocal
    from orchestrator.app.models import RcloneRemote

    with SessionLocal() as db:
        stored = db.query(RcloneRemote).filter_by(name="foo").one()
        assert stored.type == "drive"
        assert stored.route == "gdrive:foo"
        assert stored.share_url == "https://drive.google.com/drive/folders/abc123"


def test_create_rclone_remote_local_success(monkeypatch, app, tmp_path):
    calls: list[list[str]] = []

    class DummyResult:
        stderr = ""
        stdout = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return DummyResult()

    monkeypatch.setenv("RCLONE_LOCAL_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "localbackup",
            "type": "local",
            "settings": {"path": str(tmp_path)},
        },
    )
    assert resp.status_code == 201
    expected_path = tmp_path / "localbackup"
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "localbackup"
    assert data["route"] == str(expected_path)
    assert data["share_url"] == str(expected_path)
    assert "id" in data and isinstance(data["id"], int)
    assert len(calls) >= 1
    cmd = calls[-1]
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
    assert cmd[9] == str(expected_path)
    assert expected_path.is_dir()


def test_update_rclone_remote_local_success(monkeypatch, app, tmp_path):
    commands: list[list[str]] = []

    class DummyResult:
        def __init__(self, stdout: str = "", stderr: str = "") -> None:
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, capture_output, text, check):
        commands.append(cmd)
        if cmd[-1] == "listremotes":
            return DummyResult(stdout="foo:\n")
        return DummyResult()

    monkeypatch.setenv("RCLONE_LOCAL_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", fake_run)

    base_folder = tmp_path
    target_folder = base_folder / "foo"

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.put(
        "/rclone/remotes/foo",
        json={"name": "foo", "type": "local", "settings": {"path": str(base_folder)}}
    )
    assert resp.status_code == 200
    expected_path = target_folder
    assert resp.get_json() == {
        "status": "ok",
        "route": str(expected_path),
        "share_url": str(expected_path),
        "name": "foo",
    }

    config_path = os.getenv("RCLONE_CONFIG")
    assert commands[0] == ["rclone", "--config", config_path, "listremotes"]
    assert commands[1][:6] == ["rclone", "--config", config_path, "config", "copy", "foo"]
    backup_name = commands[1][6]
    assert commands[2] == ["rclone", "--config", config_path, "config", "delete", "foo"]
    create_cmd = next(
        cmd
        for cmd in commands
        if cmd[:9]
        == [
            "rclone",
            "--config",
            config_path,
            "config",
            "create",
            "--non-interactive",
            "foo",
            "alias",
            "remote",
        ]
    )
    assert create_cmd[:9] == [
        "rclone",
        "--config",
        config_path,
        "config",
        "create",
        "--non-interactive",
        "foo",
        "alias",
        "remote",
    ]
    assert create_cmd[9] == str(expected_path)
    assert [
        "rclone",
        "--config",
        config_path,
        "config",
        "delete",
        backup_name,
    ] in commands

    from orchestrator.app import SessionLocal
    from orchestrator.app.models import RcloneRemote

    with SessionLocal() as db:
        stored = db.query(RcloneRemote).filter_by(name="foo").one()
        assert stored.type == "local"
        assert stored.route == str(expected_path)
        assert stored.share_url == str(expected_path)
    assert expected_path.is_dir()


def test_update_rclone_remote_failure_restores_backup(monkeypatch, app, tmp_path):
    commands: list[list[str]] = []

    class DummyResult:
        def __init__(self, stdout: str = "", stderr: str = "") -> None:
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, capture_output, text, check):
        commands.append(cmd)
        if cmd[-1] == "listremotes":
            return DummyResult(stdout="foo:\n")
        if cmd[3:7] == ["config", "create", "--non-interactive", "foo"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
        return DummyResult()

    monkeypatch.setenv("RCLONE_LOCAL_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", fake_run)

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.put(
        "/rclone/remotes/foo",
        json={"name": "foo", "type": "local", "settings": {"path": str(tmp_path)}}
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "boom"}

    config_path = os.getenv("RCLONE_CONFIG")
    backup_name = commands[1][6]
    assert commands[0] == ["rclone", "--config", config_path, "listremotes"]
    assert commands[1][:6] == ["rclone", "--config", config_path, "config", "copy", "foo"]
    assert commands[2] == ["rclone", "--config", config_path, "config", "delete", "foo"]
    assert commands[3][3:7] == ["config", "create", "--non-interactive", "foo"]
    assert commands[4] == ["rclone", "--config", config_path, "config", "delete", "foo"]
    assert commands[5][:7] == [
        "rclone",
        "--config",
        config_path,
        "config",
        "copy",
        backup_name,
        "foo",
    ]
    assert commands[6] == [
        "rclone",
        "--config",
        config_path,
        "config",
        "delete",
        backup_name,
    ]
    assert not (tmp_path / "foo").exists()


def test_update_rclone_remote_not_found(monkeypatch, app):
    class DummyResult:
        def __init__(self, stdout: str = "", stderr: str = "") -> None:
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, capture_output, text, check):
        if cmd[-1] == "listremotes":
            return DummyResult(stdout="other:\n")
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.put(
        "/rclone/remotes/foo",
        json={"name": "foo", "type": "local", "settings": {"path": "/datos"}},
    )
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "remote not found"}


def test_delete_rclone_remote_success(monkeypatch, app):
    commands: list[list[str]] = []

    class DummyResult:
        def __init__(self, stdout: str = "", stderr: str = "") -> None:
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, capture_output, text, check):
        commands.append(cmd)
        if cmd[-1] == "listremotes":
            return DummyResult(stdout="foo:\n")
        return DummyResult()

    from orchestrator.app import SessionLocal
    from orchestrator.app.models import RcloneRemote

    with SessionLocal() as db:
        db.add(RcloneRemote(name="foo", type="drive", route="gdrive:foo", share_url="https://demo"))
        db.commit()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.delete("/rclone/remotes/foo")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}

    config_path = os.getenv("RCLONE_CONFIG")
    assert commands[0] == ["rclone", "--config", config_path, "listremotes"]
    assert commands[1][:6] == ["rclone", "--config", config_path, "config", "copy", "foo"]
    backup_name = commands[1][6]
    assert commands[2][:4] == ["rclone", "--config", config_path, "moveto"]
    assert commands[3] == ["rclone", "--config", config_path, "config", "delete", "foo"]
    assert commands[4][:3] == ["rclone", "--config", config_path]
    assert commands[4][3] == "purge"
    assert commands[5] == ["rclone", "--config", config_path, "config", "delete", backup_name]

    with SessionLocal() as db:
        assert db.query(RcloneRemote).filter_by(name="foo").count() == 0


def test_delete_rclone_remote_local_removes_folder(monkeypatch, app, tmp_path):
    commands: list[list[str]] = []

    class DummyResult:
        def __init__(self, stdout: str = "", stderr: str = "") -> None:
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, capture_output, text, check):
        commands.append(cmd)
        if cmd[-1] == "listremotes":
            return DummyResult(stdout="foo:\n")
        return DummyResult()

    base_folder = tmp_path
    remote_folder = base_folder / "foo"
    remote_folder.mkdir()
    monkeypatch.setenv("RCLONE_LOCAL_DIRECTORIES", str(base_folder))
    monkeypatch.setattr(subprocess, "run", fake_run)

    from orchestrator.app import SessionLocal
    from orchestrator.app.models import RcloneRemote, App

    with SessionLocal() as db:
        db.add(
            RcloneRemote(
                name="foo",
                type="local",
                route=str(remote_folder),
                share_url=str(remote_folder),
            )
        )
        db.add(
            App(
                name="demo",
                url="http://demo",
                token="tok",
                rclone_remote="foo:",
            )
        )
        db.commit()

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.delete("/rclone/remotes/foo")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "removed_path": str(remote_folder)}

    config_path = os.getenv("RCLONE_CONFIG")
    assert commands[0] == ["rclone", "--config", config_path, "listremotes"]
    copy_cmd = commands[1]
    assert copy_cmd[:6] == ["rclone", "--config", config_path, "config", "copy", "foo"]
    backup_name = copy_cmd[6]
    assert commands[2] == ["rclone", "--config", config_path, "config", "delete", "foo"]
    assert commands[3] == ["rclone", "--config", config_path, "config", "delete", backup_name]

    assert not remote_folder.exists()

    with SessionLocal() as db:
        assert db.query(RcloneRemote).filter_by(name="foo").count() == 0
        app_entry = db.query(App).filter_by(name="demo").one()
        assert app_entry.rclone_remote is None


def test_delete_rclone_remote_not_found(monkeypatch, app):
    class DummyResult:
        stdout = "other:\n"
        stderr = ""

    def fake_run(cmd, capture_output, text, check):
        if cmd[-1] == "listremotes":
            return DummyResult()
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.delete("/rclone/remotes/foo")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "remote not found"}


def test_browse_sftp_directories_success(monkeypatch, app):
    calls: list[list[str]] = []

    class DummyResult:
        def __init__(self, stdout: str = ""):
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        if "obscure" in cmd:
            return DummyResult("obscured-pass\n")
        if "lsjson" in cmd:
            return DummyResult('[{"Name": "backups"}, {"Name": "logs"}]')
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes/sftp/browse",
        json={"host": "example.com", "username": "user", "password": "pass"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["current_path"] == "/"
    assert data["parent_path"] == "/"
    assert data["directories"] == [
        {"name": "backups", "path": "/backups"},
        {"name": "logs", "path": "/logs"},
    ]
    assert len(calls) == 3
    obscure_cmd, config_cmd, lsjson_cmd = calls
    assert obscure_cmd[0] == "rclone"
    assert "obscure" in obscure_cmd
    assert obscure_cmd[-1] == "pass"
    assert config_cmd[0] == "rclone"
    assert config_cmd[3:7] == ["config", "create", "--non-interactive", "__probe__"]
    assert "sftp" in config_cmd
    assert config_cmd[config_cmd.index("host") + 1] == "example.com"
    assert config_cmd[config_cmd.index("user") + 1] == "user"
    assert config_cmd[config_cmd.index("pass") + 1] == "obscured-pass"
    assert lsjson_cmd[0] == "rclone"
    assert lsjson_cmd[3] == "lsjson"
    assert lsjson_cmd[4] == "__probe__:"
    assert "--dirs-only" in lsjson_cmd


def test_browse_sftp_directories_permission_error(monkeypatch, app):
    class DummyResult:
        def __init__(self, stdout: str = "", stderr: str = ""):
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, capture_output, text, check):
        if "obscure" in cmd:
            return DummyResult(stdout="obscured-pass\n")
        if "lsjson" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="permission denied")
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes/sftp/browse",
        json={"host": "example.com", "username": "user", "password": "pass"},
    )
    assert resp.status_code == 400
    assert resp.get_json() == {
        "error": "El usuario SFTP no tiene permisos suficientes en esa carpeta. Probá con otra ubicación o ajustá los permisos en el servidor.",
    }


def test_create_sftp_remote_requires_base_path(monkeypatch, app):
    class DummyResult:
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: DummyResult())
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "sftpbackup",
            "type": "sftp",
            "settings": {"host": "example.com", "username": "user", "password": "pass"},
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {
        "error": "Seleccioná la carpeta del servidor SFTP donde se crearán los respaldos.",
    }


def test_create_sftp_remote_success(monkeypatch, app):
    calls: list[list[str]] = []

    class DummyResult:
        def __init__(self, stdout: str = ""):
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        if "obscure" in cmd:
            return DummyResult("obscured-pass\n")
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "sftpbackup",
            "type": "sftp",
            "settings": {
                "host": "example.com",
                "username": "user",
                "password": "pass",
                "base_path": "/data",
            },
        },
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "sftpbackup"
    assert data["route"] == "/data"
    assert data["share_url"] == "/data"
    assert "id" in data and isinstance(data["id"], int)

    config_path = os.getenv("RCLONE_CONFIG")
    list_cmd = next(cmd for cmd in calls if cmd[-1] == "listremotes")
    assert list_cmd == ["rclone", "--config", config_path, "listremotes"]
    obscure_cmd = next(cmd for cmd in calls if "obscure" in cmd)
    assert obscure_cmd[0] == "rclone"
    assert obscure_cmd[-1] == "pass"
    config_cmd = next(
        cmd for cmd in calls if cmd[3:7] == ["config", "create", "--non-interactive", "sftpbackup"]
    )
    path_index = config_cmd.index("path")
    assert config_cmd[path_index + 1] == "/data/sftpbackup"
    assert config_cmd[config_cmd.index("pass") + 1] == "obscured-pass"
    mkdir_cmd = next(cmd for cmd in calls if cmd[3:] == ["mkdir", "sftpbackup:"])
    lsd_cmd = next(cmd for cmd in calls if cmd[3:] == ["lsd", "sftpbackup:"])
    assert mkdir_cmd[0] == "rclone"
    assert lsd_cmd[0] == "rclone"


def test_create_sftp_remote_permission_error(monkeypatch, app):
    calls: list[list[str]] = []

    class DummyResult:
        def __init__(self, stdout: str = ""):
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        if "obscure" in cmd:
            return DummyResult("obscured-pass\n")
        if "mkdir" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="permission denied")
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "sftpbackup",
            "type": "sftp",
            "settings": {
                "host": "example.com",
                "username": "user",
                "password": "pass",
                "base_path": "/data",
            },
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {
        "error": "El usuario SFTP no tiene permisos suficientes en esa carpeta. Probá con otra ubicación o ajustá los permisos en el servidor.",
    }
    config_path = os.getenv("RCLONE_CONFIG")
    assert any(cmd == ["rclone", "--config", config_path, "listremotes"] for cmd in calls)
    delete_cmd = next(cmd for cmd in calls if cmd[3:6] == ["config", "delete", "sftpbackup"])
    assert delete_cmd[:3] == ["rclone", "--config", config_path]


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
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "foo"
    assert "id" in data and isinstance(data["id"], int)
    assert "route" not in data
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
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "bar"
    assert "id" in data and isinstance(data["id"], int)
    assert "route" not in data
    assert default_config.parent.is_dir()
    cmd = calls[-1]
    assert "--config" in cmd
    config_index = cmd.index("--config")
    assert cmd[config_index + 1] == str(default_config)


def test_create_rclone_remote_failure(monkeypatch, app):
    calls: list[list[str]] = []

    class DummyResult:
        def __init__(self, stdout: str = "", stderr: str = "") -> None:
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        if cmd[-1] == "listremotes":
            return DummyResult(stdout="")
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
    config_path = os.getenv("RCLONE_CONFIG")
    assert calls[0] == ["rclone", "--config", config_path, "listremotes"]


def test_create_rclone_remote_shared_share_failure(monkeypatch, app):
    def fake_run(cmd, capture_output, text, check):
        class DummyResult:
            def __init__(self, stdout: str = "", stderr: str = "") -> None:
                self.stdout = stdout
                self.stderr = stderr

        if cmd[-1] == "listremotes":
            return DummyResult(stdout="gdrive:\n")
        if len(cmd) > 3 and cmd[3] == "lsf":
            assert "--dir-only" not in cmd
            assert any(flag in cmd for flag in ("--dirs-only", "--files-only"))
            return DummyResult(stdout="")
        if "mkdir" in cmd:
            return DummyResult()
        if "config" in cmd and "alias" in cmd:
            return DummyResult()
        if "link" in cmd:
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
            "settings": {"mode": "shared"},
        },
    )
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "share failed"}


def test_create_rclone_remote_shared_missing_share_url(monkeypatch, app):
    def fake_run(cmd, capture_output, text, check):
        class DummyResult:
            def __init__(self, stdout: str = "", stderr: str = "") -> None:
                self.stdout = stdout
                self.stderr = stderr

        if cmd[-1] == "listremotes":
            return DummyResult(stdout="gdrive:\n")
        if len(cmd) > 3 and cmd[3] == "lsf":
            assert "--dir-only" not in cmd
            assert any(flag in cmd for flag in ("--dirs-only", "--files-only"))
            return DummyResult(stdout="")
        if "mkdir" in cmd:
            return DummyResult()
        if "config" in cmd and "alias" in cmd:
            return DummyResult()
        if "link" in cmd:
            return DummyResult(stdout="\n")
        raise AssertionError("unexpected command execution order")

    monkeypatch.setenv("RCLONE_REMOTE", "gdrive")
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={"name": "foo", "type": "drive", "settings": {"mode": "shared"}},
    )
    assert resp.status_code == 500
    assert resp.get_json() == {
        "error": "No se pudo generar el enlace compartido de Google Drive.",
    }


def test_create_rclone_remote_invalid_drive_mode(monkeypatch, app):
    class DummyResult:
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: DummyResult())
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


def test_create_rclone_remote_shared_bootstrap_default_remote(monkeypatch, app):
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)

        class DummyResult:
            def __init__(self, stdout: str = "", stderr: str = "") -> None:
                self.stdout = stdout
                self.stderr = stderr

        if cmd[-1] == "listremotes":
            return DummyResult(stdout="")
        if "link" in cmd:
            return DummyResult(
                stdout="https://drive.google.com/drive/folders/new\n"
            )
        return DummyResult()

    monkeypatch.setenv("RCLONE_REMOTE", "gdrive")
    monkeypatch.setenv("RCLONE_DRIVE_CLIENT_ID", "cid")
    monkeypatch.setenv("RCLONE_DRIVE_CLIENT_SECRET", "sec")
    monkeypatch.setenv("RCLONE_DRIVE_TOKEN", "token-json")
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.post(
        "/rclone/remotes",
        json={
            "name": "foo",
            "type": "drive",
            "settings": {"mode": "shared"},
        },
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["name"] == "foo"
    assert data["route"] == "gdrive:foo"
    assert data["share_url"] == "https://drive.google.com/drive/folders/new"
    assert "id" in data and isinstance(data["id"], int)

    config_path = os.getenv("RCLONE_CONFIG")
    list_calls = [cmd for cmd in calls if cmd == ["rclone", "--config", config_path, "listremotes"]]
    assert len(list_calls) >= 2
    default_create = next(
        cmd
        for cmd in calls
        if cmd[:8]
        == [
            "rclone",
            "--config",
            config_path,
            "config",
            "create",
            "--non-interactive",
            "gdrive",
            "drive",
        ]
    )
    assert default_create[:5] == [
        "rclone",
        "--config",
        config_path,
        "config",
        "create",
    ]
    assert default_create[5] == "--non-interactive"
    assert default_create[6] == "gdrive"
    assert default_create[7] == "drive"
    assert "token" in default_create
    token_index = default_create.index("token")
    assert default_create[token_index + 1] == "token-json"
    assert "client_id" in default_create
    assert default_create[default_create.index("client_id") + 1] == "cid"
    assert "client_secret" in default_create
    assert default_create[default_create.index("client_secret") + 1] == "sec"
    mkdir_cmd = next(cmd for cmd in calls if len(cmd) > 3 and cmd[3] == "mkdir")
    alias_cmd = next(
        cmd for cmd in calls if cmd[3:9] == [
            "config",
            "create",
            "--non-interactive",
            "foo",
            "alias",
            "remote",
        ]
    )
    assert alias_cmd[9] == "gdrive:foo"
    link_cmd = next(cmd for cmd in calls if len(cmd) > 3 and cmd[3] == "link")
    assert link_cmd[:3] == ["rclone", "--config", config_path]
    assert "gdrive:foo" in link_cmd


def test_create_rclone_remote_shared_missing_default_remote(monkeypatch, app):
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)

        class DummyResult:
            def __init__(self, stdout: str = "", stderr: str = "") -> None:
                self.stdout = stdout
                self.stderr = stderr

        if cmd[-1] == "listremotes":
            return DummyResult(stdout="")
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
            "settings": {"mode": "shared"},
        },
    )
    assert resp.status_code == 500
    assert resp.get_json() == {
        "error": "La cuenta global de Google Drive no está configurada. Revisá las variables RCLONE_DRIVE_CLIENT_ID, RCLONE_DRIVE_CLIENT_SECRET y RCLONE_DRIVE_TOKEN.",
    }
    config_path = os.getenv("RCLONE_CONFIG")
    assert sum(1 for cmd in calls if cmd == ["rclone", "--config", config_path, "listremotes"]) == 2
