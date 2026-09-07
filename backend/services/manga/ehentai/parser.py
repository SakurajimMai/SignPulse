from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

GALLERY_RE = re.compile(
    r"(?:https?://(?:e-hentai|exhentai)\.org)?/g/(\d+)/([0-9a-f]{8,})/?",
    re.I,
)
IMAGE_PAGE_RE = re.compile(
    r"(?:https?://(?:e-hentai|exhentai)\.org)?/s/([0-9a-f]{8,})/(\d+)-(\d+)",
    re.I,
)
TAG_HREF_RE = re.compile(
    r"""/tag/([^"'<>\s]+)""",
    re.I,
)
TITLE_RE = re.compile(
    r"""<h1\s+id=["'](gn|gj)["'][^>]*>(.*?)</h1>""",
    re.I | re.S,
)
PAGES_RE = re.compile(
    r"""(\d+)\s*pages?""",
    re.I,
)
IMG_RE = re.compile(
    r"""<img[^>]+id=["']img["'][^>]+src=["']([^"']+)["']"""
    r"""|<img[^>]+src=["']([^"']+)["'][^>]+id=["']img["']""",
    re.I,
)
PTB_PAGE_RE = re.compile(
    r"""<table[^>]*class=["'][^"']*ptb[^"']*["'][\s\S]*?</table>""",
    re.I,
)
DIGIT_HREF_RE = re.compile(r">(\d+)</a>", re.I)
SHOWKEY_RE = re.compile(r"""var\s+showkey\s*=\s*["']([^"']+)["']""", re.I)
NL_RE = re.compile(r"""nl\(['"]([^'"]+)['"]\)""", re.I)
CONTENT_WARNING_RE = re.compile(r"Content Warning", re.I)
STRIP_TAGS_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class GalleryRef:
    gid: int
    token: str
    url: str

    @property
    def source_key(self) -> str:
        return f"eh:{self.gid}/{self.token}"


@dataclass
class GalleryMeta:
    ref: GalleryRef
    title: str
    english_title: str | None = None
    page_count: int = 0
    tags: list[str] = field(default_factory=list)
    raw_tags: list[tuple[str, str]] = field(default_factory=list)
    artist: str | None = None
    image_pages: list[str] = field(default_factory=list)
    result_pages: int = 1
    showkey: str | None = None
    content_warning: bool = False


