import importlib
import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from orchestrator import app as app_module
    from orchestrator.app import database as db_module
    from orchestrator.app import models as models_module
    importlib.reload(db_module)
    importlib.reload(models_module)
    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "schedule_app_backups", lambda: None)
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        yield client


def test_register_app_with_schedule(client):
    resp = client.post(
        "/apps",
        json={
            "name": "myapp",
            "url": "http://example",
            "token": "tok",
            "schedule": "* * * * *",
        },
    )
    assert resp.status_code == 201
    resp = client.get("/apps")
    assert resp.status_code == 200
    apps = resp.get_json()
    assert apps[0]["schedule"] == "* * * * *"


def test_register_app_invalid_schedule(client):
    resp = client.post(
        "/apps",
        json={
            "name": "bad",
            "url": "http://example",
            "token": "tok",
            "schedule": "invalid",
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid schedule"
