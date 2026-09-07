from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from backend.services.manga.config import load_manga_settings
from backend.services.manga.imgbed import (
    ImgBedClient,
    ImgBedError,
    prepare_upload_image,
    reset_upload_pacing_for_tests,
    sniff_image,
    telegram_safe_upload_url,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TELEGRAM_500 = {
    "TelegramNew": "Error: TelegramNewupload error, check your environment params about telegram channel!",
    "CloudflareR2": "Error: CloudflareR2Error: No R2 channel provided",
}
CF_520_HTML = """<!DOCTYPE html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<html class="no-js" lang="en-US">
<head>
<title>ixacg.de | 520: Web server is returning an unknown error</title>
<meta charset="UTF-8" />
</head>
<body>
<div id="cf-wrapper">Cloudflare 520 error page filler """ + ("x" * 400) + """</div>
</body>
</html>
"""
CF_522_HTML = """<!DOCTYPE html><html lang="en-US"><head><title>522: Connection timed out</title></head>
<body>cloudflare origin timeout</body></html>
"""


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reset_upload_pacing_for_tests()
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CFBED_UPLOAD_URL", "https://image.example/upload")
    monkeypatch.setenv("CFBED_PUBLIC_BASE", "https://cdn.example")
    return replace(load_manga_settings(), cfbed_retry_delay_seconds=0.01)


def test_sniff_image_types():
    assert sniff_image(JPEG) == (".jpg", "image/jpeg")
    assert sniff_image(PNG) == (".png", "image/png")
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPxxxx") == (".webp", "image/webp")


def test_prepare_upload_downscales_huge_jpeg():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (4000, 3000), (10, 20, 30)).save(buf, format="JPEG", quality=95)
    data, ext, mime = prepare_upload_image(buf.getvalue())
    assert ext == ".jpg"
    assert mime == "image/jpeg"
    fitted = Image.open(BytesIO(data))
    assert max(fitted.size) <= 2560
    assert fitted.size[0] + fitted.size[1] <= 10000


def test_prepare_upload_converts_webp_to_jpeg():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (12, 12), (200, 10, 10)).save(buf, format="WEBP")
    data, ext, mime = prepare_upload_image(buf.getvalue())
    assert ext == ".jpg"
    assert mime == "image/jpeg"
    assert data.startswith(b"\xff\xd8\xff")


def test_prepare_upload_converts_png_to_jpeg():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGBA", (16, 16), (10, 20, 30, 128)).save(buf, format="PNG")
    data, ext, mime = prepare_upload_image(buf.getvalue())
    assert ext == ".jpg"
    assert mime == "image/jpeg"
    assert data.startswith(b"\xff\xd8\xff")


def test_prepare_upload_clamps_extreme_ratio():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (40, 2000), (8, 8, 8)).save(buf, format="JPEG", quality=90)
    data, ext, mime = prepare_upload_image(buf.getvalue())
    assert ext == ".jpg"
    fitted = Image.open(BytesIO(data))
    assert max(fitted.size) / max(min(fitted.size), 1) <= 20.01
    assert fitted.size[0] + fitted.size[1] <= 10000


def test_telegram_safe_upload_url_adds_send_document_flag():
    url = telegram_safe_upload_url("https://image.example/upload?authCode=abc")
    assert "serverCompress=false" in url
    assert "uploadChannel=telegram" in url
    assert "authCode=abc" in url
    already = telegram_safe_upload_url(
        "https://image.example/upload?serverCompress=true&uploadChannel=telegram"
    )
    assert "serverCompress=true" in already
    assert already.count("serverCompress=") == 1


