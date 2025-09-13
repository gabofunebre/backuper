import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.app.models import Base, App
from orchestrator import scheduler


@pytest.fixture
def test_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield TestingSessionLocal
    Base.metadata.drop_all(bind=engine)


def test_run_backup_exports(monkeypatch, test_session):
    session = test_session()
    app = App(name="test", url="http://url", token="token", schedule="* * * * *")
    session.add(app)
    session.commit()
    app_id = app.id
    session.close()

    monkeypatch.setattr(scheduler, "SessionLocal", test_session)

    called: dict[str, object] = {}

    class DummyClient:
        def __init__(self, url: str, token: str) -> None:
            called["init"] = (url, token)

        def check_capabilities(self) -> bool:
            called["checked"] = True
            return True

        def export_backup(self, name: str) -> None:
            called["exported"] = name

    monkeypatch.setattr(scheduler, "BackupClient", DummyClient)

    scheduler.run_backup(app_id)

    assert called["init"] == (app.url, app.token)
    assert called["checked"]
    assert called["exported"] == app.name


def test_run_backup_missing_app(monkeypatch, test_session):
    monkeypatch.setattr(scheduler, "SessionLocal", test_session)

    called = {"init": False}

    class DummyClient:
        def __init__(self, url: str, token: str) -> None:
            called["init"] = True

        def check_capabilities(self) -> bool:
            return True

        def export_backup(self, name: str) -> None:  # pragma: no cover - not expected
            pass

    monkeypatch.setattr(scheduler, "BackupClient", DummyClient)

    scheduler.run_backup(999)

    assert called["init"] is False

