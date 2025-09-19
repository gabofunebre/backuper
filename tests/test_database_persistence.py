import importlib
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url


@pytest.fixture
def reload_database(monkeypatch):
    module_name = "orchestrator.app.database"

    def _reload(**env):
        existing = sys.modules.pop(module_name, None)
        if existing is not None:
            engine = getattr(existing, "engine", None)
            if engine is not None:
                engine.dispose()
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        module = importlib.import_module(module_name)
        return module

    yield _reload

    existing = sys.modules.pop(module_name, None)
    if existing is not None:
        engine = getattr(existing, "engine", None)
        if engine is not None:
            engine.dispose()
    importlib.import_module(module_name)


def test_default_database_url_points_to_persistent_volume(reload_database):
    database = reload_database(DATABASE_URL=None)
    url = database.DATABASE_URL
    parsed = make_url(url)

    assert parsed.get_backend_name() == "sqlite"
    assert parsed.database == "/datosPersistentes/db/apps.db"


def test_existing_sqlite_database_is_reused(tmp_path, reload_database):
    db_path = tmp_path / "persist" / "apps.db"
    url = f"sqlite:///{db_path}"

    database = reload_database(DATABASE_URL=url)
    with database.engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS persisted (id INTEGER PRIMARY KEY, value TEXT)"))
        conn.execute(text("INSERT INTO persisted (value) VALUES ('keep')"))

    database.engine.dispose()

    database = reload_database(DATABASE_URL=url)
    with database.engine.connect() as conn:
        result = conn.execute(text("SELECT value FROM persisted"))
        values = [row[0] for row in result]

    assert values == ["keep"]
    assert db_path.exists()
