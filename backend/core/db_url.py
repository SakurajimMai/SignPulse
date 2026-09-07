from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def database_dialect(url: str) -> str:
    scheme = (urlsplit(str(url or "").strip()).scheme or "").casefold()
    head = scheme.split("+", 1)[0]
    if head in {"sqlite"}:
        return "sqlite"
    if head in {"postgres", "postgresql"}:
        return "postgres"
    if head in {"mysql", "mariadb"}:
        return "mysql"
    return head or "unknown"


def is_sqlite_url(url: str) -> bool:
    return database_dialect(url) == "sqlite"


def is_mysql_url(url: str) -> bool:
    return database_dialect(url) == "mysql"


def _with_query(parsed, updates: dict[str, str]) -> str:
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in updates.items():
        query.setdefault(key, value)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def normalize_sync_url(url: str) -> str:
    """把云 MariaDB/MySQL 常见写法收成 SQLAlchemy 同步 URL。"""
    raw = str(url or "").strip()
    if not raw:
        return raw
    parsed = urlsplit(raw)
    scheme = parsed.scheme.casefold()
    if scheme in {"mysql", "mariadb", "mariadb+pymysql"}:
        parsed = parsed._replace(scheme="mysql+pymysql")
        return _with_query(parsed, {"charset": "utf8mb4"})
    if scheme == "mysql+pymysql":
        return _with_query(parsed, {"charset": "utf8mb4"})
    return raw


def normalize_async_url(url: str) -> str:
    """漫画库等异步引擎使用的 URL。"""
    raw = str(url or "").strip()
    if not raw:
        return raw
    if raw.startswith("sqlite:///"):
        return raw.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    parsed = urlsplit(raw)
    scheme = parsed.scheme.casefold()
    if scheme in {
        "mysql",
        "mariadb",
        "mysql+pymysql",
        "mariadb+pymysql",
        "mariadb+asyncmy",
    }:
        parsed = parsed._replace(scheme="mysql+asyncmy")
        return _with_query(parsed, {"charset": "utf8mb4"})
    if scheme == "mysql+asyncmy":
        return _with_query(parsed, {"charset": "utf8mb4"})
    return raw


def ensure_sync_driver(url: str) -> None:
    dialect = database_dialect(url)
    if dialect == "postgres":
        try:
            import importlib

            if "+psycopg2" in url or url.startswith("postgresql://"):
                importlib.import_module("psycopg2")
            elif "+asyncpg" in url:
                importlib.import_module("asyncpg")
        except ImportError as exc:
            raise RuntimeError(
                "APP_DATABASE_URL 指向 PostgreSQL，但当前环境未安装对应驱动。"
                "请安装 psycopg2-binary（同步）或调整 URL/驱动，"
                "或清空 APP_DATABASE_URL 回退 SQLite。"
            ) from exc
        return
    if dialect == "mysql":
        try:
            import importlib

            importlib.import_module("pymysql")
        except ImportError as exc:
            raise RuntimeError(
                "APP_DATABASE_URL 指向 MariaDB/MySQL，但当前环境未安装 PyMySQL。"
                "请安装 pymysql，或清空 APP_DATABASE_URL 回退 SQLite。"
            ) from exc


def ensure_async_driver(url: str) -> None:
    dialect = database_dialect(url)
    if dialect == "mysql":
        try:
            import importlib

            importlib.import_module("asyncmy")
        except ImportError as exc:
            raise RuntimeError(
                "MANGA_DATABASE_URL 指向 MariaDB/MySQL，但当前环境未安装 asyncmy。"
                "请安装 asyncmy，或改回 sqlite:/// 漫画库。"
            ) from exc
    elif dialect == "postgres":
        try:
            import importlib

            importlib.import_module("asyncpg")
        except ImportError as exc:
            raise RuntimeError(
                "MANGA_DATABASE_URL 指向 PostgreSQL，但当前环境未安装 asyncpg。"
            ) from exc
