from __future__ import annotations

import logging
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from models import Base


SQLITE_BUSY_TIMEOUT_MS = 15_000
SQLITE_CONNECT_TIMEOUT_SECONDS = 30


logger = logging.getLogger("bamboo_ai.database")


def ensure_sqlite_parent_dir(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return

    raw_path = database_url.removeprefix("sqlite:///")
    if raw_path in {"", ":memory:"} or raw_path.startswith("file:"):
        return

    db_path = Path(raw_path)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)


def sqlite_supports_wal(database_url: str) -> bool:
    if not database_url.startswith("sqlite:///"):
        return False

    raw_path = database_url.removeprefix("sqlite:///")
    if raw_path in {"", ":memory:"} or raw_path.startswith("file:"):
        return False
    return True


def create_engine_for_url(database_url: str) -> Engine:
    ensure_sqlite_parent_dir(database_url)
    connect_args = (
        {
            "check_same_thread": False,
            "timeout": SQLITE_CONNECT_TIMEOUT_SECONDS,
        }
        if database_url.startswith("sqlite")
        else {}
    )
    engine = create_engine(database_url, future=True, connect_args=connect_args)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                if sqlite_supports_wal(database_url):
                    wal_result = cursor.execute("PRAGMA journal_mode=WAL").fetchone()
                    journal_mode = (
                        str(wal_result[0]).strip().lower()
                        if wal_result and wal_result[0] is not None
                        else ""
                    )
                    if journal_mode != "wal":
                        logger.warning(
                            "sqlite_wal_not_enabled database=%s journal_mode=%s",
                            database_url,
                            journal_mode or "unknown",
                        )
                    else:
                        cursor.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                logger.exception("sqlite_connection_pragma_failed database=%s", database_url)
                raise
            finally:
                cursor.close()

    return engine


engine = create_engine_for_url(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_database() -> None:
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
