from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend.services.manga.config import (
    load_manga_settings,
    manga_config_public,
    save_manga_settings,
)
from backend.services.manga.db import close_db
from backend.services.manga.ehentai import catalog as eh_catalog
from backend.services.manga.ehentai.client import EhentaiClient, ShowpageMissing
from backend.services.manga.ehentai.parser import (
    merge_image_pages,
    parse_full_image_url,
    parse_gallery_page,
    parse_gallery_ref,
    parse_image_page_urls,
    parse_next_image_page,
    parse_search_results,
    prefer_showpage_url,
    split_searches,
)
from backend.services.manga.ehentai.tags import (
    display_tags,
    set_catalog_path,
    translate_namespace,
    translate_tag,
)
from tg_signer.security import is_encrypted_secret

SEARCH_HTML = """
<table>
<tr><td class="gl3c glname"><a href="https://e-hentai.org/g/123456/abcdef0123/">One</a></td></tr>
<tr><td class="gl3c glname"><a href="/g/123456/abcdef0123/">dup</a></td></tr>
<tr><td class="gl3c glname"><a href="https://e-hentai.org/g/999/aaabbbcccddd/">Two</a></td></tr>
</table>
"""

GALLERY_HTML = """
<html><body>
<h1 id="gn">English Title</h1>
<h1 id="gj">日本語タイトル</h1>
<div id="gdd"><table>
<tr><td class="gdt1">Length:</td><td class="gdt2">120 pages</td></tr>
</table></div>
<div id="taglist">
<a href="https://e-hentai.org/tag/female:netorare">ntr</a>
<a href="https://e-hentai.org/tag/artist:foo+bar">artist</a>
<a href="https://e-hentai.org/tag/language:chinese">zh</a>
</div>
<div id="gdt">
<a href="https://e-hentai.org/s/1111111111/123456-1"></a>
<a href="https://e-hentai.org/s/2222222222/123456-2"></a>
<a href="https://e-hentai.org/s/aaaaaaaaaa/123456-2"></a>
</div>
<div id="cdiv">
<a href="https://e-hentai.org/s/deadbeef00/999999-1">comment</a>
</div>
<table class="ptb"><tr><td><a>1</a></td><td><a>2</a></td></tr></table>
<script>var showkey="abc-show";</script>
</body></html>
"""

IMAGE_HTML = """
<img id="img" src="https://cdn.example/h/page001.jpg" style="width:1px" />
"""


def test_split_and_parse_search():
    assert split_searches("female:NTR language:Chinese, 3d language:Chinese\n") == [
        "female:NTR language:Chinese",
        "3d language:Chinese",
    ]
    refs = parse_search_results(SEARCH_HTML)
    assert [item.source_key for item in refs] == ["eh:123456/abcdef0123", "eh:999/aaabbbcccddd"]
    ref = parse_gallery_ref("https://exhentai.org/g/1/abcdef12/", "https://exhentai.org")
    assert ref is not None
    assert ref.gid == 1
    assert ref.source_key == "eh:1/abcdef12"
    stored = parse_gallery_ref("eh:4135469/3adfd314c9")
    assert stored is not None
    assert stored.gid == 4135469
    assert stored.token == "3adfd314c9"
    assert stored.source_key == "eh:4135469/3adfd314c9"


def test_parse_gallery_prefers_japanese_title_and_skips_language_tag():
    meta = parse_gallery_page(GALLERY_HTML, "https://e-hentai.org/g/123456/abcdef0123/")
    assert meta is not None
    assert meta.title == "日本語タイトル"
    assert meta.page_count == 120
    assert meta.artist == "foo bar"
    assert meta.image_pages == [
        "https://e-hentai.org/s/1111111111/123456-1",
        "https://e-hentai.org/s/2222222222/123456-2",
    ]
    assert meta.result_pages == 2
    tags = display_tags(meta.raw_tags)
    assert "chinese" not in {item.lower() for item in tags}
    assert parse_full_image_url(IMAGE_HTML) == "https://cdn.example/h/page001.jpg"


