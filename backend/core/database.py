from __future__ import annotations

from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.core.config import get_settings
from backend.core.db_url import ensure_sync_driver, is_mysql_url

Base = declarative_base()

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def init_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None and _SessionLocal is not None:
        return

    settings = get_settings()
    url = settings.database_url
    ensure_sync_driver(url)
    connect_args = {}
    engine_kwargs: dict = {
        "echo": False,
        "pool_pre_ping": True,
    }
    if settings.is_sqlite:
        connect_args = {"check_same_thread": False, "timeout": 30}
    else:
        # 云库常见 wait_timeout，避免拿到已断开的连接。
        engine_kwargs["pool_recycle"] = 3600
        if is_mysql_url(url):
            connect_args = {"charset": "utf8mb4"}

    engine = create_engine(
        url,
        connect_args=connect_args,
        **engine_kwargs,
    )

    if settings.is_sqlite:

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    return _engine  # type: ignore[return-value]


def get_session_local() -> sessionmaker:
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal  # type: ignore[return-value]


def get_db():
    session_local = get_session_local()
    db = session_local()
    try:
        yield db
    finally:
        db.close()
