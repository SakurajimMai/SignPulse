from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from backend.utils.http_error_text import looks_like_html, summarize_http_error
from backend.utils.outbound import httpx_async_client_kwargs

from .config import MangaSettings

logger = logging.getLogger(__name__)

# image.ixacg.de 这类图床走 Telegram 通道，并发上传会直接 500。
# 进程内所有采集（频道 / E-Hentai）共用一把锁。
_UPLOAD_GATE: asyncio.Semaphore | None = None
_COOLDOWN_UNTIL = 0.0
_LAST_OK = 0.0
# Telegram sendPhoto 对超长边/过大文件容易 IMAGE_PROCESS_FAILED，表现为 TelegramNew 500。
# 宽高比超过 20、CMYK/渐进 JPEG/webp 伪装成 jpg 同样会 500。
MAX_IMAGE_EDGE = 2560
MAX_IMAGE_BYTES = 2_500_000
MAX_IMAGE_RATIO = 20.0
# 群组 Bot 大约 20 条/分钟；小于 3s 连续传会 Flood，图床再包装成 TelegramNew 500。
MIN_UPLOAD_GAP = 3.2
UPLOAD_ATTEMPTS = 5
MAX_RETRY_DELAY = 60.0
# 5xx 含 Cloudflare 52x（源站挂了/超时），HTML 错误页同样当瞬时失败。
_RETRYABLE_STATUS = {
    408,
    425,
    429,
    500,
    502,
    503,
    504,
    520,
    521,
    522,
    523,
    524,
    525,
    526,
    527,
}


class ImgBedError(RuntimeError):
    pass


def _upload_gate() -> asyncio.Semaphore:
    global _UPLOAD_GATE
    if _UPLOAD_GATE is None:
        _UPLOAD_GATE = asyncio.Semaphore(1)
    return _UPLOAD_GATE


def reset_upload_pacing_for_tests() -> None:
    global _COOLDOWN_UNTIL, _LAST_OK
    _COOLDOWN_UNTIL = 0.0
    _LAST_OK = 0.0


async def _wait_pacing(base_delay: float) -> None:
    now = time.monotonic()
    wait = max(0.0, _COOLDOWN_UNTIL - now)
    min_gap = MIN_UPLOAD_GAP if base_delay >= 1 else 0.0
    if min_gap and _LAST_OK:
        wait = max(wait, min_gap - (now - _LAST_OK))
    if wait > 0:
        logger.info("ImgBed pacing wait %.1fs", wait)
        await asyncio.sleep(wait)


def _mark_success() -> None:
    global _LAST_OK
    _LAST_OK = time.monotonic()


def _mark_channel_failure(base_delay: float, body: str) -> None:
    global _COOLDOWN_UNTIL
    if base_delay < 1:
        return
    blob = (body or "").lower()
    extra = 45.0 if ("telegramnew" in blob or "flood" in blob) else 15.0
    _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + extra)


def sniff_image(data: bytes) -> tuple[str, str]:
    """根据文件头纠正扩展名和 MIME，避免把 webp/png 当成 jpg 发给 Telegram 通道。"""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".jpg", "image/jpeg"


def _to_rgb(image: Any) -> Any:
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        from PIL import Image

        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _fit_telegram_photo(width: int, height: int) -> tuple[int, int]:
    """Telegram sendPhoto：边长、边长和、宽高比都不能超。sendDocument 也受益于更小体积。"""
    scale = min(1.0, MAX_IMAGE_EDGE / max(width, height, 1), 10000 / max(width + height, 1))
    width = max(1, int(width * scale))
    height = max(1, int(height * scale))
    short = max(min(width, height), 1)
    ratio = max(width, height) / short
    if ratio > MAX_IMAGE_RATIO:
        if height >= width:
            height = max(1, int(width * MAX_IMAGE_RATIO))
        else:
            width = max(1, int(height * MAX_IMAGE_RATIO))
    return width, height


def prepare_upload_image(data: bytes) -> tuple[bytes, str, str]:
    """一律收成基线 RGB JPEG，避免 TelegramNew sendPhoto 的 IMAGE_PROCESS_FAILED / HTTP 500。"""
    ext, mime = sniff_image(data)
    try:
        from io import BytesIO

        from PIL import Image, ImageOps

        image = Image.open(BytesIO(data))
        image = ImageOps.exif_transpose(image) or image
        image.load()
        image = _to_rgb(image)
        width, height = image.size
        fitted = _fit_telegram_photo(width, height)
        if fitted != (width, height):
            image = image.resize(fitted, Image.Resampling.LANCZOS)
            width, height = fitted
        converted = b""
        for quality in (85, 78, 70, 62, 50):
            out = BytesIO()
            image.save(
                out,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=False,
                subsampling=2,
            )
            converted = out.getvalue()
            if len(converted) <= MAX_IMAGE_BYTES:
                break
        if converted.startswith(b"\xff\xd8\xff"):
            if ext != ".jpg" or fitted != image.size or len(converted) < len(data):
                logger.debug(
                    "图床预处理 %s %sx%s %sB -> jpeg %sB",
                    ext,
                    width,
                    height,
                    len(data),
                    len(converted),
                )
            return converted, ".jpg", "image/jpeg"
    except Exception:
        logger.warning("无法把 %s 转成安全 JPEG，按原格式上传", ext)
    return data, ext, mime


