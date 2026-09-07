from __future__ import annotations

import pytest

from backend.services.manga.channel_publish import (
    ChannelPublisher,
    build_outbound_caption,
    build_read_button,
    build_reader_url,
    can_publish_outbound,
    parse_tg_media_ref,
    plan_outbound_post,
    preview_image_urls,
    resolve_site_origin,
)
from backend.services.manga.config import MangaSettings
from backend.services.manga.publisher import PublishedChapter


def test_resolve_site_origin_prefers_override_then_publish_host():
    assert resolve_site_origin("https://www.ixacg.de/api/manga/publish") == "https://www.ixacg.de"
    assert (
        resolve_site_origin("https://www.ixacg.de/api/manga/publish", "https://read.example")
        == "https://read.example"
    )
    assert resolve_site_origin("", "ixacg.de") == "https://ixacg.de"


def test_build_reader_url_and_caption():
    url = build_reader_url(site_origin="https://www.ixacg.de", manga_id=154, number=1)
    assert url == "https://www.ixacg.de/manga/154/read/1"
    caption = build_outbound_caption(
        title="NTR诊所2",
        author="ZZR",
        tags=["NTR", "人妻"],
        read_url=url,
        button_text="点击阅读",
    )
    assert "<b>NTR诊所2</b>" in caption
    assert "作者：#ZZR" in caption
    assert "#NTR" in caption
    assert "#人妻" in caption
    assert 'href="https://www.ixacg.de/manga/154/read/1"' in caption
    assert "点击阅读" in caption


def test_preview_image_urls_dedupes_and_caps():
    urls = preview_image_urls(
        ["https://a/1.jpg", "https://a/1.jpg", "https://a/2.jpg", "", "https://a/3.jpg"],
        2,
    )
    assert urls == ["https://a/1.jpg", "https://a/2.jpg"]


def test_plan_outbound_holds_until_thread_closes_then_sends_once():
    assert plan_outbound_post(
        is_final=False,
        album_sent=False,
        new_video_urls=["tg:-1:2"],
        sent_video_urls=[],
    ) == (False, [])
    assert plan_outbound_post(
        is_final=True,
        album_sent=False,
        new_video_urls=["tg:-1:2", "tg:-1:2"],
        sent_video_urls=[],
    ) == (True, ["tg:-1:2"])
    assert plan_outbound_post(
        is_final=True,
        album_sent=True,
        new_video_urls=["tg:-1:2", "tg:-1:3"],
        sent_video_urls=["tg:-1:2"],
    ) == (False, ["tg:-1:3"])
    assert plan_outbound_post(
        is_final=True,
        album_sent=True,
        new_video_urls=["tg:-1:2"],
        sent_video_urls=["tg:-1:2"],
    ) == (False, [])


def test_outbound_waits_for_site_publish():
    assert (
        can_publish_outbound(
            channel_enabled=True,
            send_album=True,
            video_urls=[],
            site_enabled=True,
            has_site_images=True,
            site_published=True,
        )
        is True
    )
    assert (
        can_publish_outbound(
            channel_enabled=True,
            send_album=True,
            video_urls=[],
            site_enabled=True,
            has_site_images=True,
            site_published=False,
        )
        is False
    )
    assert (
        can_publish_outbound(
            channel_enabled=True,
            send_album=True,
            video_urls=[],
            site_enabled=True,
            has_site_images=True,
            site_published=None,
        )
        is False
    )
    assert (
        can_publish_outbound(
            channel_enabled=True,
            send_album=False,
            video_urls=["tg:-1001:9"],
            site_enabled=True,
            has_site_images=False,
            site_published=None,
        )
        is True
    )
    assert (
        can_publish_outbound(
            channel_enabled=True,
            send_album=True,
            video_urls=[],
            site_enabled=False,
            has_site_images=True,
            site_published=None,
        )
        is True
    )


