import hashlib
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sidecar.app.main import create_app, load_config


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    data = _build_config(tmp_path)
    config_path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("SIDECAR_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BACKUP_API_TOKEN", "super-secret")
    return config_path


@pytest.fixture
def sidecar_config(config_file):
    return load_config(config_file)


@pytest.fixture
def app(sidecar_config):
    flask_app = create_app(config=sidecar_config)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    with app.test_client() as client:
        yield client


def _build_config(tmp_path: Path, *, produce_artifact: bool = True) -> dict:
    workdir = tmp_path / "workdir"
    artifacts = tmp_path / "artifacts"
    temp_dump = tmp_path / "dump.bin"
    if produce_artifact:
        command = "printf 'payload:%s' \"$SIDE_CAR_DRIVE_FOLDER_ID\""
        capture_stdout = True
    else:
        command = "rm -f \"$SIDE_CAR_TEMP_DUMP\""
        capture_stdout = False
    return {
        "app": {"port": 9000},
        "capabilities": {
            "version": "v1",
            "types": ["filesystem"],
            "est_seconds": 45,
            "est_size": 2048,
        },
        "strategy": {
            "type": "custom",
            "artifact": {
                "filename": "my-backup.bin",
                "format": "binary",
                "content_type": "application/octet-stream",
            },
            "config": {
                "pre": ["mkdir -p \"$SIDE_CAR_ARTIFACTS_DIR\""],
                "command": command,
                "post": ["echo done"],
                "capture_stdout": capture_stdout,
            },
        },
        "paths": {
            "workdir": str(workdir),
            "artifacts": str(artifacts),
            "temp_dump": str(temp_dump),
        },
        "secrets": {"api_token": "${BACKUP_API_TOKEN}"},
    }


def test_capabilities_requires_token(client):
    response = client.get("/backup/capabilities")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Missing Bearer token"


def test_capabilities_returns_config(client):
    response = client.get(
        "/backup/capabilities",
        headers={"Authorization": "Bearer super-secret"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data == {"version": "v1", "types": ["filesystem"], "est_seconds": 45, "est_size": 2048}


def test_export_generates_artifact_and_metadata(client):
    drive_folder_id = "folder-123"
    response = client.post(
        "/backup/export",
        headers={"Authorization": "Bearer super-secret"},
        query_string={"drive_folder_id": drive_folder_id},
    )
    assert response.status_code == 200
    body = response.data
    assert body == b"payload:folder-123"
    expected_checksum = hashlib.sha256(body).hexdigest()
    assert response.headers["X-Checksum-Sha256"] == expected_checksum
    assert (
        response.headers["Content-Disposition"]
        == 'attachment; filename="my-backup.bin"'
    )
    assert response.headers["Content-Length"] == str(len(body))
    assert response.headers["X-Backup-Format"] == "binary"
    assert response.headers["X-Drive-Folder-Id"] == drive_folder_id


def test_export_missing_artifact_returns_error(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_build_config(tmp_path, produce_artifact=False)))
    monkeypatch.setenv("BACKUP_API_TOKEN", "super-secret")
    config = load_config(config_path)
    app = create_app(config=config)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/backup/export",
            headers={"Authorization": "Bearer super-secret"},
        )
    assert response.status_code == 500
    body = response.get_json()
    assert "did not generate" in body["error"]
