from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.services.manga.config import (
    load_manga_settings,
    manga_config_public,
    save_manga_settings,
)
from backend.services.manga.db import close_db
from backend.services.manga.wnacg.categories import (
    CATEGORY_BY_ID,
    list_url,
    parse_category_ids,
    serialize_category_ids,
)
from backend.services.manga.wnacg.parser import (
    parse_album_page,
    parse_album_ref,
    parse_image_urls,
    parse_list_results,
    parse_title_author,
)

LIST_HTML = """
<ul class="cc">
<li class="li tb gallary_item">
<div class="pic_box tb"><a href="/photos-index-aid-384502.html">
<img src="//t4.qy0.ru/data/t/3845/02/cover.jpg" alt="[ふじ家 (ねくたー)] 女になって自分を犯したい。 [中譯]" /></a></div>
<div class="info">
<div class="title"><a href="/photos-index-aid-384502.html">[ふじ家 (ねくたー)] 女になって自分を犯したい。 [中譯]</a></div>
<div class="info_col">22張圖片， 創建於2026-09-13</div>
</div>
</li>
<li class="li tb gallary_item">
<div class="pic_box tb"><a href="/photos-index-aid-384488.html">
<img alt="dup" /></a></div>
<div class="info">
<div class="title"><a href="/photos-index-aid-384488.html">[みかか] 関西弁オヤジと性交 [中国翻訳]</a></div>
<div class="info_col">74張圖片， 創建於2026-09-13</div>
</div>
</li>
</ul>
<div class="paginator">
<span class="thispage">1</span>
<a href="/albums-index-page-2-cate-1.html">2</a>
</div>
"""

GALLERY_HTML = """
<html><body>
<h2>[ふじ家 (ねくたー)] 女になって自分を犯したい。 [中譯]</h2>
<div class="uwconn">
<label>分類：同人誌／漢化</label><label>頁數：22P</label>
<div class="addtags">標籤：
<a class="tagshow" href="/albums-index-tag-x.html">ふじ家</a>
<a class="tagshow" href="/albums-index-tag-y.html">巨乳</a>
<a class="tagshow" href="/albums-index-tag-z.html">性轉</a>
</div>
</div>
</body></html>
"""

IMGLIST_JS = r"""
document.writeln("var imglist = [{ url: fast_img_host+\"//img5.qy0.ru/data/3845/02/01.jpg?verify=1\", caption: \"[01]\"},{ url: fast_img_host+\"//img5.qy0.ru/data/3845/02/02.jpg?verify=2\", caption: \"[02]\"},{ url: fast_img_host+\"/themes/weitu/images/bg/shoucang.jpg\", caption: \"喜歡紳士漫畫的同學請加入收藏哦！\"}];");
"""


def test_parse_list_and_album_ref():
    refs = parse_list_results(LIST_HTML)
    assert [item.source_key for item in refs] == ["wnacg:384502", "wnacg:384488"]
    assert refs[0].page_count == 22
    assert refs[0].title.startswith("[ふじ家")
    stored = parse_album_ref("wnacg:384502")
    assert stored is not None
    assert stored.aid == 384502
    assert stored.source_key == "wnacg:384502"
    from_url = parse_album_ref("https://www.wnacg.com/photos-index-aid-9.html")
    assert from_url is not None
    assert from_url.aid == 9


def test_parse_title_author_and_gallery():
    title, author = parse_title_author("[ふじ家 (ねくたー)] 女になって自分を犯したい。 [中譯]")
    assert title == "女になって自分を犯したい。"
    assert author == "ねくたー"
    title, author = parse_title_author("[銀茶] 恵那ちゃんは阿久津夫妻の性奴 [DL版] [5DK个人汉化]")
    assert title == "恵那ちゃんは阿久津夫妻の性奴"
    assert author == "銀茶"
    meta = parse_album_page(GALLERY_HTML, "https://www.wnacg.com/photos-index-aid-384502.html")
    assert meta is not None
    assert meta.title == "女になって自分を犯したい。"
    assert meta.author == "ねくたー"
    assert meta.page_count == 22
    assert "巨乳" in meta.tags
    urls = parse_image_urls(IMGLIST_JS)
    assert len(urls) == 2
    assert urls[0].startswith("https://img5.qy0.ru/")
    assert all("shoucang" not in item for item in urls)


def test_category_ids_and_list_url():
    assert parse_category_ids("1, 9, bogus, 1") == ["1", "9"]
    assert serialize_category_ids("1，20、latest") == "1,20,latest"
    cate = CATEGORY_BY_ID["1"]
    assert list_url(cate, 1).endswith("/albums-index-cate-1.html")
    assert list_url(cate, 2).endswith("/albums-index-page-2-cate-1.html")
    latest = CATEGORY_BY_ID["latest"]
    assert list_url(latest, 1).endswith("/albums.html")
    assert list_url(latest, 3).endswith("/albums-index-page-3.html")


def test_wnacg_settings_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_file = tmp_path / ".manga_config.json"
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    saved = save_manga_settings(
        {
            "wnacg_enabled": True,
            "wnacg_base_url": "https://www.wnacg.ru/",
            "wnacg_categories": "1,9,not-a-cate",
            "wnacg_max_pages": 300,
            "wnacg_list_pages": 2,
        }
    )
    assert saved.wnacg_enabled is True
    assert saved.wnacg_base_url == "https://www.wnacg.ru"
    assert saved.wnacg_categories == "1,9"
    assert saved.wnacg_max_pages == 300
    loaded = load_manga_settings()
    assert loaded.wnacg_categories == "1,9"
    public = manga_config_public(loaded)
    assert public["wnacg_enabled"] is True
    assert public["wnacg_categories"] == "1,9"


