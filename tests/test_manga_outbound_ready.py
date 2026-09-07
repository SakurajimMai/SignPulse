from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.manga.assembler import PendingChapter
from backend.services.manga.config import MangaSettings
from backend.services.manga.db import close_db, init_db
from backend.services.manga.publisher import publish_chapter
from backend.services.manga.telegram_worker import TelegramMangaWorker


class FakeSite:
    enabled = True

    def __init__(self):
        self.calls: list[dict] = []

    async def publish_chapter(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "status": "duplicate",
            "sourceKey": kwargs.get("source_key"),
            "mangaId": 356,
            "chapterId": 368,
        }


class FakeSiteWithoutIds:
    enabled = True

    def __init__(self):
        self.calls: list[dict] = []

    async def publish_chapter(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


class FakeChannel:
    enabled = True

    def __init__(self):
        self.calls: list[dict] = []

    async def publish_chapter(self, **kwargs):
        self.calls.append(kwargs)
        return True


class FailingSite:
    enabled = True

    def __init__(self):
        self.calls = 0

    async def publish_chapter(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("site unavailable")


@pytest.mark.asyncio
async def test_empty_final_flush_forwards_checkpointed_chapter(tmp_path: Path):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
        site_publish_secret="test",
    )
    await init_db(settings)
    worker = TelegramMangaWorker(settings)
    original_site = worker.site
    channel = FakeChannel()
    site = FakeSite()
    worker.channel = channel
    worker.site = site
    try:
        from backend.services.manga.db import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            stored = await publish_chapter(
                session,
                title="色欲迷情 20",
                chapter_title="第 1 话",
                source_key="-1002161690727:r:1094653",
                source_chat_id="-1002161690727",
                source_chat_title="3D漫画 聊天",
                author="Can",
                tags=["NTR"],
                image_urls=["https://img.test/1.jpg", "https://img.test/2.jpg"],
                message_ids=[11, 12],
            )
            assert stored is not None

        await worker._on_chapter_ready(
            PendingChapter(
                bucket_key="-1002161690727:r:1094653",
                chat_id=-1002161690727,
                chat_title="3D漫画 聊天",
                pages=[],
                title_hint="色欲迷情 20",
                author_hint="Can",
                tags=["NTR"],
                source_key="-1002161690727:r:1094653",
                is_final=True,
            )
        )
        assert len(channel.calls) == 1
        assert channel.calls[0]["title"] == "色欲迷情 20"
        assert channel.calls[0]["image_urls"] == [
            "https://img.test/1.jpg",
            "https://img.test/2.jpg",
        ]
        assert channel.calls[0]["local"].created is True
        assert channel.calls[0]["local"].site_manga_id == 356
        assert channel.calls[0]["local"].site_chapter_id == 368
        assert len(site.calls) == 1

        await worker._on_chapter_ready(
            PendingChapter(
                bucket_key="-1002161690727:r:1094653",
                chat_id=-1002161690727,
                chat_title="3D漫画 聊天",
                pages=[],
                source_key="-1002161690727:r:1094653",
                is_final=True,
            )
        )
        assert len(channel.calls) == 1
        assert len(site.calls) == 1
    finally:
        await worker.imgbed.aclose()
        await original_site.aclose()
        await worker.telegraph.aclose()
        await close_db()


@pytest.mark.asyncio
async def test_empty_checkpoint_does_not_forward(tmp_path: Path):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
        outbound_enabled=True,
        outbound_channel="-1002085070183",
    )
    await init_db(settings)
    worker = TelegramMangaWorker(settings)
    original_site = worker.site
    channel = FakeChannel()
    worker.channel = channel
    worker.site = FakeSite()
    try:
        await worker._on_chapter_ready(
            PendingChapter(
                bucket_key="-1002161690727:r:1",
                chat_id=-1002161690727,
                chat_title="3D漫画 聊天",
                pages=[],
                source_key="-1002161690727:r:1",
                is_final=False,
            )
        )
        assert channel.calls == []
    finally:
        await worker.imgbed.aclose()
        await original_site.aclose()
        await worker.telegraph.aclose()
        await close_db()


