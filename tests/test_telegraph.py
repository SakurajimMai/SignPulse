from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from backend.services.manga.sources import parse_source_bindings
from backend.services.manga.telegraph import (
    ascii_referer,
    chapter_images_usable,
    collect_telegraph_image_urls,
    decode_telegraph_page_message_id,
    encode_telegraph_page_message_id,
    extract_online_read_urls_from_message,
    extract_telegraph_urls_from_message,
    extract_telegraph_urls_from_text,
    html_image_urls,
    inspect_image_file,
    is_telegraph_url,
    parse_html_title,
    parse_js_image_gallery,
    resolve_reader_urls_from_message,
    sibling_image_url,
    telegraph_page_path,
    unwrap_reader_url,
)


def _write_jpeg(path: Path, size: tuple[int, int], color: tuple[int, int, int] = (30, 40, 50)) -> None:
    image = Image.new("RGB", size, color)
    image.save(path, "JPEG", quality=85)


def test_ascii_referer_percent_encodes_chinese_path():
    encoded = ascii_referer(
        "https://telegra.ph/Potions-Samus-Captioned-Image-Set-AI-Generated-机翻-08-19"
    )
    encoded.encode("ascii")
    assert "机翻" not in encoded
    assert "%E6%9C%BA%E7%BF%BB" in encoded
    assert ascii_referer("https://telegra.ph/foo-08-19") == "https://telegra.ph/foo-08-19"


def test_telegraph_page_message_id_roundtrip():
    assert encode_telegraph_page_message_id(1820, 1) == 18200001
    assert decode_telegraph_page_message_id(18200032) == 1820
    assert decode_telegraph_page_message_id(3) == 3
    assert decode_telegraph_page_message_id(None) is None


def test_extract_telegraph_url_from_caption_and_button():
    text = (
        "我妻子接受的特别教育\n"
        "♠在telegraph观看♠ https://telegra.ph/Chihel-test-08-13\n"
    )
    assert extract_telegraph_urls_from_text(text) == [
        "https://telegra.ph/Chihel-test-08-13"
    ]

    message = SimpleNamespace(
        caption=text,
        text=text,
        message=text,
        raw_text=text,
        entities=[],
        caption_entities=[
            SimpleNamespace(
                type="text_link",
                url="https://telegra.ph/Chihel-%EB%82%B4-%EC%95%84%EB%82%B4%EA%B0%80-08-13",
                offset=0,
                length=4,
            )
        ],
        reply_markup=SimpleNamespace(
            inline_keyboard=[
                [
                    SimpleNamespace(url="https://telegra.ph/from-button-08-13", text="在telegraph观看"),
                    SimpleNamespace(url="https://t.me/NTR_EH_Bot", text="搜索更多"),
                ]
            ]
        ),
        web_page=None,
    )
    urls = extract_telegraph_urls_from_message(message)
    assert urls[0] == "https://telegra.ph/Chihel-test-08-13"
    assert "https://telegra.ph/from-button-08-13" in urls
    assert all("t.me" not in item for item in urls)


def test_collect_telegraph_images_preserves_order_and_resolves_relative():
    content = [
        {"tag": "img", "attrs": {"src": "https://eh-image.ntr.you/images/001.jpg"}},
        {"tag": "p", "children": [{"tag": "img", "attrs": {"src": "/file/abc.jpg"}}]},
        {"tag": "hr"},
    ]
    assert collect_telegraph_image_urls(content) == [
        "https://eh-image.ntr.you/images/001.jpg",
        "https://telegra.ph/file/abc.jpg",
    ]
    assert html_image_urls(
        '<img src="https://cdn.example/1.jpg"><img src="/file/2.jpg">'
    ) == ["https://cdn.example/1.jpg", "https://telegra.ph/file/2.jpg"]
    assert is_telegraph_url("https://telegra.ph/Chihel-08-13")
    assert telegraph_page_path("https://telegra.ph/Chihel-08-13?foo=1") == "Chihel-08-13"
    assert html_image_urls(
        '<img data-src="https://cdn.example/page01.jpg"><img src="/icon.svg">'
    ) == ["https://cdn.example/page01.jpg"]