def telegram_safe_upload_url(url: str) -> str:
    """CloudFlare-ImgBed 默认 sendPhoto；serverCompress=false 改为 sendDocument，避开压图失败。"""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    lowered = {key.lower(): key for key in query}
    if "servercompress" not in lowered:
        query["serverCompress"] = "false"
    if "uploadchannel" not in lowered:
        query["uploadChannel"] = "telegram"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _should_retry(status: int | None, body: str) -> bool:
    if status in _RETRYABLE_STATUS:
        return True
    if status is not None and 520 <= status <= 527:
        return True
    blob = (body or "").lower()
    if "telegramnew" in blob or "upload error" in blob or "flood" in blob:
        return True
    # JSON 图床返回 HTML（含 Cloudflare 200/403 挑战页）视为瞬时失败。
    return looks_like_html(body)


def format_upload_failure(status: int, body: str) -> str:
    """把图床失败收成短句。Cloudflare HTML 不能原样塞进 last_error。"""
    return summarize_http_error(status, body, action="Upload failed")


class ImgBedClient:
    """Upload client for a user-configured CloudFlare ImgBed (or compatible) endpoint.

    No default host is assumed. You must set CFBED_UPLOAD_URL to your full upload API,
    e.g. https://your-domain.example/upload
    """

    def __init__(self, settings: MangaSettings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self._owns_http = http is None
        self._client = http or httpx.AsyncClient(
            **httpx_async_client_kwargs(
                timeout=httpx.Timeout(90.0, connect=20.0),
                limits=httpx.Limits(
                    max_keepalive_connections=0, max_connections=4
                ),
                headers={"Connection": "close"},
            )
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.settings.cfbed_api_token:
            headers["Authorization"] = f"Bearer {self.settings.cfbed_api_token}"
        return headers

    def resolve_url(self, item: dict[str, Any]) -> str:
        public = item.get("publicUrl")
        if isinstance(public, str) and public.startswith("http"):
            return public
        src = item.get("src") or item.get("url") or ""
        if isinstance(src, str) and src.startswith("http"):
            return src
        base = (self.settings.cfbed_public_base or "").rstrip("/")
        if isinstance(src, str) and src.startswith("/") and base:
            return f"{base}{src}"
        if isinstance(src, str) and src and base:
            return f"{base}/{src.lstrip('/')}"
        if isinstance(src, str) and src.startswith("/"):
            raise ImgBedError(
                f"Upload returned relative path {src!r} but CFBED_PUBLIC_BASE is empty"
            )
        raise ImgBedError(f"Upload response missing URL fields: {item!r}")

    async def upload_file(self, path: Path, *, filename: str | None = None) -> str:
        if not path.is_file():
            raise ImgBedError(f"File not found: {path}")
        name = filename or path.name
        mime, _ = mimetypes.guess_type(name)
        return await self.upload_bytes(path.read_bytes(), filename=name, mime=mime)

    async def upload_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        mime: str | None = None,
    ) -> str:
        try:
            url = telegram_safe_upload_url(self.settings.build_cfbed_upload_url())
        except ValueError as exc:
            raise ImgBedError(str(exc)) from exc
        data, ext, detected = prepare_upload_image(data)
        stem = Path(filename or "image").stem or "image"
        name = f"{stem}{ext}"
        guessed, _ = mimetypes.guess_type(name)
        mime = detected or mime or guessed or "application/octet-stream"
        field = self.settings.cfbed_file_field or "file"
        files = {field: (name, data, mime)}
        headers = self._auth_headers()
        safe_log = url.split("?")[0]
        attempts = UPLOAD_ATTEMPTS
        base_delay = max(0.01, float(self.settings.cfbed_retry_delay_seconds or 30.0))
        last_error = "upload failed"
        async with _upload_gate():
            for attempt in range(1, attempts + 1):
                await _wait_pacing(base_delay)
                req_headers = dict(headers)
                req_headers["Connection"] = "close"
                logger.info(
                    "Uploading %s (%d bytes) → %s attempt=%s/%s",
                    name,
                    len(data),
                    safe_log,
                    attempt,
                    attempts,
                )
                try:
                    resp = await self._client.post(url, files=files, headers=req_headers)
                except httpx.HTTPError as ext_err:
                    last_error = f"Upload network error: {ext_err}"
                    _mark_channel_failure(base_delay, last_error)
                    if attempt == attempts:
                        raise ImgBedError(last_error) from ext_err
                    delay = min(base_delay * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
                    logger.warning("%s; retry in %.1fs", last_error, delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code < 400:
                    try:
                        url_out = self._parse_upload_body(resp)
                    except ImgBedError as parse_exc:
                        last_error = str(parse_exc)
                        _mark_channel_failure(base_delay, resp.text)
                        if attempt == attempts or not _should_retry(
                            resp.status_code, resp.text
                        ):
                            raise
                    else:
                        _mark_success()
                        return url_out
                else:
                    last_error = format_upload_failure(resp.status_code, resp.text)
                    _mark_channel_failure(base_delay, resp.text)
                    if attempt == attempts or not _should_retry(resp.status_code, resp.text):
                        raise ImgBedError(last_error)
                delay = min(base_delay * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
                logger.warning(
                    "ImgBed %s; retry %s/%s in %.1fs",
                    last_error[:180],
                    attempt,
                    attempts - 1,
                    delay,
                )
                await asyncio.sleep(delay)
        raise ImgBedError(last_error)

    def _parse_upload_body(self, resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except Exception as exc:
            raise ImgBedError(
                format_upload_failure(resp.status_code, resp.text)
            ) from exc

        if isinstance(body, list) and body:
            return self.resolve_url(body[0])
        if isinstance(body, dict):
            if "src" in body or "publicUrl" in body or "url" in body:
                return self.resolve_url(body)
            data_field = body.get("data")
            if isinstance(data_field, list) and data_field:
                return self.resolve_url(data_field[0])
            if isinstance(data_field, dict):
                return self.resolve_url(data_field)
        raise ImgBedError(f"Unexpected upload response: {body!r}")