@pytest.mark.asyncio
async def test_site_success_without_remote_ids_still_forwards_outbound(tmp_path: Path):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        site_publish_url="https://reader.test/api/manga/publish",
        site_publish_secret="test",
    )
    await init_db(settings)
    worker = TelegramMangaWorker(settings)
    original_site = worker.site
    channel = FakeChannel()
    site = FakeSiteWithoutIds()
    worker.channel = channel
    worker.site = site
    try:
        from backend.services.manga.db import get_session_factory
        from backend.services.manga.publisher import load_stored_outbound

        factory = get_session_factory()
        async with factory() as session:
            stored = await publish_chapter(
                session,
                title="兼容站点",
                chapter_title="第 1 话",
                source_key="-1002161690727:r:success-without-ids",
                source_chat_id="-1002161690727",
                source_chat_title="3D漫画 聊天",
                image_urls=["https://img.test/1.jpg", "https://img.test/2.jpg"],
                message_ids=[11, 12],
            )
            assert stored is not None

        await worker._on_chapter_ready(
            PendingChapter(
                bucket_key="-1002161690727:r:success-without-ids",
                chat_id=-1002161690727,
                chat_title="3D漫画 聊天",
                pages=[],
                title_hint="兼容站点",
                source_key="-1002161690727:r:success-without-ids",
                is_final=True,
            )
        )

        assert len(site.calls) == 1
        assert len(channel.calls) == 1
        assert channel.calls[0]["local"].site_manga_id is None
        async with factory() as session:
            persisted = await load_stored_outbound(
                session, "-1002161690727:r:success-without-ids"
            )
        assert persisted is not None
        assert persisted.published.site_published_page_count == 2
        assert persisted.published.album_sent is True
        assert persisted.published.outbound_attempts == 0
    finally:
        await worker.imgbed.aclose()
        await original_site.aclose()
        await worker.telegraph.aclose()
        await close_db()


@pytest.mark.asyncio
async def test_stale_site_failure_is_deferred_instead_of_retried_every_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = MangaSettings(
        database_url=f"sqlite:///{tmp_path / 'tg_manga.db'}",
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        telegram_session_file=str(tmp_path / "unused.session"),
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
        site_publish_secret="test",
    )
    await init_db(settings)
    worker = TelegramMangaWorker(settings)
    original_site = worker.site
    channel = FakeChannel()
    site = FailingSite()
    worker.channel = channel
    worker.site = site
    monkeypatch.setattr(
        "backend.services.manga.telegram_worker.schedule_alert",
        lambda *_args, **_kwargs: None,
    )
    try:
        from backend.services.manga.db import Chapter, get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            stored = await publish_chapter(
                session,
                title="乱世书",
                chapter_title="第 1 话",
                source_key="-1002161690727:r:1102578",
                source_chat_id="-1002161690727",
                source_chat_title="3D漫画 聊天",
                image_urls=["https://img.test/1.jpg"],
                message_ids=[11],
            )
            assert stored is not None

        await worker._on_chapter_ready(
            PendingChapter(
                bucket_key="-1002161690727:r:1102578",
                chat_id=-1002161690727,
                chat_title="3D漫画 聊天",
                pages=[],
                source_key="-1002161690727:r:1102578",
                is_final=True,
            )
        )
        assert site.calls == 1
        assert channel.calls == []
        async with factory() as session:
            chapter = await session.get(Chapter, stored.chapter_id)
            assert chapter is not None
            assert chapter.tg_outbound_attempts == 1
            assert chapter.tg_outbound_retry_at is not None
            assert chapter.tg_outbound_last_error == "site unavailable"

        await worker._flush_stale_outbound()
        assert site.calls == 1
    finally:
        await worker.imgbed.aclose()
        await original_site.aclose()
        await worker.telegraph.aclose()
        await close_db()