def _clean_text(raw: str) -> str:
    text = html.unescape(STRIP_TAGS_RE.sub("", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_gallery_ref(url: str, base: str = "https://e-hentai.org") -> GalleryRef | None:
    raw = (url or "").strip()
    if raw.lower().startswith("eh:"):
        rest = raw[3:].strip().strip("/")
        raw = f"/g/{rest}/"
    match = GALLERY_RE.search(raw)
    if not match:
        return None
    gid = int(match.group(1))
    token = match.group(2).lower()
    origin = urlparse(base).scheme + "://" + (urlparse(base).netloc or "e-hentai.org")
    return GalleryRef(gid=gid, token=token, url=f"{origin}/g/{gid}/{token}/")


def parse_search_results(html_text: str, base: str = "https://e-hentai.org") -> list[GalleryRef]:
    found: list[GalleryRef] = []
    seen: set[tuple[int, str]] = set()
    for match in GALLERY_RE.finditer(html_text or ""):
        gid = int(match.group(1))
        token = match.group(2).lower()
        key = (gid, token)
        if key in seen:
            continue
        seen.add(key)
        ref = parse_gallery_ref(f"/g/{gid}/{token}/", base)
        if ref is not None:
            found.append(ref)
    return found


def _titles(html_text: str) -> tuple[str, str | None]:
    english = ""
    japanese = ""
    for match in TITLE_RE.finditer(html_text or ""):
        kind = match.group(1).lower()
        value = _clean_text(match.group(2))
        if kind == "gn":
            english = value
        elif kind == "gj":
            japanese = value
    title = japanese or english
    return title, english or None


def _page_count(html_text: str) -> int:
    match = PAGES_RE.search(html_text or "")
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _result_pages(html_text: str) -> int:
    block = PTB_PAGE_RE.search(html_text or "")
    if not block:
        return 1
    numbers = [int(item) for item in DIGIT_HREF_RE.findall(block.group(0))]
    return max(numbers) if numbers else 1


def parse_raw_tags(html_text: str) -> list[tuple[str, str]]:
    tags: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in TAG_HREF_RE.findall(html_text or ""):
        decoded = html.unescape(raw.replace("+", " "))
        namespace, sep, name = decoded.partition(":")
        if not sep:
            namespace, name = "other", decoded
        key = (namespace.strip().lower(), name.strip())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        tags.append(key)
    return tags


def _origin(base: str) -> str:
    parsed = urlparse(base or "https://e-hentai.org")
    host = parsed.netloc or "e-hentai.org"
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}"


def parse_showpage_parts(url: str) -> tuple[str, int, int] | None:
    """返回 (imgkey, gid, page)。"""
    match = IMAGE_PAGE_RE.search(url or "")
    if not match:
        return None
    try:
        return match.group(1).lower(), int(match.group(2)), int(match.group(3))
    except ValueError:
        return None


def _gallery_thumb_html(html_text: str) -> str:
    """优先只扫缩略图区，避免评论/相关画廊里的 /s/ 链接混进来。"""
    text = html_text or ""
    start = re.search(r"""id=["']gdt["']""", text, re.I)
    if not start:
        return text
    rest = text[start.start() :]
    end = re.search(
        r"""id=["']cdiv["']|id=["']gnd["']|<table[^>]*class=["'][^"']*ptb""",
        rest,
        re.I,
    )
    if end:
        return rest[: end.start()]
    return rest[:80_000]


def parse_image_page_urls(
    html_text: str,
    base: str = "https://e-hentai.org",
    gid: int | None = None,
) -> list[str]:
    origin = _origin(base)
    scope = _gallery_thumb_html(html_text)
    by_page: dict[int, str] = {}
    for match in IMAGE_PAGE_RE.finditer(scope):
        imgkey, raw_gid, raw_page = match.group(1).lower(), match.group(2), match.group(3)
        try:
            page_gid = int(raw_gid)
            page_n = int(raw_page)
        except ValueError:
            continue
        if page_n <= 0:
            continue
        if gid is not None and page_gid != int(gid):
            continue
        if page_n in by_page:
            continue
        by_page[page_n] = f"{origin}/s/{imgkey}/{page_gid}-{page_n}"
    return [by_page[n] for n in sorted(by_page)]


def merge_image_pages(*batches: list[str]) -> list[str]:
    """按页码去重合并；同一页只保留先出现的 imgkey。"""
    by_page: dict[int, str] = {}
    owner: int | None = None
    for batch in batches:
        for url in batch or []:
            parts = parse_showpage_parts(url)
            if parts is None:
                continue
            _key, page_gid, page_n = parts
            if owner is None:
                owner = page_gid
            elif page_gid != owner:
                continue
            if page_n in by_page:
                continue
            by_page[page_n] = url
    return [by_page[n] for n in sorted(by_page)]


def prefer_showpage_url(thumbnail_url: str, previous_next: str | None) -> str:
    """上一张的 next 链和缩略图指向同一页时，优先用 next（imgkey 更新）。"""
    if not previous_next:
        return thumbnail_url
    thumb = parse_showpage_parts(thumbnail_url)
    nxt = parse_showpage_parts(previous_next)
    if thumb and nxt and thumb[1] == nxt[1] and thumb[2] == nxt[2]:
        return previous_next
    return thumbnail_url


NEXT_ID_RE = re.compile(
    r"""<a[^>]+id=["']next["'][^>]*href=["']([^"']+)["']"""
    r"""|<a[^>]+href=["']([^"']+)["'][^>]*id=["']next["']""",
    re.I,
)
NEXT_VAR_RE = re.compile(r"""var\s+nexturl\s*=\s*["']([^"']+)["']""", re.I)


def parse_next_image_page(html_text: str, base: str = "https://e-hentai.org") -> str | None:
    raw = ""
    match = NEXT_ID_RE.search(html_text or "")
    if match:
        raw = match.group(1) or match.group(2) or ""
    if not raw:
        var_match = NEXT_VAR_RE.search(html_text or "")
        raw = var_match.group(1) if var_match else ""
    if not raw:
        return None
    url = urljoin(_origin(base) + "/", html.unescape(raw))
    parts = parse_showpage_parts(url)
    if parts is None:
        return None
    imgkey, gid, page = parts
    return f"{_origin(base)}/s/{imgkey}/{gid}-{page}"


def parse_full_image_url(html_text: str) -> str | None:
    match = IMG_RE.search(html_text or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


def parse_nl_token(html_text: str) -> str | None:
    match = NL_RE.search(html_text or "")
    return match.group(1) if match else None


def has_content_warning(html_text: str) -> bool:
    return bool(CONTENT_WARNING_RE.search(html_text or ""))


def parse_gallery_page(html_text: str, url: str, base: str = "https://e-hentai.org") -> GalleryMeta | None:
    ref = parse_gallery_ref(url, base)
    if ref is None:
        return None
    title, english = _titles(html_text)
    raw_tags = parse_raw_tags(html_text)
    artist = next((name for ns, name in raw_tags if ns in {"artist", "group"}), None)
    showkey = None
    key_match = SHOWKEY_RE.search(html_text or "")
    if key_match:
        showkey = key_match.group(1)
    return GalleryMeta(
        ref=ref,
        title=title or f"gallery-{ref.gid}",
        english_title=english,
        page_count=_page_count(html_text),
        raw_tags=raw_tags,
        artist=artist,
        image_pages=parse_image_page_urls(html_text, base, gid=ref.gid),
        result_pages=_result_pages(html_text),
        showkey=showkey,
        content_warning=has_content_warning(html_text),
    )


def append_nl(url: str, token: str) -> str:
    if not token:
        return url
    joiner = "&" if "?" in url else "?"
    if "nl=" in url:
        return re.sub(r"nl=[^&]*", f"nl={token}", url, count=1)
    return f"{url}{joiner}nl={token}"


def absolute_url(url: str, base: str) -> str:
    return urljoin(base.rstrip("/") + "/", url)


def split_searches(raw: str) -> list[str]:
    queries: list[str] = []
    for line in (raw or "").replace("，", "\n").splitlines():
        for item in line.split(","):
            query = item.strip()
            if query and query not in queries:
                queries.append(query)
    return queries
