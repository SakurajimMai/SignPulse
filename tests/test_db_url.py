from __future__ import annotations

import pytest

from backend.core.db_url import (
    database_dialect,
    ensure_async_driver,
    ensure_sync_driver,
    is_mysql_url,
    is_sqlite_url,
    normalize_async_url,
    normalize_sync_url,
)
from backend.services.manga.db import _to_async_url


def test_dialect_detection():
    assert database_dialect("sqlite:///tmp/db.sqlite") == "sqlite"
    assert database_dialect("postgresql+psycopg2://u:p@h/db") == "postgres"
    assert database_dialect("mysql://u:p@h/db") == "mysql"
    assert database_dialect("mariadb://u:p@h/db") == "mysql"
    assert is_sqlite_url("sqlite:////data/db.sqlite")
    assert is_mysql_url("mariadb+pymysql://u:p@h/db")
    assert not is_sqlite_url("mysql+pymysql://u:p@h/db")


def test_normalize_sync_mysql_variants():
    url = normalize_sync_url("mysql://app:secret@rm-xxx.mysql.rds.aliyuncs.com:3306/panel")
    assert url.startswith("mysql+pymysql://")
    assert "charset=utf8mb4" in url
    assert "rm-xxx.mysql.rds.aliyuncs.com:3306/panel" in url

    mariadb = normalize_sync_url("mariadb://app:secret@host/db")
    assert mariadb.startswith("mysql+pymysql://")
    assert "charset=utf8mb4" in mariadb

    already = normalize_sync_url(
        "mysql+pymysql://app:secret@host/db?charset=utf8mb4&ssl=true"
    )
    assert already.startswith("mysql+pymysql://")
    assert "ssl=true" in already
    assert already.count("charset=utf8mb4") == 1


def test_normalize_async_mysql_and_sqlite():
    assert _to_async_url("sqlite:////data/tg_manga.db").startswith(
        "sqlite+aiosqlite:///"
    )
    assert _to_async_url("postgresql://u:p@h/db").startswith("postgresql+asyncpg://")
    mysql = normalize_async_url("mysql://u:p@h:3306/manga")
    assert mysql.startswith("mysql+asyncmy://")
    assert "charset=utf8mb4" in mysql
    from_sync = normalize_async_url("mysql+pymysql://u:p@h/manga")
    assert from_sync.startswith("mysql+asyncmy://")


def test_ensure_sync_mysql_driver_missing(monkeypatch):
    import importlib

    original = importlib.import_module

    def fake(name, package=None):
        if name == "pymysql":
            raise ImportError("no pymysql")
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)
    with pytest.raises(RuntimeError, match="PyMySQL"):
        ensure_sync_driver("mysql+pymysql://u:p@h/db")


def test_ensure_async_mysql_driver_missing(monkeypatch):
    import importlib

    original = importlib.import_module

    def fake(name, package=None):
        if name == "asyncmy":
            raise ImportError("no asyncmy")
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)
    with pytest.raises(RuntimeError, match="asyncmy"):
        ensure_async_driver("mysql+asyncmy://u:p@h/db")
