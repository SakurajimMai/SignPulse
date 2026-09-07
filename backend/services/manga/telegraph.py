"""从频道正文里的 Telegraph 链接拉取整话图片。"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

import httpx

from backend.utils.outbound import httpx_async_client_kwargs

logger = logging.getLogger(__name__)

TELEGRAPH_HOSTS = {
    "telegra.ph",
    "www.telegra.ph",
    "te.legra.ph",
    "www.te.legra.ph",
    "graph.org",
    "www.graph.org",
}
TELEGRAPH_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:telegra\.ph|te\.legra\.ph|graph\.org)/[^\s<>\]\)\"']+",
    re.I,
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
# imgbox 宕机/审核拦截时会返回同一张 240×240、约 8KB 的 JPEG 占位图。
MIN_PAGE_BYTES = 15_000
MIN_PAGE_EDGE = 320
PLACEHOLDER_EDGE = 240
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,image/avif,image/webp,image/*;q=0.8,*/*;q=0.5",
}


def _clean_url(raw: str) -> str:
    value = unquote(str(raw or "").strip())
    value = value.rstrip(").,，。;；\"'")
    return value


def ascii_referer(raw: str) -> str:
    """httpx 的 Referer 必须是 ASCII；中文 Telegraph 路径要做百分号编码。"""
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        pass
    parsed = urlparse(value)
    scheme = parsed.scheme or "https"
    host = parsed.netloc or "telegra.ph"
    path = quote(unquote(parsed.path or "/"), safe="/-_.~")
    query = quote(parsed.query, safe="=&%-_.~") if parsed.query else ""
    encoded = urlunparse((scheme, host, path or "/", "", query, ""))
    try:
        encoded.encode("ascii")
        return encoded
    except UnicodeEncodeError:
        return f"{scheme}://{host}/"


def request_headers(*, referer: str = "") -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    cleaned = ascii_referer(referer)
    if cleaned:
        headers["Referer"] = cleaned
    return headers


TELEGRAPH_PAGE_ID_FACTOR = 10_000


def encode_telegraph_page_message_id(message_id: int, index: int) -> int:
    mid = int(message_id or 0)
    return mid * TELEGRAPH_PAGE_ID_FACTOR + int(index) if mid else int(index)


def decode_telegraph_page_message_id(stored: int | None) -> int | None:
    """Recover the source channel message id from a stored Telegraph page id."""
    if stored is None:
        return None
    try:
        value = int(stored)
    except (TypeError, ValueError):
        return None
    if value >= TELEGRAPH_PAGE_ID_FACTOR:
        return value // TELEGRAPH_PAGE_ID_FACTOR
    return value if value > 0 else None


def is_telegraph_url(url: str) -> bool:
    parsed = urlparse(_clean_url(url))
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").strip("/")
    return host in {item.removeprefix("www.") for item in TELEGRAPH_HOSTS} and bool(path)


def telegraph_page_path(url: str) -> str | None:
    parsed = urlparse(_clean_url(url))
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host not in {item.removeprefix("www.") for item in TELEGRAPH_HOSTS}:
        return None
    path = unquote((parsed.path or "").lstrip("/"))
    return path or None


def normalize_telegraph_image_url(src: str, *, page_url: str = "https://telegra.ph/") -> str | None:
    raw = str(src or "").strip()
    if not raw or raw.startswith("data:"):
        return None
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif raw.startswith("/"):
        raw = urljoin(page_url, raw)
    if not raw.startswith(("http://", "https://")):
        return None
    return raw


def collect_telegraph_image_urls(content: Any, *, page_url: str = "https://telegra.ph/") -> list[str]:
    """Walk Telegraph Node content and keep image src in document order."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        tag = str(node.get("tag") or "").lower()
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        if tag == "img":
            src = normalize_telegraph_image_url(
                str(attrs.get("src") or attrs.get("data-src") or ""),
                page_url=page_url,
            )
            if src and src not in seen:
                seen.add(src)
                found.append(src)
        children = node.get("children")
        if children is not None:
            walk(children)

    walk(content)
    return found