@pytest.mark.asyncio
async def test_upload_retries_telegram_channel_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CFBED_UPLOAD_URL", "https://image.example/upload")
    monkeypatch.setenv("CFBED_PUBLIC_BASE", "https://cdn.example")
    attempts = {"n": 0}
    settings = replace(load_manga_settings(), cfbed_retry_delay_seconds=0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500, json=TELEGRAM_500)
        return httpx.Response(200, json={"src": "/file/ok.jpg"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ImgBedClient(settings, http=http)
        url = await client.upload_bytes(JPEG, filename="001.jpg")
    assert url == "https://cdn.example/file/ok.jpg"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_upload_converts_png_and_uses_send_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from io import BytesIO

    from PIL import Image

    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CFBED_UPLOAD_URL", "https://image.example/upload")
    monkeypatch.setenv("CFBED_PUBLIC_BASE", "https://cdn.example")
    seen: dict[str, object] = {}
    buf = BytesIO()
    Image.new("RGB", (12, 12), (30, 40, 50)).save(buf, format="PNG")
    png = buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        body = request.content
        seen["has_png_name"] = b"eh1-001.png" in body
        seen["has_jpg_name"] = b"eh1-001.jpg" in body
        seen["jpeg_mime"] = b"image/jpeg" in body
        return httpx.Response(200, json={"src": "/file/ok.jpg"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ImgBedClient(load_manga_settings(), http=http)
        url = await client.upload_bytes(png, filename="eh1-001.png")
    assert url == "https://cdn.example/file/ok.jpg"
    assert seen["has_png_name"] is False
    assert seen["has_jpg_name"] is True
    assert seen["jpeg_mime"] is True
    assert "serverCompress=false" in str(seen["url"])
    assert "uploadChannel=telegram" in str(seen["url"])


@pytest.mark.asyncio
async def test_upload_gives_up_after_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CFBED_UPLOAD_URL", "https://image.example/upload")
    settings = replace(load_manga_settings(), cfbed_retry_delay_seconds=0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=TELEGRAM_500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ImgBedClient(settings, http=http)
        with pytest.raises(ImgBedError, match="TelegramNew"):
            await client.upload_bytes(JPEG, filename="001.jpg")


@pytest.mark.asyncio
async def test_upload_retries_cloudflare_520_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _settings(tmp_path, monkeypatch)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(
                520,
                text=CF_520_HTML,
                headers={"content-type": "text/html; charset=UTF-8"},
            )
        return httpx.Response(200, json={"src": "/file/ok-520.jpg"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ImgBedClient(settings, http=http)
        url = await client.upload_bytes(JPEG, filename="eh-page.jpg")
    assert url == "https://cdn.example/file/ok-520.jpg"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_upload_retries_cloudflare_522_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _settings(tmp_path, monkeypatch)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                522,
                text=CF_522_HTML,
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, json={"src": "/file/ok-522.jpg"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ImgBedClient(settings, http=http)
        url = await client.upload_bytes(JPEG, filename="eh-page.jpg")
    assert url == "https://cdn.example/file/ok-522.jpg"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_upload_520_exhausted_has_short_error_no_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _settings(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            520,
            text=CF_520_HTML,
            headers={"content-type": "text/html; charset=UTF-8"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ImgBedClient(settings, http=http)
        with pytest.raises(ImgBedError) as raised:
            await client.upload_bytes(JPEG, filename="eh-page.jpg")
    message = str(raised.value)
    assert "520" in message
    assert "<!DOCTYPE" not in message
    assert "<html" not in message.lower()
    assert "ixacg.de" not in message
    assert len(message) < 200
    assert "unknown error" in message.lower() or "origin" in message.lower()


@pytest.mark.asyncio
async def test_upload_200_cloudflare_html_is_short_error_no_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _settings(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=CF_520_HTML,
            headers={"content-type": "text/html; charset=UTF-8"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ImgBedClient(settings, http=http)
        with pytest.raises(ImgBedError) as raised:
            await client.upload_bytes(JPEG, filename="eh-page.jpg")
    message = str(raised.value)
    assert "520" in message
    assert "<!DOCTYPE" not in message
    assert "<html" not in message.lower()
    assert "ixacg.de" not in message
    assert "oldie" not in message
    assert "Invalid JSON" not in message
