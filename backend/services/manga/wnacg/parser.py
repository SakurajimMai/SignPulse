from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from .categories import album_url, normalize_base_url

AID_RE = re.compile(r"photos-index-aid-(\d+)", re.I)
ITEM_RE = re.compile(r"""<li[^>]*gallary_item[\s\S]*?</li>""", re.I)
TITLE_LINK_RE = re.compile(
    r"""<a[^>]+photos-index-aid-\d+\.html[^>]*>([^<]+)</a>""",
    re.I,
)
ALT_RE = re.compile(r"""alt=["']([^"']+)["']""", re.I)
LIST_PAGES_RE = re.compile(r"(\d+)\s*張", re.I)
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.I | re.S)
CAT_LABEL_RE = re.compile(r"分類：([^<]+)")
PAGES_LABEL_RE = re.compile(r"頁數：\s*(\d+)\s*P", re.I)
TAG_RE = re.compile(
    r"""<a[^>]*class=["'][^"']*tagshow[^"']*["'][^>]*>([^<]+)</a>""",
    re.I,
)
IMGLIST_START_RE = re.compile(r"var\s+imglist\s*=\s*\[", re.I)
URL_FIELD_RE = re.compile(r'url:\s*"([^"]+)"')
EVENT_PREFIX_RE = re.compile(r"^\([^)]+\)\s*")
CIRCLE_ARTIST_RE = re.compile(r"^\[([^\[\]]+?)\s+\(([^()]+)\)\]\s*(.+)$")
BRACKET_AUTHOR_RE = re.compile(r"^\[([^\[\]]+)\]\s*(.+)$")
TRAILING_TAG_RE = re.compile(r"(?:\s*\[[^\[\]]+\])+$")
STRIP_TAGS_RE = re.compile(r"<[^>]+>")
PAGER_RE = re.compile(r"albums-index-page-(\d+)(?:-cate-\d+)?\.html", re.I)


@dataclass(frozen=True)
class AlbumRef:
    aid: int
    url: str
    title: str = ""
    page_count: int = 0

    @property
    def source_key(self) -> str:
        return f"wnacg:{self.aid}"


@dataclass
class AlbumMeta:
    ref: AlbumRef
    title: str
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    category: str = ""
    page_count: int = 0
    image_urls: list[str] = field(default_factory=list)


def _clean_text(raw: str) -> str:
    text = html.unescape(STRIP_TAGS_RE.sub("", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_album_ref(raw: str, base: str = "https://www.wnacg.com") -> AlbumRef | None:
    text = (raw or "").strip()
    if text.lower().startswith("wnacg:"):
        text = text[6:].strip().strip("/")
        if text.isdigit():
            aid = int(text)
            origin = normalize_base_url(base)
            return AlbumRef(aid=aid, url=album_url(aid, base=origin), title=f"wnacg:{aid}")
    match = AID_RE.search(text)
    if not match:
        if text.isdigit():
            aid = int(text)
            origin = normalize_base_url(base)
            return AlbumRef(aid=aid, url=album_url(aid, base=origin), title=f"wnacg:{aid}")
        return None
    aid = int(match.group(1))
    origin = normalize_base_url(base)
    return AlbumRef(aid=aid, url=album_url(aid, base=origin), title=f"wnacg:{aid}")


def parse_list_results(html_text: str, base: str = "https://www.wnacg.com") -> list[AlbumRef]:
    origin = normalize_base_url(base)
    found: list[AlbumRef] = []
    seen: set[int] = set()
    blocks = ITEM_RE.findall(html_text or "") or [html_text or ""]
    for block in blocks:
        match = AID_RE.search(block)
        if not match:
            continue
        aid = int(match.group(1))
        if aid in seen:
            continue
        title_match = TITLE_LINK_RE.search(block)
        alt_match = ALT_RE.search(block)
        title = _clean_text(title_match.group(1) if title_match else "") or _clean_text(
            alt_match.group(1) if alt_match else ""
        )
        pages_match = LIST_PAGES_RE.search(block)
        page_count = int(pages_match.group(1)) if pages_match else 0
        seen.add(aid)
        found.append(
            AlbumRef(
                aid=aid,
                url=album_url(aid, base=origin),
                title=title or f"wnacg:{aid}",
                page_count=page_count,
            )
        )
    return found


def parse_list_page_count(html_text: str) -> int:
    numbers = [int(item) for item in PAGER_RE.findall(html_text or "")]
    return max(numbers) if numbers else 1


def parse_title_author(raw: str) -> tuple[str, str | None]:
    text = _clean_text(raw)
    text = EVENT_PREFIX_RE.sub("", text).strip()
    match = CIRCLE_ARTIST_RE.match(text)
    if match:
        _circle, artist, rest = match.groups()
        return _strip_suffix_tags(rest) or text, artist.strip() or None
    match = BRACKET_AUTHOR_RE.match(text)
    if match:
        author, rest = match.groups()
        title = _strip_suffix_tags(rest)
        return title or text, author.strip() or None
    return _strip_suffix_tags(text) or text, None


def _strip_suffix_tags(text: str) -> str:
    stripped = TRAILING_TAG_RE.sub("", text or "").strip()
    return stripped or (text or "").strip()


def parse_album_page(html_text: str, url: str, base: str = "https://www.wnacg.com") -> AlbumMeta | None:
    ref = parse_album_ref(url, base)
    if ref is None:
        return None
    heading = H2_RE.search(html_text or "")
    raw_title = _clean_text(heading.group(1) if heading else "") or ref.title
    title, author = parse_title_author(raw_title)
    cat_match = CAT_LABEL_RE.search(html_text or "")
    pages_match = PAGES_LABEL_RE.search(html_text or "")
    tags = []
    seen: set[str] = set()
    for raw in TAG_RE.findall(html_text or ""):
        tag = _clean_text(raw)
        if not tag or tag in seen or tag.upper() == "TAG":
            continue
        seen.add(tag)
        tags.append(tag)
    page_count = int(pages_match.group(1)) if pages_match else 0
    return AlbumMeta(
        ref=AlbumRef(aid=ref.aid, url=ref.url, title=title, page_count=page_count),
        title=title,
        author=author,
        tags=tags[:30],
        category=_clean_text(cat_match.group(1) if cat_match else ""),
        page_count=page_count,
    )


def _abs_image_url(raw: str, base: str) -> str:
    url = (raw or "").strip().replace("\\/", "/")
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    origin = normalize_base_url(base)
    return urljoin(origin + "/", url.lstrip("/"))


def is_promo_image(url: str, caption: str = "") -> bool:
    blob = f"{url} {caption}".lower()
    if "/themes/" in blob or "shoucang" in blob:
        return True
    if "喜歡紳士漫畫" in caption or "加入收藏" in caption:
        return True
    return False


def parse_image_urls(html_text: str, base: str = "https://www.wnacg.com") -> list[str]:
    text = html_text or ""
    match = IMGLIST_START_RE.search(text)
    if not match:
        return []
    start = text.find("[", match.start())
    end = text.find("];", start)
    if start < 0 or end < 0:
        return []
    blob = text[start : end + 1].replace('\\"', '"').replace("fast_img_host+", "")
    found: list[str] = []
    seen: set[str] = set()
    for item in URL_FIELD_RE.finditer(blob):
        url = _abs_image_url(item.group(1), base)
        if not url or url in seen or is_promo_image(url):
            continue
        parsed = urlparse(url)
        if not parsed.netloc:
            continue
        seen.add(url)
        found.append(url)
    return found
