from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.services.manga.config import MangaSettings
from backend.services.manga.db import (
    Chapter,
    Page,
    close_db,
    get_session_factory,
    init_db,
    utcnow,
)
from backend.services.manga.publisher import (
    OUTBOUND_MAX_ATTEMPTS,
    list_chapter_image_urls,
    list_chapter_videos,
    list_pending_reply_outbound,
    load_stored_outbound,
    mark_outbound_failed,
    mark_outbound_sent,
    mark_site_publish_succeeded,
    publish_chapter,
    read_outbound_state,
)


@pytest.mark.asyncio
async def test_same_source_key_appends_new_pages(tmp_path: Path):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
    )
    await init_db(settings)
    try:
        factory = get_session_factory()
        async with factory() as session:
            first = await publish_chapter(
                session,
                title="1-5 纯爱IF线",
                chapter_title="第 1 话",
                source_key="123:r:99",
                source_chat_id="123",
                source_chat_title="3D漫画 聊天",
                image_urls=["https://img.test/1.jpg", "https://img.test/2.jpg"],
                message_ids=[11, 12],
            )
            assert first is not None
            assert first.page_count == 2
            assert first.created is True

        async with factory() as session:
            second = await publish_chapter(
                session,
                title="1-5 纯爱IF线",
                chapter_title="第 1 话",
                source_key="123:r:99",
                source_chat_id="123",
                source_chat_title="3D漫画 聊天",
                image_urls=["https://img.test/2.jpg", "https://img.test/3.jpg"],
                message_ids=[12, 13],
            )
            assert second is not None
            assert second.chapter_id == first.chapter_id
            assert second.page_count == 3
            assert second.created is False

        async with factory() as session:
            pages = (
                await session.execute(
                    select(Page.image_url, Page.tg_message_id)
                    .where(Page.chapter_id == first.chapter_id)
                    .order_by(Page.index)
                )
            ).all()
            assert [row[0] for row in pages] == [
                "https://img.test/1.jpg",
                "https://img.test/2.jpg",
                "https://img.test/3.jpg",
            ]
            assert [row[1] for row in pages] == [11, 12, 13]
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_outbound_sent_state_is_sticky(tmp_path: Path):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
    )
    await init_db(settings)
    try:
        factory = get_session_factory()
        async with factory() as session:
            first = await publish_chapter(
                session,
                title="男子穿上后妈的皮后为所欲为 03 + 番外 + 视频",
                chapter_title="第 1 话",
                source_key="-1001:r:9",
                source_chat_id="-1001",
                source_chat_title="3D漫画 聊天",
                image_urls=["https://img.test/1.jpg", "tg:-1001:88"],
                message_ids=[1, 88],
                media_kinds=["image", "video"],
            )
            assert first is not None
            assert first.album_sent is False
            await mark_outbound_sent(
                session,
                first.chapter_id,
                album=True,
                video_urls=["tg:-1001:88"],
            )

        async with factory() as session:
            again = await publish_chapter(
                session,
                title="男子穿上后妈的皮后为所欲为 03 + 番外 + 视频",
                chapter_title="第 1 话",
                source_key="-1001:r:9",
                source_chat_id="-1001",
                source_chat_title="3D漫画 聊天",
                image_urls=["https://img.test/2.jpg"],
                message_ids=[2],
            )
            assert again is not None
            assert again.created is False
            assert again.album_sent is True
            assert again.sent_video_urls == ["tg:-1001:88"]
            chapter = (
                await session.execute(select(Chapter).where(Chapter.id == first.chapter_id))
            ).scalar_one()
            album_sent, sent = read_outbound_state(chapter)
            assert album_sent is True
            assert sent == ["tg:-1001:88"]
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_videos_are_stored_but_not_counted_as_manga_pages(tmp_path: Path):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
    )
    await init_db(settings)
    try:
        factory = get_session_factory()
        async with factory() as session:
            published = await publish_chapter(
                session,
                title="诛仙 小白 问心+50分钟剧情视频",
                chapter_title="第 1 话",
                source_key="123:g:9",
                source_chat_id="123",
                source_chat_title="3D漫画 聊天",
                image_urls=["https://img.test/1.jpg", "tg:-1001:88"],
                message_ids=[1, 88],
                media_kinds=["image", "video"],
            )
            assert published is not None
            assert published.page_count == 1
            assert await list_chapter_image_urls(session, published.chapter_id) == [
                "https://img.test/1.jpg"
            ]
            assert await list_chapter_videos(session, published.chapter_id) == ["tg:-1001:88"]
            assert published.new_video_urls == ["tg:-1001:88"]

        async with factory() as session:
            attached = await publish_chapter(
                session,
                title="诛仙 小白 问心+50分钟剧情视频",
                chapter_title="第 1 话",
                source_key="-1002285859531:m:99",
                source_chat_id="-1002285859531",
                source_chat_title="3D漫画",
                image_urls=["tg:-1002285859531:99"],
                message_ids=[99],
                media_kinds=["video"],
            )
            assert attached is not None
            assert attached.created is False
            assert attached.chapter_id == published.chapter_id
            assert attached.page_count == 1
            assert attached.new_video_urls == ["tg:-1002285859531:99"]
            assert await list_chapter_image_urls(session, published.chapter_id) == [
                "https://img.test/1.jpg"
            ]
            assert await list_chapter_videos(session, published.chapter_id) == [
                "tg:-1001:88",
                "tg:-1002285859531:99",
            ]

        async with factory() as session:
            orphan = await publish_chapter(
                session,
                title="广告片不会进漫画库",
                chapter_title="广告",
                source_key="-1002161690727:m:1",
                source_chat_id="-1002161690727",
                source_chat_title="3D漫画 聊天",
                image_urls=["tg:-1002161690727:1"],
                message_ids=[1],
                media_kinds=["video"],
            )
            assert orphan is None
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_load_stored_outbound_and_pending_reply_sweep(tmp_path: Path):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
    )
    await init_db(settings)
    try:
        factory = get_session_factory()
        async with factory() as session:
            published = await publish_chapter(
                session,
                title="凡人修仙传 慕沛灵 CV中配+素材",
                chapter_title="第 1 话",
                source_key="-1002161690727:r:1094788",
                source_chat_id="-1002161690727",
                source_chat_title="3D漫画 聊天",
                author="CC",
                tags=["修仙"],
                image_urls=["https://img.test/1.jpg", "tg:-1002161690727:9"],
                message_ids=[1, 9],
                media_kinds=["image", "video"],
            )
            assert published is not None
            chapter = (
                await session.execute(select(Chapter).where(Chapter.id == published.chapter_id))
            ).scalar_one()
            chapter.updated_at = utcnow() - timedelta(minutes=20)
            await session.commit()

        async with factory() as session:
            stored = await load_stored_outbound(session, "-1002161690727:r:1094788")
            assert stored is not None
            assert stored.title == "凡人修仙传 慕沛灵 CV中配+素材"
            assert stored.author == "CC"
            assert stored.tags == ["修仙"]
            assert stored.image_urls == ["https://img.test/1.jpg"]
            assert stored.video_urls == ["tg:-1002161690727:9"]
            assert stored.published.album_sent is False

            idle_before = utcnow() - timedelta(seconds=900)
            pending = await list_pending_reply_outbound(session, idle_before=idle_before)
            assert [item.source_key for item in pending] == ["-1002161690727:r:1094788"]

            await mark_outbound_sent(session, published.chapter_id, album=True)
            pending_video = await list_pending_reply_outbound(
                session, idle_before=utcnow() + timedelta(seconds=1)
            )
            assert [item.source_key for item in pending_video] == [
                "-1002161690727:r:1094788"
            ]
            await mark_outbound_sent(
                session,
                published.chapter_id,
                album=False,
                video_urls=stored.video_urls,
            )
            pending_after = await list_pending_reply_outbound(
                session, idle_before=utcnow() + timedelta(seconds=1)
            )
            assert pending_after == []
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_site_ids_and_outbound_retry_state_are_persistent(tmp_path: Path):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
    )
    await init_db(settings)
    try:
        factory = get_session_factory()
        async with factory() as session:
            published = await publish_chapter(
                session,
                title="乱世书",
                chapter_title="第 1 话",
                source_key="-1002161690727:r:1102578",
                source_chat_id="-1002161690727",
                source_chat_title="3D漫画 聊天",
                image_urls=["https://img.test/1.jpg", "https://img.test/2.jpg"],
                message_ids=[11, 12],
            )
            assert published is not None
            remote_ids = await mark_site_publish_succeeded(
                session,
                published.chapter_id,
                page_count=2,
                response={"status": "duplicate"},
            )
            assert remote_ids == (None, None)
            stored_without_ids = await load_stored_outbound(
                session, "-1002161690727:r:1102578"
            )
            assert stored_without_ids is not None
            assert stored_without_ids.published.site_published_page_count == 2
            assert stored_without_ids.published.site_manga_id is None
            remote_ids = await mark_site_publish_succeeded(
                session,
                published.chapter_id,
                page_count=2,
                response={"status": "duplicate", "mangaId": 356, "chapterId": 368},
            )
            assert remote_ids == (356, 368)

        async with factory() as session:
            stored = await load_stored_outbound(session, "-1002161690727:r:1102578")
            assert stored is not None
            assert stored.published.site_published_page_count == 2
            assert stored.published.site_manga_id == 356
            assert stored.published.site_chapter_id == 368
            retry = await mark_outbound_failed(
                session,
                stored.published.chapter_id,
                RuntimeError("Telegram unavailable"),
            )
            assert retry.attempts == 1
            assert retry.retry_at is not None
            assert retry.exhausted is False

        async with factory() as session:
            pending = await list_pending_reply_outbound(
                session,
                idle_before=utcnow() + timedelta(days=1),
            )
            assert pending == []
            chapter = (
                await session.execute(
                    select(Chapter).where(Chapter.id == published.chapter_id)
                )
            ).scalar_one()
            chapter.tg_outbound_retry_at = utcnow() - timedelta(seconds=1)
            chapter.updated_at = utcnow() - timedelta(minutes=20)
            await session.commit()

        async with factory() as session:
            pending = await list_pending_reply_outbound(
                session,
                idle_before=utcnow() - timedelta(minutes=15),
            )
            assert [item.source_key for item in pending] == [
                "-1002161690727:r:1102578"
            ]
            for _ in range(OUTBOUND_MAX_ATTEMPTS - 1):
                retry = await mark_outbound_failed(
                    session,
                    published.chapter_id,
                    "still unavailable",
                )
            assert retry.attempts == OUTBOUND_MAX_ATTEMPTS
            assert retry.retry_at is None
            assert retry.exhausted is True

        async with factory() as session:
            pending = await list_pending_reply_outbound(
                session,
                idle_before=utcnow() + timedelta(days=365),
            )
            assert pending == []
            await mark_outbound_sent(session, published.chapter_id, album=True)
            chapter = (
                await session.execute(
                    select(Chapter).where(Chapter.id == published.chapter_id)
                )
            ).scalar_one()
            assert chapter.tg_outbound_attempts == 0
            assert chapter.tg_outbound_retry_at is None
            assert chapter.tg_outbound_last_error is None
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_publish_api_duplicate_returns_canonical_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from backend.api.routes import manga as manga_routes
    from backend.services.manga.schemas import ManualPublishRequest

    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
    )
    await init_db(settings)
    monkeypatch.setattr(manga_routes, "_valid_publish_secret", lambda _token: True)
    body = ManualPublishRequest(
        title="乱世书",
        chapterTitle="第 1 话",
        sourceKey="-1002161690727:r:1102578",
        imageUrls=["https://img.test/1.jpg"],
    )
    try:
        factory = get_session_factory()
        async with factory() as session:
            created = await manga_routes.publish_manga(
                body,
                x_manga_publish_key="secret",
                authorization=None,
                session=session,
            )
            duplicate = await manga_routes.publish_manga(
                body,
                x_manga_publish_key="secret",
                authorization=None,
                session=session,
            )
        assert created["status"] == "ok"
        assert duplicate == {
            "status": "duplicate",
            "source_key": "-1002161690727:r:1102578",
            "manga_id": created["manga_id"],
            "chapter_id": created["chapter_id"],
            "slug": created["slug"],
            "number": created["number"],
            "page_count": created["page_count"],
        }
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_legacy_sqlite_migration_does_not_requeue_sent_history(tmp_path: Path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE mangas (id INTEGER PRIMARY KEY, slug TEXT, title TEXT);
        CREATE TABLE chapters (
            id INTEGER PRIMARY KEY,
            manga_id INTEGER,
            number INTEGER,
            title TEXT,
            source_key TEXT,
            page_count INTEGER,
            tg_album_sent BOOLEAN DEFAULT 0,
            tg_sent_videos TEXT
        );
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            chapter_id INTEGER,
            image_url TEXT,
            media_kind TEXT DEFAULT 'image'
        );
        INSERT INTO mangas (id, slug, title) VALUES (1, 'sent', '已发送'), (2, 'pending', '待发送');
        INSERT INTO chapters
            (id, manga_id, number, title, source_key, page_count, tg_album_sent)
        VALUES
            (1, 1, 1, '已发送', '1:r:1', 12, 1),
            (2, 2, 1, '待发送', '1:r:2', 9, 0);
        """
    )
    connection.commit()
    connection.close()

    settings = MangaSettings(
        database_url=f"sqlite:///{database}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
    )
    await init_db(settings)
    await close_db()

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT id, site_published_page_count, tg_outbound_attempts "
            "FROM chapters ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [(1, 12, 0), (2, 0, 0)]


@pytest.mark.asyncio
async def test_list_mangas_source_filter_paginates(tmp_path: Path):
    from backend.services.manga.db import Manga, list_mangas

    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
    )
    await init_db(settings)
    try:
        factory = get_session_factory()
        async with factory() as session:
            for index in range(1, 16):
                session.add(
                    Manga(
                        slug=f"eh-{index}",
                        title=f"E-Hentai {index}",
                        source_chat_id="ehentai",
                        source_chat_title="E-Hentai",
                        page_count=index,
                        chapter_count=1,
                    )
                )
            session.add(
                Manga(
                    slug="tg-one",
                    title="频道采集 1",
                    source_chat_id="-1001",
                    source_chat_title="频道",
                    page_count=9,
                    chapter_count=2,
                )
            )
            await session.commit()

            page1, total = await list_mangas(session, page=1, limit=12, source="ehentai", published_only=False)
            assert total == 15
            assert len(page1) == 12
            page2, total2 = await list_mangas(session, page=2, limit=12, source="ehentai", published_only=False)
            assert total2 == 15
            assert len(page2) == 3
            channel, channel_total = await list_mangas(
                session, page=1, limit=12, source="telegram", published_only=False
            )
            assert channel_total == 1
            assert channel[0].slug == "tg-one"
            found, found_total = await list_mangas(
                session, page=1, limit=12, q="E-Hentai 15", source="ehentai", published_only=False
            )
            assert found_total == 1
            assert found[0].slug == "eh-15"
    finally:
        await close_db()
