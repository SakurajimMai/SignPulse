from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from backend.services.manga.filters import parse_caption


class TelegramLinkError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramPostRef:
    channel: str | int
    post_id: int
    comment_id: int | None = None
    raw: str = ""


_TME_PATH = re.compile(
    r"^/(?:c/(?P<cid>\d+)|(?P<user>[A-Za-z0-9_]+))/(?P<post>\d+)/?$",
    re.I,
)
_CHAPTER_TAIL = re.compile(
    r"^(?P<head>.+?)[\s_\-／/]*"
    r"(?P<tail>(?:第\s*)?(?P<num>\d{1,4})(?:\.\d+)?\s*[话話章回部]?)\s*$",
    re.I,
)
_UNSAFE_FOLDER = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _as_channel_id(cid: str) -> int:
    value = int(cid)
    if value > 0:
        return int(f"-100{value}")
    return value


def parse_telegram_post_url(raw: str) -> TelegramPostRef:
    text = str(raw or "").strip()
    if not text:
        raise TelegramLinkError("链接不能为空")

    if text.startswith("tg://"):
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        domain = (query.get("domain") or [""])[0].strip()
        post = (query.get("post") or [""])[0].strip()
        comment = (query.get("comment") or [""])[0].strip()
        if not domain or not post.isdigit():
            raise TelegramLinkError("无法解析 tg 链接")
        return TelegramPostRef(
            channel=domain,
            post_id=int(post),
            comment_id=int(comment) if comment.isdigit() else None,
            raw=text,
        )

    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    host = (parsed.hostname or "").casefold()
    if host not in {"t.me", "telegram.me", "telegram.dog", "www.t.me"}:
        raise TelegramLinkError("请粘贴 t.me 频道帖链接")
    match = _TME_PATH.match(parsed.path or "")
    if not match:
        raise TelegramLinkError("链接需要包含频道和帖子编号，例如 t.me/manhua_3D/20061?comment=932161")
    post_id = int(match.group("post"))
    query = parse_qs(parsed.query)
    comment_raw = (query.get("comment") or [""])[0].strip()
    comment_id = int(comment_raw) if comment_raw.isdigit() else None
    if match.group("cid"):
        channel: str | int = _as_channel_id(match.group("cid"))
    else:
        channel = match.group("user")
    return TelegramPostRef(channel=channel, post_id=post_id, comment_id=comment_id, raw=raw)


def safe_folder_name(name: str, fallback: str = "untitled") -> str:
    cleaned = _UNSAFE_FOLDER.sub("_", str(name or "")).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:120] if cleaned else fallback) or fallback


def split_series_title(title: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", str(title or "").strip())
    if not text:
        return "untitled", "untitled"
    match = _CHAPTER_TAIL.match(text)
    if not match:
        return text, text
    head = match.group("head").strip(" -_／/")
    if not head or len(head) < 2:
        return text, text
    return head, text


def title_from_text(text: str | None) -> str | None:
    parsed = parse_caption(text)
    if parsed.title:
        return parsed.title.strip()
    raw = str(text or "").strip()
    if not raw:
        return None
    first = raw.splitlines()[0].strip()
    return first[:200] or None