@pytest.mark.asyncio
async def test_channel_publisher_sends_album_only_for_new_chapters():
    sent: list[tuple] = []

    class FakeClient:
        async def send_media_group(self, chat, media):
            sent.append(("group", chat, media))

        async def send_photo(self, chat, photo, caption=None, parse_mode=None):
            sent.append(("photo", chat, photo, caption))

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=3,
        outbound_button_text="点击阅读",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    publisher = ChannelPublisher(settings, lambda: FakeClient())
    local = PublishedChapter(
        manga_id=154,
        chapter_id=1,
        slug="ntr",
        number=1,
        page_count=10,
        created=True,
    )
    ok = await publisher.publish_chapter(
        title="NTR诊所2",
        author="ZZR",
        tags=["NTR"],
        image_urls=["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg", "https://img/4.jpg"],
        local=local,
    )
    assert ok is True
    assert sent[0][0] == "group"
    assert sent[0][1] == "-1002085070183"
    assert len(sent[0][2]) == 3

    sent.clear()
    skipped = await publisher.publish_chapter(
        title="NTR诊所2",
        author="ZZR",
        tags=["NTR"],
        image_urls=["https://img/1.jpg"],
        local=PublishedChapter(
            manga_id=154,
            chapter_id=1,
            slug="ntr",
            number=1,
            page_count=11,
            created=False,
        ),
    )
    assert skipped is False
    assert sent == []


def test_build_read_button_uses_url():
    button = build_read_button("点击阅读", "https://www.ixacg.de/manga/1/read/1")
    assert button["inline_keyboard"][0][0]["text"] == "点击阅读"
    assert button["inline_keyboard"][0][0]["url"].endswith("/manga/1/read/1")


@pytest.mark.asyncio
async def test_channel_publisher_bot_sends_one_album_post():
    calls: list[tuple[str, dict]] = []

    async def fake_http(method: str, payload: dict) -> dict:
        calls.append((method, payload))
        if method == "sendMediaGroup":
            return {"ok": True, "result": [{"message_id": 88}]}
        return {"ok": True, "result": {"message_id": 89}}

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=3,
        outbound_button_text="点击阅读",
        outbound_bot_token="123:ABC",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    from backend.services.manga.channel_publish import TelegramBotApi

    publisher = ChannelPublisher(
        settings,
        lambda: None,
        bot_api=TelegramBotApi("123:ABC", http=fake_http),
    )
    ok = await publisher.publish_chapter(
        title="NTR诊所2",
        author="ZZR",
        tags=["NTR"],
        image_urls=["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"],
        local=PublishedChapter(
            manga_id=355,
            chapter_id=1,
            slug="ntr",
            number=1,
            page_count=10,
            created=True,
            site_manga_id=356,
        ),
    )
    assert ok is True
    assert [item[0] for item in calls] == ["sendMediaGroup"]
    media = calls[0][1]["media"]
    assert len(media) == 3
    assert "作者：#ZZR" in media[0]["caption"]
    assert 'href="https://www.ixacg.de/manga/356/read/1"' in media[0]["caption"]
    assert "点击阅读" in media[0]["caption"]


@pytest.mark.asyncio
async def test_channel_publisher_omits_reader_link_without_remote_manga_id():
    calls: list[tuple[str, dict]] = []

    async def fake_http(method: str, payload: dict) -> dict:
        calls.append((method, payload))
        return {"ok": True, "result": [{"message_id": 88}]}

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=2,
        outbound_button_text="点击阅读",
        outbound_bot_token="123:ABC",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    from backend.services.manga.channel_publish import TelegramBotApi

    publisher = ChannelPublisher(
        settings,
        lambda: None,
        bot_api=TelegramBotApi("123:ABC", http=fake_http),
    )
    ok = await publisher.publish_chapter(
        title="无远端 ID 漫画",
        author=None,
        tags=[],
        image_urls=["https://img/1.jpg", "https://img/2.jpg"],
        local=PublishedChapter(
            manga_id=355,
            chapter_id=1,
            slug="without-remote-id",
            number=1,
            page_count=2,
            created=True,
        ),
    )

    assert ok is True
    caption = calls[0][1]["media"][0]["caption"]
    assert "href=" not in caption
    assert "/manga/355/" not in caption


