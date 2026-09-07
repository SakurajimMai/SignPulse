from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from backend.core.db_url import (
    ensure_async_driver,
    is_mysql_url,
    is_sqlite_url,
    normalize_async_url,
)

from .config import MangaSettings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Manga(Base):
    __tablename__ = "mangas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_chat_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="manga",
        cascade="all, delete-orphan",
        order_by="Chapter.number",
    )


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (
        UniqueConstraint("manga_id", "number", name="uq_chapter_manga_number"),
        UniqueConstraint("source_key", name="uq_chapter_source_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    manga_id: Mapped[int] = mapped_column(ForeignKey("mangas.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Deterministic key from telegram message group to avoid re-import
    source_key: Mapped[str] = mapped_column(String(255), index=True)
    source_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    tg_album_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    tg_sent_videos: Mapped[str | None] = mapped_column(Text, nullable=True)
    tg_outbound_attempts: Mapped[int] = mapped_column(Integer, default=0)
    tg_outbound_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tg_outbound_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    site_published_page_count: Mapped[int] = mapped_column(Integer, default=0)
    site_manga_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    site_chapter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    manga: Mapped[Manga] = relationship(back_populates="chapters")
    pages: Mapped[list[Page]] = relationship(
        back_populates="chapter",
        cascade="all, delete-orphan",
        order_by="Page.index",
    )


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("chapter_id", "index", name="uq_page_chapter_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer, default=0)
    image_url: Mapped[str] = mapped_column(String(1000))
    media_kind: Mapped[str] = mapped_column(String(16), default="image")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chapter: Mapped[Chapter] = relationship(back_populates="pages")


class IngestEvent(Base):
    """Audit trail for worker activity."""

    __tablename__ = "ingest_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _to_async_url(url: str) -> str:
    return normalize_async_url(url)


def init_engine(settings: MangaSettings):
    global _engine, _session_factory
    url = _to_async_url(settings.database_url)
    ensure_async_driver(url)
    connect_args = {}
    kwargs: dict = {"echo": False}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 3600
    _engine = create_async_engine(url, connect_args=connect_args, **kwargs)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def init_db(settings: MangaSettings) -> None:
    settings.ensure_dirs()
    await close_db()
    engine = init_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if is_sqlite_url(settings.database_url):
            rows = (await conn.exec_driver_sql('PRAGMA table_info(mangas)')).all()
            columns = {str(row[1]) for row in rows}
            if 'author' not in columns:
                await conn.exec_driver_sql('ALTER TABLE mangas ADD COLUMN author VARCHAR(255)')
            if 'tags' not in columns:
                await conn.exec_driver_sql('ALTER TABLE mangas ADD COLUMN tags TEXT')
            page_rows = (await conn.exec_driver_sql('PRAGMA table_info(pages)')).all()
            page_columns = {str(row[1]) for row in page_rows}
            if 'media_kind' not in page_columns:
                await conn.exec_driver_sql("ALTER TABLE pages ADD COLUMN media_kind VARCHAR(16) DEFAULT 'image'")
            chapter_rows = (await conn.exec_driver_sql('PRAGMA table_info(chapters)')).all()
            chapter_columns = {str(row[1]) for row in chapter_rows}
            added_album = False
            added_site_progress = False
            if 'tg_album_sent' not in chapter_columns:
                await conn.exec_driver_sql(
                    'ALTER TABLE chapters ADD COLUMN tg_album_sent BOOLEAN DEFAULT 0'
                )
                added_album = True
            if 'tg_sent_videos' not in chapter_columns:
                await conn.exec_driver_sql('ALTER TABLE chapters ADD COLUMN tg_sent_videos TEXT')
            if 'tg_outbound_attempts' not in chapter_columns:
                await conn.exec_driver_sql(
                    'ALTER TABLE chapters ADD COLUMN tg_outbound_attempts INTEGER DEFAULT 0'
                )
            if 'tg_outbound_retry_at' not in chapter_columns:
                await conn.exec_driver_sql(
                    'ALTER TABLE chapters ADD COLUMN tg_outbound_retry_at DATETIME'
                )
            if 'tg_outbound_last_error' not in chapter_columns:
                await conn.exec_driver_sql(
                    'ALTER TABLE chapters ADD COLUMN tg_outbound_last_error TEXT'
                )
            if 'site_published_page_count' not in chapter_columns:
                await conn.exec_driver_sql(
                    'ALTER TABLE chapters ADD COLUMN site_published_page_count INTEGER DEFAULT 0'
                )
                added_site_progress = True
            if 'site_manga_id' not in chapter_columns:
                await conn.exec_driver_sql(
                    'ALTER TABLE chapters ADD COLUMN site_manga_id INTEGER'
                )
            if 'site_chapter_id' not in chapter_columns:
                await conn.exec_driver_sql(
                    'ALTER TABLE chapters ADD COLUMN site_chapter_id INTEGER'
                )
            if added_album:
                # 已入库章节默认视为发过，避免升级后把旧话再刷到频道
                await conn.exec_driver_sql(
                    'UPDATE chapters SET tg_album_sent=1 WHERE tg_album_sent IS NULL OR tg_album_sent=0'
                )
                await conn.exec_driver_sql(
                    """
                    UPDATE chapters SET tg_sent_videos = (
                        SELECT IFNULL(json_group_array(image_url), '[]')
                        FROM pages
                        WHERE pages.chapter_id = chapters.id
                          AND IFNULL(pages.media_kind, 'image') = 'video'
                    )
                    WHERE tg_sent_videos IS NULL OR tg_sent_videos = ''
                    """
                )
            if added_site_progress:
                # 旧章节只有在频道已发时才能确定主站曾成功，避免升级后重刷历史。
                await conn.exec_driver_sql(
                    """
                    UPDATE chapters
                    SET site_published_page_count = page_count
                    WHERE tg_album_sent=1
                    """
                )
        else:
            schema_expr = (
                "DATABASE()" if is_mysql_url(settings.database_url) else "current_schema()"
            )
            pg_album_exists = (
                await conn.exec_driver_sql(
                    f"""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = {schema_expr}
                      AND table_name = 'chapters'
                      AND column_name = 'tg_album_sent'
                    """
                )
            ).first() is not None
            pg_site_progress_exists = (
                await conn.exec_driver_sql(
                    f"""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = {schema_expr}
                      AND table_name = 'chapters'
                      AND column_name = 'site_published_page_count'
                    """
                )
            ).first() is not None
            await conn.exec_driver_sql('ALTER TABLE mangas ADD COLUMN IF NOT EXISTS author VARCHAR(255) NULL')
            await conn.exec_driver_sql('ALTER TABLE mangas ADD COLUMN IF NOT EXISTS tags TEXT NULL')
            await conn.exec_driver_sql("ALTER TABLE pages ADD COLUMN IF NOT EXISTS media_kind VARCHAR(16) DEFAULT 'image'")
            await conn.exec_driver_sql(
                'ALTER TABLE chapters ADD COLUMN IF NOT EXISTS tg_album_sent BOOLEAN DEFAULT FALSE'
            )
            await conn.exec_driver_sql(
                'ALTER TABLE chapters ADD COLUMN IF NOT EXISTS tg_sent_videos TEXT NULL'
            )
            await conn.exec_driver_sql(
                'ALTER TABLE chapters ADD COLUMN IF NOT EXISTS tg_outbound_attempts INTEGER DEFAULT 0'
            )
            await conn.exec_driver_sql(
                'ALTER TABLE chapters ADD COLUMN IF NOT EXISTS tg_outbound_retry_at TIMESTAMP NULL'
            )
            await conn.exec_driver_sql(
                'ALTER TABLE chapters ADD COLUMN IF NOT EXISTS tg_outbound_last_error TEXT NULL'
            )
            await conn.exec_driver_sql(
                'ALTER TABLE chapters ADD COLUMN IF NOT EXISTS site_published_page_count INTEGER DEFAULT 0'
            )
            await conn.exec_driver_sql(
                'ALTER TABLE chapters ADD COLUMN IF NOT EXISTS site_manga_id INTEGER NULL'
            )
            await conn.exec_driver_sql(
                'ALTER TABLE chapters ADD COLUMN IF NOT EXISTS site_chapter_id INTEGER NULL'
            )
            if not pg_album_exists:
                await conn.exec_driver_sql(
                    "UPDATE chapters SET tg_album_sent=TRUE, "
                    "tg_sent_videos=COALESCE(tg_sent_videos, '[]')"
                )
            if not pg_site_progress_exists:
                await conn.exec_driver_sql(
                    "UPDATE chapters SET site_published_page_count=page_count "
                    "WHERE tg_album_sent=TRUE"
                )


async def close_db() -> None:
    global _engine, _session_factory
    engine = _engine
    _engine = None
    _session_factory = None
    if engine is not None:
        await engine.dispose()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def get_manga_by_slug(session: AsyncSession, slug: str) -> Manga | None:
    result = await session.execute(select(Manga).where(Manga.slug == slug))
    return result.scalar_one_or_none()


async def get_manga_by_id(session: AsyncSession, manga_id: int) -> Manga | None:
    result = await session.execute(select(Manga).where(Manga.id == manga_id))
    return result.scalar_one_or_none()


async def list_mangas(
    session: AsyncSession,
    *,
    page: int = 1,
    limit: int = 24,
    q: str | None = None,
    published_only: bool = True,
    source: str | None = None,
) -> tuple[list[Manga], int]:
    page = max(1, page)
    limit = min(max(1, limit), 100)
    stmt = select(Manga)
    count_stmt = select(func.count()).select_from(Manga)
    if published_only:
        stmt = stmt.where(Manga.is_published.is_(True))
        count_stmt = count_stmt.where(Manga.is_published.is_(True))
    source_name = (source or "").strip().casefold()
    if source_name == "ehentai":
        cond = or_(Manga.source_chat_id == "ehentai", Manga.source_chat_title == "E-Hentai")
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    elif source_name in {"telegram", "channel"}:
        cond = or_(Manga.source_chat_id.is_(None), Manga.source_chat_id != "ehentai")
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Manga.title.ilike(like))
        count_stmt = count_stmt.where(Manga.title.ilike(like))
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(Manga.updated_at.desc()).offset((page - 1) * limit).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows), int(total)


async def chapter_exists(session: AsyncSession, source_key: str) -> bool:
    result = await session.execute(select(Chapter.id).where(Chapter.source_key == source_key))
    return result.scalar_one_or_none() is not None
