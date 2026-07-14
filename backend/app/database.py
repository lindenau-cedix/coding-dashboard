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
    "tasks": {
        "mode": "VARCHAR(16) NOT NULL DEFAULT 'task'",
        "model": "VARCHAR(128) NOT NULL DEFAULT ''",
        "effort": "VARCHAR(32) NOT NULL DEFAULT ''",
        "images": "TEXT NOT NULL DEFAULT ''",
        "is_session": "BOOLEAN NOT NULL DEFAULT 0",
        "chat_history": "TEXT NOT NULL DEFAULT ''",
        "merge_state": "VARCHAR(32) NOT NULL DEFAULT ''",
        "workdir": "VARCHAR(1024) NOT NULL DEFAULT ''",
        "heartbeat_spawned": "BOOLEAN NOT NULL DEFAULT 0",
        "heartbeat_issue_number": "INTEGER NULL",
        # Stamps set by ``HeartbeatFollowup.maybe_run`` after it
        # successfully posts the dashboard's status comment on the
        # issue / closes the issue on a clean merge. NULL until then.
        "heartbeat_commented_at": "TIMESTAMP NULL",
        "heartbeat_closed_at": "TIMESTAMP NULL",
        # Per-task env-profile + runner selection. Empty = today's
        # behaviour (no env injection, in-container agent).
        "env_profile_key": "VARCHAR(64) NOT NULL DEFAULT ''",
        "runner": "VARCHAR(16) NOT NULL DEFAULT ''",
    },
    "projects": {
        "archived": "BOOLEAN NOT NULL DEFAULT 0",
        "archived_at": "TIMESTAMP NULL",
        "heartbeat_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "last_heartbeat_at": "TIMESTAMP NULL",
        "last_issue_poll_at": "TIMESTAMP NULL",
        "last_heartbeat_status": "VARCHAR(32) NOT NULL DEFAULT ''",
        "last_heartbeat_error": "TEXT NOT NULL DEFAULT ''",
        # Per-project override for the heartbeat's env profile. Empty =
        # fall through to the global ``CD_HEARTBEAT_ENV_PROFILE_KEY``.
        "heartbeat_env_profile_key": "VARCHAR(64) NOT NULL DEFAULT ''",
    },
    "heartbeat_seen": {
        # Comment-back state: ``last_comment_id`` is the GitHub-side
        # comment id (overwrite via PATCH from the "Re-comment" route);
        # ``last_comment_error`` is set when a comment attempt FAILED
        # so the UI can surface "github error" without losing the
        # successful later attempt (which overwrites the row).
        "last_comment_id": "INTEGER NULL",
        "last_commented_at": "TIMESTAMP NULL",
        "last_comment_error": "TEXT NOT NULL DEFAULT ''",
        "last_comment_url": "VARCHAR(512) NOT NULL DEFAULT ''",
        "last_issue_state": "VARCHAR(16) NOT NULL DEFAULT ''",
        "last_issue_state_changed_at": "TIMESTAMP NULL",
    },
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
    _seed_default_env_profile()


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


def _seed_default_env_profile() -> None:
    """Insert a ``default-anthropic`` profile row if the table is empty.

    Just a placeholder so the UI dropdown isn't empty on a fresh install;
    the operator still has to paste a token (or set a base_url) for it to
    actually do anything.  Idempotent — every call is a no-op unless the
    table is freshly empty.
    """
    from .models import EnvProfile  # avoid circular import

    with session_scope() as db:
        if db.query(EnvProfile).count() > 0:
            return
        db.add(
            EnvProfile(
                key="default-anthropic",
                name="Standard (Anthropic)",
                anthropic_base_url="",
                anthropic_auth_token_encrypted="",
            )
        )