def test_settings_request_accepts_wnacg_fields():
    from backend.api.routes.manga import MangaSettingsRequest

    parsed = MangaSettingsRequest(
        wnacg_enabled=True,
        wnacg_categories="1,20",
        wnacg_list_pages=2,
        wnacg_max_pages=120,
    )
    assert parsed.wnacg_enabled is True
    assert parsed.wnacg_categories == "1,20"
    assert parsed.wnacg_list_pages == 2


def test_wnacg_routes_registered():
    from backend.api.routes import manga

    paths = {getattr(route, "path", "") for route in manga.router.routes}
    assert "/manga/wnacg/status" in paths
    assert "/manga/wnacg/start" in paths
    assert "/manga/wnacg/stop" in paths
    assert "/manga/wnacg/run-once" in paths
    assert "/manga/wnacg/categories" in paths


class FakeWnacgWorker:
    def __init__(self, settings, client_provider):
        self.settings = settings
        self._stopped = asyncio.Event()
        self.pass_requests = 0

    def status(self):
        return {
            "worker_status": "running",
            "last_error": None,
            "selected": ["1"],
            "category_count": 1,
            "current": {},
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "recent": [],
            "phase": "waiting",
            "listening": True,
        }

    def request_pass(self):
        self.pass_requests += 1

    async def run_until_stopped(self):
        await self._stopped.wait()

    async def stop(self):
        self._stopped.set()


@pytest.mark.asyncio
async def test_start_wnacg_auto_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "wnacg_enabled": False,
            "wnacg_categories": "1",
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    monkeypatch.setattr("backend.services.manga.wnacg.worker.WnacgWorker", FakeWnacgWorker)
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        payload = await runtime.start_wnacg()
        assert load_manga_settings().wnacg_enabled is True
        assert runtime.current_settings().wnacg_enabled is True
        assert payload["worker_status"] == "running"
    finally:
        await runtime.stop_wnacg()
        await close_db()


@pytest.mark.asyncio
async def test_reload_starts_wnacg_listen_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "wnacg_enabled": True,
            "wnacg_categories": "1,9",
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    monkeypatch.setattr("backend.services.manga.wnacg.worker.WnacgWorker", FakeWnacgWorker)
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        assert runtime.wnacg_worker is None
        await runtime.reload()
        assert runtime.wnacg_worker is not None
        assert runtime.wnacg_status()["worker_status"] == "running"
    finally:
        await runtime.stop_wnacg()
        await close_db()


@pytest.mark.asyncio
async def test_stop_wnacg_can_persist_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "wnacg_enabled": False,
            "wnacg_categories": "1",
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    monkeypatch.setattr("backend.services.manga.wnacg.worker.WnacgWorker", FakeWnacgWorker)
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        await runtime.start_wnacg()
        assert load_manga_settings().wnacg_enabled is True
        await runtime.stop_wnacg(persist_disabled=True)
        assert load_manga_settings().wnacg_enabled is False
        assert runtime.wnacg_worker is None
    finally:
        await runtime.stop_wnacg()
        await close_db()


@pytest.mark.asyncio
async def test_reload_swallows_wnacg_start_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "wnacg_enabled": True,
            "wnacg_categories": "",
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        payload = await runtime.reload()
        assert payload["wnacg"]["worker_status"] in {"stopped", "disabled"}
        assert runtime.wnacg_error
        assert "分类" in runtime.wnacg_error
    finally:
        await runtime.stop_wnacg()
        await close_db()


@pytest.mark.asyncio
async def test_request_wnacg_pass_requires_listen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings({"wnacg_enabled": False, "wnacg_categories": "1"})
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        with pytest.raises(RuntimeError, match="持续监听"):
            await runtime.request_wnacg_pass()
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_request_wnacg_pass_wakes_running_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "wnacg_enabled": True,
            "wnacg_categories": "1",
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    monkeypatch.setattr("backend.services.manga.wnacg.worker.WnacgWorker", FakeWnacgWorker)
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        await runtime.start_wnacg()
        await runtime.request_wnacg_pass()
        assert runtime.wnacg_worker.pass_requests == 1
    finally:
        await runtime.stop_wnacg()
        await close_db()


def test_poll_seconds_floor():
    from backend.services.manga.config import MangaSettings
    from backend.services.manga.wnacg.worker import poll_seconds

    settings = MangaSettings(wnacg_poll_seconds=30)
    assert poll_seconds(settings) == 60.0


@pytest.mark.asyncio
async def test_worker_skips_over_max_pages():
    from backend.services.manga.config import MangaSettings
    from backend.services.manga.wnacg.parser import AlbumRef
    from backend.services.manga.wnacg.worker import WnacgWorker

    settings = MangaSettings(wnacg_max_pages=50, wnacg_categories="1")
    worker = WnacgWorker(settings)

    async def no_exist(_key: str) -> int:
        return 0

    async def no_card(_key: str) -> dict:
        return {}

    worker._existing_page_count = no_exist  # type: ignore[method-assign]
    worker._card_fields = no_card  # type: ignore[method-assign]
    ref = AlbumRef(
        aid=1,
        url="https://www.wnacg.com/photos-index-aid-1.html",
        title="too-big",
        page_count=900,
    )
    await worker._handle_ref(ref)
    assert worker.stats["skipped"] == 1
    assert worker.recent[0]["status"] == "skipped"
    await worker.stop()
