"""Coser 图集对象键：稳定、可网页访问的 CDN 路径。"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .config import CoserSettings

# 这些字符会出现在作品标题里，写进 S3 key / URL 后可能导致 IncompleteBody
# 或浏览器把路径当成 query/fragment 而无法打开。
UNSAFE_TITLE_CHARS = frozenset("()（）+&#%[]【】")
_PATH_BREAKERS = frozenset('\\/?:@=<>{}"\'`|,;*~$')
_MULTI_DOT = re.compile(r"\.{2,}")


def s3_prefix(settings: CoserSettings) -> str:
    raw = str(getattr(settings, "s3_prefix", "") or "").strip().strip("/")
    if not raw or raw == "uploads":
        return "coser"
    cleaned = sanitize_path_segment(raw, fallback="coser")
    return cleaned or "coser"


def sanitize_path_segment(text: str, *, fallback: str = "work") -> str:
    """去掉标题/名称中会破坏 URL 与 S3 上传的字符和空白。"""
    out: list[str] = []
    for char in str(text or ""):
        if char in UNSAFE_TITLE_CHARS or char in _PATH_BREAKERS:
            continue
        if char.isspace() or ord(char) < 32:
            continue
        if "A" <= char <= "Z":
            out.append(char.lower())
        else:
            out.append(char)
    cleaned = _MULTI_DOT.sub(".", "".join(out)).strip("._-")
    return cleaned[:80] or fallback


def titles_match(left: str, right: str) -> bool:
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if a.casefold() == b.casefold():
        return True
    sa = sanitize_path_segment(a, fallback="")
    sb = sanitize_path_segment(b, fallback="")
    return bool(sa) and sa == sb


def path_is_unsafe(url_or_key: str) -> bool:
    raw = str(url_or_key or "").strip()
    if not raw:
        return False
    if "://" in raw:
        raw = unquote(urlsplit(raw).path or "")
    else:
        raw = unquote(raw)
    return any(char in UNSAFE_TITLE_CHARS or char.isspace() for char in raw)


def image_filename(index: int, total: int = 0) -> str:
    width = max(3, len(str(max(int(total or 0), int(index or 1)))))
    return f"{int(index):0{width}d}.avif"


def gallery_folder(
    settings: CoserSettings,
    *,
    coser_name: str,
    work_id: int,
    title: str,
) -> str:
    prefix = s3_prefix(settings)
    person = sanitize_path_segment(coser_name, fallback="coser")
    safe_title = sanitize_path_segment(title, fallback="work")
    return f"{prefix}/{person}/{int(work_id)}{safe_title}"


def object_key(
    settings: CoserSettings,
    *,
    coser_name: str,
    work_id: int,
    title: str,
    index: int,
    total: int = 0,
) -> str:
    return f"{gallery_folder(settings, coser_name=coser_name, work_id=work_id, title=title)}/{image_filename(index, total)}"


def normalize_cdn_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return unquote(raw).rstrip("/")
    path = unquote(parts.path or "")
    return f"{parts.scheme}://{parts.netloc}{path}".rstrip("/")


def urls_match(left: list[str], right: list[str]) -> bool:
    a = [normalize_cdn_url(item) for item in left if str(item or "").strip()]
    b = [normalize_cdn_url(item) for item in right if str(item or "").strip()]
    return a == b
