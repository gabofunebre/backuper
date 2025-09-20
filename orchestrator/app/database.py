import os
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker, declarative_base


def _default_database_url() -> str:
    base_dir = "/sqlite/db"
    filename = "apps.db"
    path = os.path.join(base_dir, filename)
    return f"sqlite:////{path.lstrip('/')}"


DATABASE_URL = os.getenv("DATABASE_URL", _default_database_url())


def _prepare_sqlite_directory(url: str) -> None:
    try:
        parsed = make_url(url)
    except Exception:
        return
    if parsed.get_backend_name() != "sqlite":
        return
    database = parsed.database or ""
    if database in {"", ":memory:"}:
        return
    directory = os.path.dirname(os.path.abspath(database))
    if directory:
        os.makedirs(directory, exist_ok=True)


_prepare_sqlite_directory(DATABASE_URL)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()
