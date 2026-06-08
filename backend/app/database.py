"""Database engine / session setup (sync SQLAlchemy 2.0 over SQLite)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

_settings = get_settings()
_url = _settings.resolved_database_url
_connect_args = {"check_same_thread": False} if _url.startswith("sqlite") else {}

engine = create_engine(_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True
)


class Base(DeclarativeBase):
    pass


# New model columns added after a DB already exists.  create_all() never
# ALTERs existing tables and we run without Alembic, so add them by hand here.
# Each entry is idempotent (checked against PRAGMA table_info).
_SQLITE_COLUMN_ADDITIONS: dict[str, dict[str, str]] = {
    "tasks": {"mode": "VARCHAR(16) NOT NULL DEFAULT 'task'"},
}


def _ensure_sqlite_columns() -> None:
    if not _url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, cols in _SQLITE_COLUMN_ADDITIONS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db() -> None:
    """Create the data directory and all tables."""
    _settings.data_dir.mkdir(parents=True, exist_ok=True)
    _settings.projects_dir.mkdir(parents=True, exist_ok=True)
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standalone session for background work."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