def test_online_read_button_is_required_when_post_has_buttons():
    read_url = "https://telegra.ph/lula-cram-school-08-19"
    message = SimpleNamespace(
        caption="《补习老师的勾心斗角》 +幕后图 共 338 P",
        text="《补习老师的勾心斗角》 +幕后图 共 338 P",
        message="《补习老师的勾心斗角》 +幕后图 共 338 P",
        raw_text="《补习老师的勾心斗角》 +幕后图 共 338 P",
        entities=[],
        caption_entities=[],
        reply_markup=SimpleNamespace(
            inline_keyboard=[
                [
                    SimpleNamespace(url=read_url, text="📖 在线阅读"),
                    SimpleNamespace(url="https://t.me/SomeFileBot?start=pack", text="下载资源"),
                ]
            ]
        ),
        web_page=None,
    )
    assert extract_online_read_urls_from_message(message) == [read_url]
    urls, reason = resolve_reader_urls_from_message(message)
    assert urls == [read_url]
    assert reason == "online-read"

    no_read = SimpleNamespace(
        caption="只有下载",
        text="只有下载",
        message="只有下载",
        raw_text="只有下载",
        entities=[],
        caption_entities=[],
        reply_markup=SimpleNamespace(
            inline_keyboard=[
                [SimpleNamespace(url="https://t.me/SomeFileBot?start=pack", text="下载资源")]
            ]
        ),
        web_page=None,
    )
    urls, reason = resolve_reader_urls_from_message(no_read)
    assert urls == []
    assert reason == "missing-online-read"
    assert unwrap_reader_url("https://t.me/iv?url=https://telegra.ph/foo-08-19") == (
        "https://telegra.ph/foo-08-19"
    )


def test_parse_js_image_gallery_from_ckp_reader():
    html = """
    <title>补习老师的勾心斗角</title>
    <script>
    const base =
    "https://pub-bd6c6785be3f45a188ae1a4b7d7c3e30.r2.dev/";
    for(let i = 1; i <= 100; i++){
        let num = String(i).padStart(3,'0');
        let img = document.createElement("img");
        img.src = base + num + ".jpg";
        document.body.appendChild(img);
    }
    </script>
    """
    urls = parse_js_image_gallery(html)
    assert len(urls) == 100
    assert urls[0] == "https://pub-bd6c6785be3f45a188ae1a4b7d7c3e30.r2.dev/001.jpg"
    assert urls[-1] == "https://pub-bd6c6785be3f45a188ae1a4b7d7c3e30.r2.dev/100.jpg"

    png = parse_js_image_gallery(
        'const base="https://cdn.example/x/"; for(let i = 1; i <= 2; i++){'
        'let num = String(i).padStart(3,"0"); img.src = base + num + ".png";}'
    )
    assert png == [
        "https://cdn.example/x/001.png",
        "https://cdn.example/x/002.png",
    ]
    bare = parse_js_image_gallery(
        'const base="https://cdn.example/y/"; for(i=1;i<=2;i++){'
        "let num = String(i).padStart(3,'0'); img.src = base + num + '.jpg';}"
    )
    assert bare == [
        "https://cdn.example/y/001.jpg",
        "https://cdn.example/y/002.jpg",
    ]
    assert parse_html_title("<title>催眠大师 29</title>") == "催眠大师 29"
    assert sibling_image_url("https://cdn.example/001.jpg") == "https://cdn.example/001.png"
    assert sibling_image_url("https://cdn.example/001.png") == "https://cdn.example/001.jpg"


def test_empty_discussion_defaults_to_telegraph_mode():
    pairs = parse_source_bindings(
        [
            {"channel": "-1001", "discussion": "-1002"},
            {"channel": "-1003", "discussion": ""},
            {"channel": "-1004", "discussion": "-1005", "ingest_mode": "telegraph"},
        ]
    )
    assert pairs[0].resolved_ingest_mode() == "discussion"
    assert pairs[0].wants_history_backfill() is True
    assert pairs[1].is_telegraph()
    assert pairs[1].wants_history_backfill() is False
    assert pairs[2].is_telegraph()
    assert pairs[2].wants_history_backfill() is False
    assert all(item.ingest_enabled for item in pairs)


def test_inspect_rejects_imgbox_placeholder_and_html(tmp_path: Path):
    placeholder = tmp_path / "ph.jpg"
    _write_jpeg(placeholder, (240, 240))
    info = inspect_image_file(placeholder)
    assert not info.valid
    assert info.reason == "placeholder-size"

    html = tmp_path / "page.jpg"
    html.write_text("<html>service update</html>", encoding="utf-8")
    assert inspect_image_file(html).valid is False

    real = tmp_path / "page.jpg"
    _write_jpeg(real, (1200, 1800), (80, 20, 20))
    ok = inspect_image_file(real)
    assert ok.valid
    assert ok.width == 1200


def test_skip_chapter_when_images_are_repeated_placeholders(tmp_path: Path):
    inspected = []
    for index in range(6):
        path = tmp_path / f"{index}.jpg"
        _write_jpeg(path, (240, 240))
        inspected.append(inspect_image_file(path))
    usable, reason = chapter_images_usable(inspected, expected=64)
    assert usable is False
    assert "no valid" in reason or "repeated" in reason

    good = []
    for index in range(8):
        path = tmp_path / f"ok-{index}.jpg"
        _write_jpeg(path, (1000, 1400), (index * 10, 40, 50))
        good.append(inspect_image_file(path))
    assert chapter_images_usable(good, expected=8)[0] is True