@pytest.mark.asyncio
async def test_channel_publisher_bot_drops_unfetchable_album_photo_and_retries():
    calls: list[tuple[str, dict]] = []

    async def fake_http(method: str, payload: dict) -> dict:
        calls.append((method, payload))
        if len(calls) == 1:
            return {
                "ok": False,
                "description": (
                    'Bad Request: failed to send message #4 with "WEBPAGE_CURL_FAILED"'
                ),
            }
        return {"ok": True, "result": [{"message_id": 88}]}

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=4,
        outbound_button_text="点击阅读",
        outbound_bot_token="123:ABC",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    from backend.services.manga.channel_publish import TelegramBotApi

    publisher = ChannelPublisher(
        settings,
        lambda: None,
        bot_api=TelegramBotApi("123:ABC", http=fake_http),
    )
    ok = await publisher.publish_chapter(
        title="乱世书",
        author="朱雀",
        tags=["漫画"],
        image_urls=[f"https://img/{index}.jpg" for index in range(1, 5)],
        local=PublishedChapter(
            manga_id=356,
            chapter_id=368,
            slug="luanshishu",
            number=1,
            page_count=219,
            created=True,
            site_manga_id=356,
        ),
    )

    assert ok is True
    assert [method for method, _payload in calls] == ["sendMediaGroup", "sendMediaGroup"]
    assert [item["media"] for item in calls[1][1]["media"]] == [
        "https://img/1.jpg",
        "https://img/2.jpg",
        "https://img/3.jpg",
    ]
    assert "<b>乱世书</b>" in calls[1][1]["media"][0]["caption"]


@pytest.mark.asyncio
async def test_channel_publisher_bot_falls_back_to_photo_with_button():
    calls: list[tuple[str, dict]] = []

    async def fake_http(method: str, payload: dict) -> dict:
        calls.append((method, payload))
        if method == "sendMediaGroup":
            return {
                "ok": False,
                "description": (
                    'Bad Request: failed to send message #2 with "WEBPAGE_CURL_FAILED"'
                ),
            }
        return {"ok": True, "result": {"message_id": 89}}

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=2,
        outbound_button_text="立即阅读",
        outbound_bot_token="123:ABC",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    from backend.services.manga.channel_publish import TelegramBotApi

    publisher = ChannelPublisher(
        settings,
        lambda: None,
        bot_api=TelegramBotApi("123:ABC", http=fake_http),
    )
    ok = await publisher.publish_chapter(
        title="乱世书",
        author=None,
        tags=[],
        image_urls=["https://img/good.jpg", "https://img/bad.jpg"],
        local=PublishedChapter(
            manga_id=356,
            chapter_id=368,
            slug="luanshishu",
            number=1,
            page_count=219,
            created=True,
            site_manga_id=356,
        ),
    )

    assert ok is True
    assert [method for method, _payload in calls] == ["sendMediaGroup", "sendPhoto"]
    photo = calls[1][1]
    assert photo["photo"] == "https://img/good.jpg"
    assert "<b>乱世书</b>" in photo["caption"]
    assert photo["reply_markup"]["inline_keyboard"][0][0] == {
        "text": "立即阅读",
        "url": "https://www.ixacg.de/manga/356/read/1",
    }


@pytest.mark.asyncio
async def test_channel_publisher_bot_does_not_hide_unrelated_album_errors():
    calls: list[str] = []

    async def fake_http(method: str, payload: dict) -> dict:
        calls.append(method)
        return {"ok": False, "description": "Bad Request: chat not found"}

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=2,
        outbound_bot_token="123:ABC",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    from backend.services.manga.channel_publish import TelegramBotApi

    publisher = ChannelPublisher(
        settings,
        lambda: None,
        bot_api=TelegramBotApi("123:ABC", http=fake_http),
    )
    with pytest.raises(RuntimeError, match="chat not found"):
        await publisher.publish_chapter(
            title="乱世书",
            author=None,
            tags=[],
            image_urls=["https://img/1.jpg", "https://img/2.jpg"],
            local=PublishedChapter(
                manga_id=356,
                chapter_id=368,
                slug="luanshishu",
                number=1,
                page_count=219,
                created=True,
            ),
        )

    assert calls == ["sendMediaGroup"]