def extract_telegraph_urls_from_text(text: str | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in TELEGRAPH_URL_RE.findall(text or ""):
        cleaned = _clean_url(match)
        if is_telegraph_url(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls


def _utf16_slice(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    start = max(int(offset), 0) * 2
    end = start + max(int(length), 0) * 2
    return encoded[start:end].decode("utf-16-le", errors="ignore")


def _entity_url(text: str, entity: Any) -> str | None:
    direct = getattr(entity, "url", None)
    if direct:
        return str(direct)
    ent_type = str(getattr(entity, "type", "") or type(entity).__name__).lower()
    if "url" in ent_type and "text" not in ent_type and "mention" not in ent_type:
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if offset is not None and length is not None and text:
            return _utf16_slice(text, offset, length)
    return None


def _iter_markup_urls(markup: Any) -> Iterable[str]:
    keyboard = getattr(markup, "inline_keyboard", None) or []
    for row in keyboard:
        for button in row or []:
            url = getattr(button, "url", None)
            if url:
                yield str(url)


def extract_telegraph_urls_from_message(message: Any) -> list[str]:
    """Caption / entities / inline buttons / link preview 里的 telegra.ph 链接。"""
    from .filters import message_text

    text = message_text(message)
    found: list[str] = extract_telegraph_urls_from_text(text)
    seen = set(found)

    def add(raw: str | None) -> None:
        cleaned = _clean_url(raw or "")
        if cleaned and is_telegraph_url(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)

    for attr in ("entities", "caption_entities"):
        for entity in getattr(message, attr, None) or []:
            add(_entity_url(text, entity))

    for url in _iter_markup_urls(getattr(message, "reply_markup", None)):
        add(url)

    webpage = getattr(message, "web_page", None)
    if webpage is not None:
        add(getattr(webpage, "url", None))
        add(getattr(webpage, "display_url", None))

    return found


JS_GALLERY_BASE = re.compile(
    r'''(?:const|let|var)\s+base\s*=\s*["'](https?://[^"']+)["']''',
    re.I,
)
JS_GALLERY_LOOP = re.compile(
    r'''for\s*\(\s*(?:(?:let|var|const)\s+)?i\s*=\s*(\d+)\s*;\s*i\s*<=\s*(\d+)''',
    re.I,
)
JS_GALLERY_PAD = re.compile(r'''padStart\(\s*(\d+)\s*,\s*['"]0['"]\s*\)''', re.I)
JS_GALLERY_EXT = re.compile(
    r'''base\s*\+\s*\w+\s*\+\s*["'](\.[A-Za-z0-9]+)["']''',
    re.I,
)


HTML_TITLE = re.compile(r"<title>([^<]+)</title>", re.I)
GALLERY_FILE = re.compile(r"^(https?://.*/)(\d+)(\.[A-Za-z0-9]+)(?:\?.*)?$", re.I)


def parse_html_title(html: str | None) -> str | None:
    match = HTML_TITLE.search(html or "")
    if not match:
        return None
    from .filters import clean_display_title

    title = clean_display_title(match.group(1))
    return title or None


def sibling_image_url(url: str) -> str | None:
    raw = str(url or "").split("?", 1)[0]
    lowered = raw.lower()
    if lowered.endswith(".jpg"):
        return raw[:-4] + ".png"
    if lowered.endswith(".jpeg"):
        return raw[:-5] + ".png"
    if lowered.endswith(".png"):
        return raw[:-4] + ".jpg"
    return None


@dataclass(frozen=True)
class ReaderPage:
    image_urls: list[str]
    html_title: str | None = None


def parse_js_image_gallery(html: str) -> list[str]:
    """8-ckp.pages.dev 这类阅读页用 JS 循环拼 R2 图片，HTML 里没有 <img>。"""
    base_match = JS_GALLERY_BASE.search(html or "")
    loop_match = JS_GALLERY_LOOP.search(html or "")
    if not base_match or not loop_match:
        return []
    start = int(loop_match.group(1))
    end = int(loop_match.group(2))
    if end < start or end - start > 5000:
        return []
    base = base_match.group(1).rstrip("/") + "/"
    pad_match = JS_GALLERY_PAD.search(html or "")
    width = int(pad_match.group(1)) if pad_match else 3
    width = min(max(width, 1), 8)
    ext_match = JS_GALLERY_EXT.search(html or "")
    ext = (ext_match.group(1) if ext_match else ".jpg").lower()
    if not ext.startswith("."):
        ext = f".{ext}"
    return [f"{base}{str(index).zfill(width)}{ext}" for index in range(start, end + 1)]


def html_image_urls(html: str, *, page_url: str = "https://telegra.ph/") -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.findall(
        r"""<(?:img|source)[^>]+(?:src|data-src|data-original|data-lazy-src|data-url|data-image)\s*=\s*["']([^"']+)["']""",
        html,
        flags=re.I,
    ):
        src = normalize_telegraph_image_url(match, page_url=page_url)
        if not src or src in seen:
            continue
        lowered = src.lower()
        if lowered.endswith(".svg") or any(
            token in lowered for token in ("emoji", "/icon", "favicon", "1x1", "pixel.gif")
        ):
            continue
        seen.add(src)
        urls.append(src)
    return urls


ONLINE_READ_LABEL = re.compile(
    r"在线阅读|線上閱讀|在線閱讀|线上阅读|在telegraph观看|telegraph\s*观看",
    re.I,
)
DOWNLOAD_LABEL = re.compile(r"下载|下載|网盘|網盤|资源|資源", re.I)


def unwrap_reader_url(url: str) -> str:
    cleaned = _clean_url(url)
    parsed = urlparse(cleaned)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host in {"t.me", "telegram.me"} and parsed.path.rstrip("/") == "/iv":
        nested = (parse_qs(parsed.query).get("url") or [""])[0]
        if nested:
            return _clean_url(nested)
    return cleaned


def is_reader_url(url: str) -> bool:
    cleaned = unwrap_reader_url(url)
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host in {"t.me", "telegram.me"}:
        return parsed.path.rstrip("/") == "/iv"
    suffix = Path(parsed.path).suffix.lower()
    if suffix in {".zip", ".rar", ".7z", ".pdf", ".mp4", ".mkv", ".torrent"}:
        return False
    return True


def reader_source_identity(page_url: str) -> str:
    cleaned = unwrap_reader_url(page_url)
    path = telegraph_page_path(cleaned)
    if path:
        return path
    return hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:20]


def _button_label(button: Any) -> str:
    return str(getattr(button, "text", None) or getattr(button, "label", None) or "")


def message_has_inline_buttons(message: Any) -> bool:
    markup = getattr(message, "reply_markup", None)
    keyboard = getattr(markup, "inline_keyboard", None) or []
    return any(bool(row) for row in keyboard)


def extract_online_read_urls_from_message(message: Any) -> list[str]:
    """只收「在线阅读 / 在telegraph观看」按钮或文字链，忽略下载资源。"""
    from .filters import message_text

    text = message_text(message)
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        cleaned = unwrap_reader_url(raw or "")
        if cleaned and is_reader_url(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)

    markup = getattr(message, "reply_markup", None)
    keyboard = getattr(markup, "inline_keyboard", None) or []
    for row in keyboard:
        for button in row or []:
            label = _button_label(button)
            url = getattr(button, "url", None)
            if not url:
                continue
            if DOWNLOAD_LABEL.search(label) and not ONLINE_READ_LABEL.search(label):
                continue
            if ONLINE_READ_LABEL.search(label):
                add(url)

    for attr in ("entities", "caption_entities"):
        for entity in getattr(message, attr, None) or []:
            url = _entity_url(text, entity)
            if not url:
                continue
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            label = ""
            if offset is not None and length is not None and text:
                label = _utf16_slice(text, offset, length)
            if ONLINE_READ_LABEL.search(label or ""):
                add(url)
    return found


def resolve_reader_urls_from_message(message: Any) -> tuple[list[str], str]:
    """有按钮的帖必须带「在线阅读」；纯 Telegraph 旧帖仍可用正文里的 telegra.ph。"""
    labeled = extract_online_read_urls_from_message(message)
    if labeled:
        return labeled, "online-read"
    if message_has_inline_buttons(message):
        return [], "missing-online-read"
    telegraph = extract_telegraph_urls_from_message(message)
    if telegraph:
        return telegraph, "telegraph"
    return [], "missing-reader"


@dataclass(frozen=True)
class DownloadedImage:
    path: Path
    sha256: str
    size: int
    width: int
    height: int
    valid: bool
    reason: str = ""


def inspect_image_file(path: Path) -> DownloadedImage:
    """判断下载结果是不是真正的漫画页，而不是占位图/HTML/损坏文件。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return DownloadedImage(path, "", 0, 0, 0, False, f"unreadable:{exc}")
    sha = hashlib.sha256(raw).hexdigest()
    size = len(raw)
    if size < 64:
        return DownloadedImage(path, sha, size, 0, 0, False, "empty")
    stripped = raw.lstrip()
    if stripped.startswith((b"<", b"{", b"<!")):
        return DownloadedImage(path, sha, size, 0, 0, False, "not-an-image")
    try:
        from PIL import Image

        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
            image.load()
    except Exception:
        return DownloadedImage(path, sha, size, 0, 0, False, "decode-failed")
    if width <= PLACEHOLDER_EDGE and height <= PLACEHOLDER_EDGE:
        return DownloadedImage(path, sha, size, width, height, False, "placeholder-size")
    if size < MIN_PAGE_BYTES and min(width, height) < MIN_PAGE_EDGE:
        return DownloadedImage(path, sha, size, width, height, False, "too-small")
    if min(width, height) < 200:
        return DownloadedImage(path, sha, size, width, height, False, "tiny-edge")
    return DownloadedImage(path, sha, size, width, height, True, "ok")


def chapter_images_usable(
    inspected: list[DownloadedImage],
    *,
    expected: int,
) -> tuple[bool, str]:
    valid = [item for item in inspected if item.valid]
    if expected < 1:
        return False, "no images listed"
    if not valid:
        return False, "no valid images"
    hashes = [item.sha256 for item in inspected if item.sha256]
    if len(hashes) >= 3:
        _digest, count = Counter(hashes).most_common(1)[0]
        if count / len(hashes) >= 0.6:
            return False, f"repeated image {count}/{len(hashes)}"
    if len(valid) < 3:
        return False, f"only {len(valid)} valid page(s)"
    if expected >= 5 and len(valid) / expected < 0.5:
        return False, f"valid {len(valid)}/{expected} below half"
    return True, "ok"


class TelegraphClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            **httpx_async_client_kwargs(
                timeout=httpx.Timeout(60.0, connect=20.0),
                follow_redirects=True,
                headers=DEFAULT_HEADERS,
            )
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_image_urls(self, page_url: str, *, limit: int = 10000) -> list[str]:
        page = await self.fetch_reader_page(page_url, limit=limit)
        return page.image_urls

    async def fetch_reader_page(
        self,
        page_url: str,
        *,
        limit: int = 10000,
        page_hint: int | None = None,
    ) -> ReaderPage:
        cleaned = unwrap_reader_url(page_url)
        cap = max(int(limit), 1)
        if is_telegraph_url(cleaned):
            images = await self._fetch_telegraph_images(cleaned, limit=cap)
            return ReaderPage(images)
        try:
            response = await self._client.get(cleaned)
            response.raise_for_status()
        except Exception:
            logger.exception("Reader page fetch failed for %s", cleaned)
            return ReaderPage([])
        final_url = str(response.url or cleaned)
        html_title = parse_html_title(response.text)
        if is_telegraph_url(final_url):
            images = await self._fetch_telegraph_images(final_url, limit=cap)
            return ReaderPage(images, html_title)
        images = parse_js_image_gallery(response.text)
        if not images:
            images = html_image_urls(response.text, page_url=final_url)
        if images:
            images = await self.extend_image_gallery(
                images,
                referer=final_url,
                limit=cap,
                page_hint=page_hint,
            )
        if not images:
            logger.warning("Reader page has no images url=%s", final_url)
        return ReaderPage(images[:cap], html_title)

    async def extend_image_gallery(
        self,
        urls: list[str],
        *,
        referer: str,
        limit: int,
        page_hint: int | None = None,
    ) -> list[str]:
        """JS 常写死 i<=100；R2 上若还有 101.jpg/png 就继续补。"""
        if not urls:
            return urls
        match = GALLERY_FILE.match(urls[-1].split("?", 1)[0])
        if not match:
            return urls
        prefix, digits, ext = match.group(1), match.group(2), match.group(3)
        width = len(digits)
        start = int(digits)
        index = start
        found = list(urls)
        seen = set(found)
        misses = 0
        alt = sibling_image_url(f"{prefix}{digits}{ext}")
        alt_ext = Path(alt).suffix if alt else ""
        hard_cap = min(max(int(limit), 1), max(start + 2000, int(page_hint or 0)))
        while index < hard_cap and len(found) < limit:
            index += 1
            hit = None
            for suffix in (ext, alt_ext):
                if not suffix:
                    continue
                url = f"{prefix}{str(index).zfill(width)}{suffix}"
                if await self.url_exists(url, referer=referer):
                    hit = url
                    break
            if hit is None:
                misses += 1
                if misses >= 3:
                    break
                continue
            misses = 0
            if hit not in seen:
                seen.add(hit)
                found.append(hit)
        if len(found) > len(urls):
            logger.info(
                "Reader gallery extended %d -> %d referer=%s",
                len(urls),
                len(found),
                referer,
            )
        return found

    async def download_reader_file(
        self, url: str, dest: Path, *, referer: str = "https://telegra.ph/"
    ) -> Path:
        candidates = [url]
        alt = sibling_image_url(url)
        if alt:
            candidates.append(alt)
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                return await self.download_file(candidate, dest, referer=referer)
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 404:
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError(f"empty reader image {url}")

    async def _fetch_telegraph_images(self, page_url: str, *, limit: int) -> list[str]:
        cleaned = _clean_url(page_url)
        path = telegraph_page_path(cleaned)
        if not path:
            return []
        if Path(path).suffix.lower() in IMAGE_SUFFIXES:
            return [cleaned][:limit]

        api_url = f"https://api.telegra.ph/getPage/{quote(path, safe='-_.~')}?return_content=true"
        try:
            response = await self._client.get(api_url)
            response.raise_for_status()
            body = response.json()
        except Exception:
            logger.warning("Telegraph API failed for %s, falling back to HTML", cleaned, exc_info=True)
            body = None

        images: list[str] = []
        if isinstance(body, dict) and body.get("ok") and isinstance(body.get("result"), dict):
            result = body["result"]
            page = str(result.get("url") or cleaned)
            images = collect_telegraph_image_urls(result.get("content"), page_url=page)

        if not images:
            html_url = f"https://telegra.ph/{quote(path, safe='-_.~')}"
            try:
                response = await self._client.get(html_url, headers={"Referer": "https://telegra.ph/"})
                response.raise_for_status()
                images = html_image_urls(response.text, page_url=html_url)
            except Exception:
                logger.exception("Telegraph HTML fallback failed for %s", cleaned)
                return []

        return images[:limit]

    async def url_exists(self, url: str, *, referer: str = "https://telegra.ph/") -> bool:
        headers = request_headers(referer=referer)
        try:
            response = await self._client.head(url, headers=headers)
            if response.status_code in {200, 204}:
                return True
            if response.status_code in {404, 410}:
                return False
        except Exception:
            pass
        try:
            response = await self._client.get(url, headers=headers)
            return response.status_code == 200 and bool(response.content)
        except Exception:
            return False

    async def download_file(self, url: str, dest: Path, *, referer: str = "https://telegra.ph/") -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        response = await self._client.get(url, headers=request_headers(referer=referer))
        response.raise_for_status()
        data = response.content
        if not data:
            raise RuntimeError(f"empty telegraph image {url}")
        dest.write_bytes(data)
        return dest