def _thumb_gallery_html(
    *,
    length: int,
    start: int,
    end: int,
    result_pages: int,
    gid: int = 123456,
    title: str = "Paged Title",
) -> str:
    thumbs = "\n".join(
        f'<a href="https://e-hentai.org/s/{n:010x}/{gid}-{n}"></a>'
        for n in range(start, end + 1)
    )
    pager = "".join(f"<td><a>{i}</a></td>" for i in range(1, result_pages + 1))
    return f"""
<html><body>
<h1 id="gn">{title}</h1>
<div id="gdd"><table>
<tr><td class="gdt1">Length:</td><td class="gdt2">{length} pages</td></tr>
</table></div>
<div id="gdt">{thumbs}</div>
<table class="ptb"><tr>{pager}</tr></table>
</body></html>
"""


@pytest.mark.asyncio
async def test_fetch_gallery_paginates_past_first_thumbnail_batch():
    gid = 123456
    token = "abcdef0123"
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        hits.append(str(request.url))
        page = int((parse_qs(parsed.query).get("p") or ["0"])[0])
        if page <= 0:
            html_text = _thumb_gallery_html(length=45, start=1, end=20, result_pages=3, gid=gid)
        elif page == 1:
            # 重叠一张，验证去重
            html_text = _thumb_gallery_html(length=45, start=20, end=40, result_pages=3, gid=gid)
        else:
            html_text = _thumb_gallery_html(length=45, start=41, end=45, result_pages=3, gid=gid)
        return httpx.Response(200, text=html_text)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = EhentaiClient(delay_seconds=0, http=http)
        ref = parse_gallery_ref(f"https://e-hentai.org/g/{gid}/{token}/")
        assert ref is not None
        meta = await client.fetch_gallery(ref)
    assert meta.page_count == 45
    assert len(meta.image_pages) == 45
    assert len(set(meta.image_pages)) == 45
    assert meta.image_pages[0].endswith(f"/{gid}-1")
    assert meta.image_pages[-1].endswith(f"/{gid}-45")
    assert any("?p=1" in url for url in hits)
    assert any("?p=2" in url for url in hits)