def test_parse_tg_media_ref():
    assert parse_tg_media_ref("tg:-1002161690727:1073180") == (-1002161690727, 1073180)
    assert parse_tg_media_ref("https://image.ixacg.de/a.jpg") is None


@pytest.mark.asyncio
async def test_channel_publisher_copies_source_videos_without_site_urls():
    copied: list[tuple] = []

    class FakeClient:
        async def copy_message(self, dest, src, message_id):
            copied.append((dest, src, message_id))

        async def send_media_group(self, chat, media):
            copied.append(("album", chat, len(media)))

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=4,
        outbound_bot_token="123:ABC",
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    from backend.services.manga.channel_publish import TelegramBotApi

    async def fake_http(method: str, payload: dict) -> dict:
        copied.append((method, payload.get("chat_id"), len(payload.get("media") or [])))
        return {"ok": True, "result": [{"message_id": 1}]}

    publisher = ChannelPublisher(
        settings,
        lambda: FakeClient(),
        bot_api=TelegramBotApi("123:ABC", http=fake_http),
    )
    ok = await publisher.publish_chapter(
        title="诛仙 小白 问心+50分钟剧情视频",
        author=None,
        tags=[],
        image_urls=["https://img/1.jpg"],
        video_urls=["tg:-1002161690727:1073180"],
        local=PublishedChapter(
            manga_id=8,
            chapter_id=1,
            slug="x",
            number=1,
            page_count=10,
            created=True,
        ),
    )
    assert ok is True
    assert (-1002085070183, -1002161690727, 1073180) in copied


@pytest.mark.asyncio
async def test_channel_publisher_forwards_appended_videos_after_album():
    copied: list[tuple] = []

    class FakeClient:
        async def copy_message(self, dest, src, message_id):
            copied.append((dest, src, message_id))

        async def send_media_group(self, chat, media):
            copied.append(("album", chat, len(media)))

        async def send_photo(self, chat, photo, caption=None, parse_mode=None):
            copied.append(("photo", chat, photo, caption))

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=4,
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    publisher = ChannelPublisher(settings, lambda: FakeClient())
    ok = await publisher.publish_chapter(
        title="诛仙 小白 问心+50分钟剧情视频",
        author=None,
        tags=[],
        image_urls=["https://img/1.jpg"],
        video_urls=["tg:-1002285859531:99"],
        local=PublishedChapter(
            manga_id=1,
            chapter_id=1,
            slug="x",
            number=1,
            page_count=10,
            created=False,
        ),
    )
    assert ok is True
    assert copied == [(-1002085070183, -1002285859531, 99)]


@pytest.mark.asyncio
async def test_channel_publisher_skips_videos_when_disabled():
    copied: list[tuple] = []

    class FakeClient:
        async def copy_message(self, dest, src, message_id):
            copied.append((dest, src, message_id))

        async def send_media_group(self, chat, media):
            copied.append(("album", chat, len(media)))

        async def send_photo(self, chat, photo, caption=None, parse_mode=None):
            copied.append(("photo", chat, photo, caption))

    settings = MangaSettings(
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=4,
        outbound_forward_videos=False,
        site_publish_url="https://www.ixacg.de/api/manga/publish",
    )
    publisher = ChannelPublisher(settings, lambda: FakeClient())
    ok = await publisher.publish_chapter(
        title="诛仙 小白 问心+50分钟剧情视频",
        author=None,
        tags=[],
        image_urls=["https://img/1.jpg", "https://img/2.jpg"],
        video_urls=["tg:-1002161690727:1073385"],
        local=PublishedChapter(
            manga_id=1,
            chapter_id=1,
            slug="x",
            number=1,
            page_count=10,
            created=True,
        ),
    )
    assert ok is True
    assert copied[0][0] == "album"
    assert all(item[0] == "album" or item[0] == "photo" for item in copied)
