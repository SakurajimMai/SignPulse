from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.manga.config import MangaSettings
from backend.services.manga.site_publish import SitePublisher, SitePublishError


@pytest.mark.asyncio
async def test_site_publisher_keeps_only_manga_images():
    posted: list[dict] = []

    class FakeClient:
        async def post(self, url, json, headers):
            posted.append(json)
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True}, text="")

    settings = MangaSettings(
        site_publish_url="https://www.ixacg.de/api/manga/publish",
        site_publish_secret="secret",
    )
    publisher = SitePublisher(settings)
    publisher._client = FakeClient()
    body = await publisher.publish_chapter(
        title="诛仙 小白 问心+50分钟剧情视频",
        chapter_title="第 1 话",
        source_key="123:g:9",
        source_chat_id="123",
        source_chat_title="3D漫画 聊天",
        image_urls=[
            "https://image.ixacg.de/1.jpg",
            "tg:-1002161690727:1073180",
            "https://cdn.example/drama.mp4",
        ],
    )
    assert body == {"ok": True}
    assert posted[0]["imageUrls"] == ["https://image.ixacg.de/1.jpg"]
    assert posted[0]["coverUrl"] == "https://image.ixacg.de/1.jpg"


@pytest.mark.asyncio
async def test_site_publisher_skips_video_only_chapters():
    posted: list[dict] = []

    class FakeClient:
        async def post(self, url, json, headers):
            posted.append(json)
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True}, text="")

    settings = MangaSettings(
        site_publish_url="https://www.ixacg.de/api/manga/publish",
        site_publish_secret="secret",
    )
    publisher = SitePublisher(settings)
    publisher._client = FakeClient()
    body = await publisher.publish_chapter(
        title="诛仙 小白 问心+50分钟剧情视频",
        chapter_title="第 1 话",
        source_key="123:m:99",
        source_chat_id="123",
        source_chat_title="3D漫画",
        image_urls=["tg:-1002285859531:99"],
    )
    assert body is None
    assert posted == []


@pytest.mark.asyncio
async def test_site_publisher_rejects_cloudflare_html_without_leaking_markup():
    html = (
        "<!DOCTYPE html><!--[if lt IE 7]> <html class=\"no-js ie6 oldie\" "
        "lang=\"en-US\"> <![endif]--><title>ixacg.de | 520: Web server is "
        "returning an unknown error</title>"
    )

    class FakeClient:
        async def post(self, url, json, headers):
            def boom():
                raise ValueError("not json")

            return SimpleNamespace(status_code=200, json=boom, text=html)

    settings = MangaSettings(
        site_publish_url="https://www.ixacg.de/api/manga/publish",
        site_publish_secret="secret",
    )
    publisher = SitePublisher(settings)
    publisher._client = FakeClient()
    with pytest.raises(SitePublishError) as raised:
        await publisher.publish_chapter(
            title="测试",
            chapter_title="第 1 话",
            source_key="123:g:9",
            source_chat_id="123",
            source_chat_title="3D漫画",
            image_urls=["https://image.ixacg.de/1.jpg"],
        )
    message = str(raised.value)
    assert "520" in message
    assert "<!DOCTYPE" not in message
    assert "oldie" not in message
    assert "ixacg.de" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"status": "ok"},
        {"status": "success"},
        {"status": "created"},
        {"status": "updated"},
        {"status": "duplicate"},
        {"status": " DUPLICATE "},
    ],
)
async def test_site_publisher_accepts_explicit_success_statuses(body):
    class FakeClient:
        async def post(self, url, json, headers):
            return SimpleNamespace(status_code=200, json=lambda: body, text="")

    publisher = SitePublisher(
        MangaSettings(
            site_publish_url="https://www.ixacg.de/api/manga/publish",
            site_publish_secret="secret",
        )
    )
    publisher._client = FakeClient()

    result = await publisher.publish_chapter(
        title="测试",
        chapter_title="第 1 话",
        source_key="123:g:9",
        source_chat_id="123",
        source_chat_title="3D漫画",
        image_urls=["https://image.ixacg.de/1.jpg"],
    )

    assert result == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"ok": False},
        {"success": False, "status": "ok"},
        {"status": "error"},
        {"ok": True, "error": "database write failed"},
        {"status": "pending"},
        {"ok": True, "status": "error"},
    ],
)
async def test_site_publisher_rejects_json_without_success_confirmation(body):
    leaked_secret = "super-secret-response-token"

    class FakeClient:
        async def post(self, url, json, headers):
            return SimpleNamespace(
                status_code=200,
                json=lambda: body,
                text=f"<!DOCTYPE html><title>{leaked_secret}</title>",
            )

    publisher = SitePublisher(
        MangaSettings(
            site_publish_url="https://www.ixacg.de/api/manga/publish",
            site_publish_secret="secret",
        )
    )
    publisher._client = FakeClient()

    with pytest.raises(SitePublishError) as raised:
        await publisher.publish_chapter(
            title="测试",
            chapter_title="第 1 话",
            source_key="123:g:9",
            source_chat_id="123",
            source_chat_title="3D漫画",
            image_urls=["https://image.ixacg.de/1.jpg"],
        )

    message = str(raised.value)
    assert "Site publish failed" in message
    assert leaked_secret not in message
    assert "<!DOCTYPE" not in message


@pytest.mark.asyncio
async def test_site_publisher_accepts_wrapped_success_response():
    body = {"data": {"success": True, "mangaId": 356, "chapterId": 368}}

    class FakeClient:
        async def post(self, url, json, headers):
            return SimpleNamespace(status_code=200, json=lambda: body, text="")

    publisher = SitePublisher(
        MangaSettings(
            site_publish_url="https://www.ixacg.de/api/manga/publish",
            site_publish_secret="secret",
        )
    )
    publisher._client = FakeClient()

    result = await publisher.publish_chapter(
        title="测试",
        chapter_title="第 1 话",
        source_key="123:g:9",
        source_chat_id="123",
        source_chat_title="3D漫画",
        image_urls=["https://image.ixacg.de/1.jpg"],
    )

    assert result == body