@pytest.mark.asyncio
async def test_fetch_gallery_does_not_page_when_length_already_covered():
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(
            200,
            text=_thumb_gallery_html(length=8, start=1, end=8, result_pages=1),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = EhentaiClient(delay_seconds=0, http=http)
        ref = parse_gallery_ref("https://e-hentai.org/g/123456/abcdef0123/")
        assert ref is not None
        meta = await client.fetch_gallery(ref)
    assert len(meta.image_pages) == 8
    assert all("?p=" not in url for url in hits)


def test_parse_image_pages_keeps_gallery_gid_and_first_imgkey():
    html_text = """
    <div id="gdt">
      <a href="https://e-hentai.org/s/8d5a4a7968/4150402-41"></a>
      <a href="https://e-hentai.org/s/bbbbbbbbbb/4150402-41"></a>
      <a href="https://e-hentai.org/s/aaaaaaaaaa/4150402-1"></a>
      <a href="https://e-hentai.org/s/cccccccccc/999-1"></a>
    </div>
    <div id="cdiv"><a href="https://e-hentai.org/s/dddddddddd/4150402-99"></a></div>
    """
    urls = parse_image_page_urls(html_text, gid=4150402)
    assert urls == [
        "https://e-hentai.org/s/aaaaaaaaaa/4150402-1",
        "https://e-hentai.org/s/8d5a4a7968/4150402-41",
    ]


def test_prefer_showpage_url_uses_next_link_when_page_matches():
    thumb = "https://e-hentai.org/s/8d5a4a7968/4150402-41"
    nxt = "https://e-hentai.org/s/1111111111/4150402-41"
    assert prefer_showpage_url(thumb, nxt) == nxt
    assert prefer_showpage_url(thumb, "https://e-hentai.org/s/1111111111/4150402-42") == thumb
    assert prefer_showpage_url(thumb, None) == thumb
    merged = merge_image_pages(
        ["https://e-hentai.org/s/aaaaaaaa/1-1", "https://e-hentai.org/s/bbbbbbbb/1-2"],
        ["https://e-hentai.org/s/ffffffff/1-2", "https://e-hentai.org/s/cccccccc/1-3"],
    )
    assert merged == [
        "https://e-hentai.org/s/aaaaaaaa/1-1",
        "https://e-hentai.org/s/bbbbbbbb/1-2",
        "https://e-hentai.org/s/cccccccc/1-3",
    ]


def test_parse_next_image_page_from_showpage_html():
    html_text = """
    <a id="prev" href="https://e-hentai.org/s/aaaa/4150402-40"></a>
    <a id="next" href="https://e-hentai.org/s/1111111111/4150402-42"></a>
    <img id="img" src="https://cdn.example/h/page041.jpg" />
    """
    assert parse_next_image_page(html_text) == "https://e-hentai.org/s/1111111111/4150402-42"


@pytest.mark.asyncio
async def test_fetch_showpage_skips_http_404():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/s/" in str(request.url):
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = EhentaiClient(delay_seconds=0, http=http)
        page = await client.fetch_showpage("https://e-hentai.org/s/8d5a4a7968/4150402-41")
        assert page is None
        with pytest.raises(ShowpageMissing):
            await client.fetch_image_url("https://e-hentai.org/s/8d5a4a7968/4150402-41")


@pytest.mark.asyncio
async def test_fetch_showpage_reads_image_and_next_link():
    html_text = """
    <a href="https://e-hentai.org/s/abcdef0123/123456-2" id="next"></a>
    <img id="img" src="https://cdn.example/h/page001.jpg" />
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html_text)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = EhentaiClient(delay_seconds=0, http=http)
        page = await client.fetch_showpage("https://e-hentai.org/s/1111111111/123456-1")
    assert page is not None
    assert page.image_url == "https://cdn.example/h/page001.jpg"
    assert page.next_url == "https://e-hentai.org/s/abcdef0123/123456-2"


@pytest.mark.asyncio
async def test_fetch_gallery_dedupes_conflicting_imgkeys_across_pages():
    gid = 123456
    token = "abcdef0123"

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        page = int((parse_qs(parsed.query).get("p") or ["0"])[0])
        if page <= 0:
            html_text = _thumb_gallery_html(length=25, start=1, end=20, result_pages=2, gid=gid)
            html_text = html_text.replace(
                f"/s/{20:010x}/{gid}-20",
                f"/s/deadbeefaa/{gid}-20",
            )
        else:
            html_text = _thumb_gallery_html(length=25, start=20, end=25, result_pages=2, gid=gid)
        return httpx.Response(200, text=html_text)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = EhentaiClient(delay_seconds=0, http=http)
        ref = parse_gallery_ref(f"https://e-hentai.org/g/{gid}/{token}/")
        assert ref is not None
        meta = await client.fetch_gallery(ref)
    assert len(meta.image_pages) == 25
    assert sum(1 for url in meta.image_pages if url.endswith(f"/{gid}-20")) == 1


def test_tag_translation_uses_bundled_names():
    assert translate_namespace("female") == "女性"
    translated = translate_tag("female", "netorare")
    assert translated
    assert translated != "female"


def test_ehentai_cookie_is_encrypted(tmp_path: Path, monkeypatch):
    config_file = tmp_path / ".manga_config.json"
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    saved = save_manga_settings(
        {
            "ehentai_enabled": True,
            "ehentai_cookie": "ipb_member_id=1; ipb_pass_hash=abc",
            "ehentai_search": "female:NTR language:Chinese",
            "ehentai_max_pages": 300,
        }
    )
    assert saved.ehentai_cookie.startswith("ipb_member_id")
    raw = config_file.read_text(encoding="utf-8")
    assert "ipb_pass_hash=abc" not in raw
    stored = __import__("json").loads(raw)
    assert is_encrypted_secret(stored["ehentai_cookie"])
    loaded = load_manga_settings()
    assert loaded.ehentai_search.startswith("female:NTR")
    assert loaded.ehentai_max_pages == 300
    public = manga_config_public(loaded)
    assert public["ehentai_cookie"] is None
    assert public["ehentai_cookie_set"] is True
    assert public["ehentai_enabled"] is True


DUMP = {
    "repo": "https://github.com/EhTagTranslation/Database.git",
    "version": 7,
    "head": {"sha": "abc123def456"},
    "data": [
        {"namespace": "rows", "data": {"female": {"name": "女性"}}},
        {
            "namespace": "female",
            "data": {"netorare": {"name": "NTR", "intro": "long text"}},
        },
        {"namespace": "artist", "data": {"foo bar": {"name": "测试画师"}}},
    ],
}


def test_slim_dump_keeps_names_only():
    slim = eh_catalog.slim_dump(DUMP, source="https://example.test/db.text.json")
    assert slim["sha"] == "abc123def456"
    assert slim["editor"] == eh_catalog.EHTT_EDITOR_URL
    assert slim["tag_count"] == 2
    female = next(item for item in slim["data"] if item["namespace"] == "female")
    assert female["data"]["netorare"] == {"name": "NTR"}


@pytest.mark.asyncio
async def test_refresh_catalog_writes_cache_and_translates(tmp_path: Path):
    eh_catalog._STATUS = None
    set_catalog_path(None)

    class FakeHttp:
        async def head(self, url, **kwargs):
            return SimpleNamespace(headers={"ETag": '"abc123def456"'})

        async def get(self, url, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: DUMP,
            )

        async def aclose(self):
            return None

    status = await eh_catalog.refresh_catalog(tmp_path, force=True, http=FakeHttp())
    assert status.using_bundled is False
    assert status.tag_count == 2
    assert translate_tag("female", "netorare") == "NTR"
    assert translate_tag("artist", "foo bar") == "测试画师"
    assert eh_catalog.catalog_path(tmp_path).is_file()

    class NoGetHttp:
        async def head(self, url, **kwargs):
            return SimpleNamespace(headers={"ETag": '"abc123def456"'})

        async def get(self, url, **kwargs):
            raise AssertionError("matched SHA should not download")

        async def aclose(self):
            return None

    skipped = await eh_catalog.refresh_catalog(tmp_path, force=False, http=NoGetHttp())
    assert skipped.sha == "abc123def456"
    set_catalog_path(None)
    eh_catalog._STATUS = None


@pytest.mark.asyncio
async def test_start_ehentai_auto_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_file = tmp_path / ".manga_config.json"
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "ehentai_enabled": False,
            "ehentai_search": "female:NTR language:Chinese",
            "ehentai_translation_auto": False,
            "cfbed_upload_url": "https://image.example/upload",
        }
    )

    class FakeWorker:
        def __init__(self, settings, client_provider):
            self.settings = settings
            self._stopped = __import__("asyncio").Event()

        def status(self):
            return {
                "worker_status": "running",
                "last_error": None,
                "cookie_configured": False,
                "exhentai": False,
                "search_count": 1,
                "current": {},
                "processed": 0,
                "skipped": 0,
                "failed": 0,
                "recent": [],
            }

        async def run_until_stopped(self):
            await self._stopped.wait()

        async def stop(self):
            self._stopped.set()

    monkeypatch.setattr("backend.services.manga.ehentai.worker.EhentaiWorker", FakeWorker)
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        payload = await runtime.start_ehentai()
        assert load_manga_settings().ehentai_enabled is True
        assert runtime.current_settings().ehentai_enabled is True
        assert payload["worker_status"] == "running"
    finally:
        await runtime.stop_ehentai()
        await close_db()


@pytest.mark.asyncio
async def test_reload_starts_ehentai_listen_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "ehentai_enabled": True,
            "ehentai_search": "female:NTR language:Chinese",
            "ehentai_translation_auto": False,
            "cfbed_upload_url": "https://image.example/upload",
        }
    )

    class FakeWorker:
        def __init__(self, settings, client_provider):
            self.settings = settings
            self._stopped = __import__("asyncio").Event()

        def status(self):
            return {
                "worker_status": "running",
                "last_error": None,
                "cookie_configured": False,
                "exhentai": False,
                "search_count": 1,
                "current": {},
                "processed": 0,
                "skipped": 0,
                "failed": 0,
                "recent": [],
            }

        async def run_until_stopped(self):
            await self._stopped.wait()

        async def stop(self):
            self._stopped.set()

    monkeypatch.setattr("backend.services.manga.ehentai.worker.EhentaiWorker", FakeWorker)
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        assert runtime.ehentai_worker is None
        await runtime.reload()
        assert runtime.ehentai_worker is not None
        assert runtime.ehentai_status()["worker_status"] == "running"
    finally:
        await runtime.stop_ehentai()
        await close_db()


def _fake_listen_worker(*, crash_first: bool = False, woken: list | None = None):
    created = {"n": 0}

    class FakeWorker:
        def __init__(self, settings, client_provider):
            created["n"] += 1
            self.settings = settings
            self._stopped = asyncio.Event()
            self._wake = asyncio.Event()

        def status(self):
            running = not self._stopped.is_set()
            return {
                "worker_status": "running" if running else "stopped",
                "last_error": None,
                "cookie_configured": False,
                "exhentai": False,
                "search_count": 1,
                "current": {},
                "processed": 0,
                "skipped": 0,
                "failed": 0,
                "recent": [],
                "listening": running,
                "phase": "waiting",
            }

        def request_pass(self):
            if woken is not None:
                woken.append(True)
            self._wake.set()

        async def run_until_stopped(self):
            if crash_first and created["n"] == 1:
                raise RuntimeError("boom")
            await self._stopped.wait()

        async def stop(self):
            self._stopped.set()

    return FakeWorker, created


@pytest.mark.asyncio
async def test_stop_ehentai_can_persist_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "ehentai_enabled": False,
            "ehentai_search": "female:NTR language:Chinese",
            "ehentai_translation_auto": False,
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    fake, _created = _fake_listen_worker()
    monkeypatch.setattr("backend.services.manga.ehentai.worker.EhentaiWorker", fake)
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        await runtime.start_ehentai()
        assert load_manga_settings().ehentai_enabled is True
        await runtime.stop_ehentai(persist_disabled=True)
        assert load_manga_settings().ehentai_enabled is False
        assert runtime.ehentai_worker is None
    finally:
        await runtime.stop_ehentai()
        await close_db()


@pytest.mark.asyncio
async def test_reload_swallows_ehentai_start_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "ehentai_enabled": True,
            "ehentai_search": "",
            "ehentai_translation_auto": False,
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        payload = await runtime.reload()
        assert payload["ehentai"]["worker_status"] in {"stopped", "disabled"}
        assert runtime.ehentai_error
        assert "搜索词" in runtime.ehentai_error
    finally:
        await runtime.stop_ehentai()
        await close_db()


@pytest.mark.asyncio
async def test_request_ehentai_pass_requires_listen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "ehentai_enabled": False,
            "ehentai_search": "female:NTR language:Chinese",
            "ehentai_translation_auto": False,
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        with pytest.raises(RuntimeError, match="持续监听"):
            await runtime.request_ehentai_pass()
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_request_ehentai_pass_wakes_running_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "ehentai_enabled": True,
            "ehentai_search": "female:NTR language:Chinese",
            "ehentai_translation_auto": False,
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    woken: list[bool] = []
    fake, _created = _fake_listen_worker(woken=woken)
    monkeypatch.setattr("backend.services.manga.ehentai.worker.EhentaiWorker", fake)
    from backend.services.manga.runtime import MangaRuntime

    runtime = MangaRuntime()
    await runtime.initialize()
    try:
        await runtime.start_ehentai()
        await runtime.request_ehentai_pass()
        assert woken == [True]
    finally:
        await runtime.stop_ehentai()
        await close_db()


@pytest.mark.asyncio
async def test_ehentai_restarts_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    save_manga_settings(
        {
            "ehentai_enabled": True,
            "ehentai_search": "female:NTR language:Chinese",
            "ehentai_translation_auto": False,
            "cfbed_upload_url": "https://image.example/upload",
        }
    )
    fake, created = _fake_listen_worker(crash_first=True)
    monkeypatch.setattr("backend.services.manga.ehentai.worker.EhentaiWorker", fake)
    import backend.services.manga.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "EHENTAI_RESTART_DELAY", 0)
    runtime = runtime_mod.MangaRuntime()
    await runtime.initialize()
    try:
        await runtime.start_ehentai()
        for _ in range(50):
            if created["n"] >= 2 and runtime.ehentai_worker is not None:
                break
            await asyncio.sleep(0.02)
        assert created["n"] >= 2
        assert runtime.ehentai_worker is not None
        assert runtime.ehentai_status()["worker_status"] == "running"
    finally:
        await runtime.stop_ehentai()
        await close_db()


@pytest.mark.asyncio
async def test_request_pass_skips_listen_wait():
    from backend.services.manga.config import MangaSettings
    from backend.services.manga.ehentai.worker import EhentaiWorker

    settings = MangaSettings(
        cfbed_upload_url="https://image.example/upload",
        ehentai_search="female:NTR language:Chinese",
        ehentai_poll_seconds=30,
    )
    worker = EhentaiWorker(settings)

    async def kick() -> None:
        await asyncio.sleep(0.05)
        worker.request_pass()

    started = asyncio.get_running_loop().time()
    await asyncio.gather(worker._sleep_until_next_pass(20), kick())
    assert asyncio.get_running_loop().time() - started < 2
    await worker.stop()
