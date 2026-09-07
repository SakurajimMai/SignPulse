from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Archive / video / audio / other non-page payloads
BLOCKED_EXTENSIONS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".iso",
    ".mp3",
    ".flac",
    ".wav",
    ".aac",
    ".ogg",
    ".m4a",
    ".pdf",
    ".epub",
    ".apk",
    ".exe",
    ".dmg",
    ".torrent",
    ".m3u8",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".ts",
}

BLOCKED_MIME_PREFIXES = (
    "audio/",
    "application/zip",
    "application/x-zip",
    "application/x-rar",
    "application/vnd.rar",
    "application/x-7z",
    "application/x-tar",
    "application/gzip",
    "application/x-bzip",
    "application/pdf",
    "application/x-bittorrent",
    "application/vnd.android.package-archive",
)

# Promo / download posts in discussion groups (see comment_ex.png)
SPAM_TEXT_PATTERNS = [
    re.compile(r"点击下载", re.I),
    re.compile(r"點擊下載", re.I),
    re.compile(r"点击\s*.*下载", re.I),
    re.compile(r"點擊\s*.*下載", re.I),
    re.compile(r"⬇️+\s*点击", re.I),
    re.compile(r"网盘|網盤|夸克|百度云|百度網盤|阿里云盘|迅雷|磁力|magnet:", re.I),
    re.compile(r"下载链接|下載連結|下載鏈接", re.I),
    re.compile(r"\bhttps?://.*\.(zip|rar|7z)\b", re.I),
]

# A title card is the one useful kind of user-authored non-reply message when
# no sender allowlist is configured.
TITLE_CARD_CAPTION = re.compile(r"(?:\[[^\[\]]+\]|【[^【】]+】)[ \t]*\S+")
BRACKET_SEGMENT = r"(?:\[[^\[\]]*\]|【[^【】]*】)"
BOILERPLATE_LINE_PATTERNS = (
    re.compile(r"^\s*(?:更新|更新时间|来源于?|來源於?|整理|已整理|备注|備註)(?:\s*[:：,，].*|\s+.*)?$", re.I),
    re.compile(r"^\s*3\s*[dD]\s*漫画\s*(?:聊天|群组|群|频道)?\s*$", re.I),
    re.compile(r"^\s*.*(?:从第?一张内容开始|從第?一張內容開始).*\s*$", re.I),
    re.compile(r"^\s*作者\s*[:：].*$", re.I),
    re.compile(r"^\s*(?:语言|語言|页数|頁數|页數|男性|女性|标签|標籤)\s*[:：].*$", re.I),
    re.compile(r"^\s*E\s*站源\s*$", re.I),
    re.compile(r"telegraph\s*观看|在评论区观看|在評論區觀看|加入讨论群|收藏本作品|下载原图|搜索更多", re.I),
    # 频道模板行：空的或带值的「原作: / 角色:」都不能进标题
    re.compile(
        r"^\s*(?:原作|角色|艺术家|藝術家|其他|混合|汉化|漢化|译者|譯者)\s*[:：]\s*.*$",
        re.I,
    ),
)
AUTHOR_LINE = re.compile(r"作者\s*[:：]\s*#?\s*([^\s#/|]+)", re.I)
BOOK_COVER_TITLE = re.compile(
    r"《(?P<title>[^》]+)》(?P<extra>.*?)(?:\s*共\s*\d+\s*[Pp页頁])?\s*$",
    re.I,
)
PAGE_COUNT_SUFFIX = re.compile(r"(?:共\s*)?\d+\s*[Pp页頁]\s*$", re.I)
PAGE_COUNT_HINT = re.compile(r"共\s*(\d+)\s*[Pp页頁]", re.I)
_CN_NUM = r"(?:[零一二三四五六七八九十百千两]+\d*|\d+)"
_PLUS_KEEP = r"幕后图|视频|番外|其他|素材|无码|無碼|重制|重製"
_SHORT_PLUS = re.compile(rf"[+＋]\s*({_PLUS_KEEP})")
_CHAPTER_EXTRA = re.compile(
    rf"^(?:(?:\s*[+＋]\s*(?:{_PLUS_KEEP}))+|"
    rf"(?:\s*改)?\s*第\s*{_CN_NUM}\s*[话話章回部][A-Za-z]?|"
    rf"\s*[上下中]篇?|"
    rf"\s*\d+\s*[.．]\s*\d+|"
    rf"\s*\d{{1,3}}[A-Za-z]?)"
)
EMPTY_META_LABELS = (
    "原作",
    "角色",
    "艺术家",
    "藝術家",
    "其他",
    "混合",
    "汉化",
    "漢化",
    "译者",
    "譯者",
)
_META_LABEL_ALT = "|".join(re.escape(item) for item in EMPTY_META_LABELS)
TRAILING_EMPTY_META = re.compile(
    rf"(?:\s*(?:{_META_LABEL_ALT})\s*[:：]\s*)+$",
    re.I,
)
# 「原作： Order」「原作: 角色: EX站源」这类模板尾巴
TRAILING_META = re.compile(
    rf"(?:\s*(?:{_META_LABEL_ALT})\s*[:：][^\n]*)+$",
    re.I,
)


@dataclass
class ParsedCaption:
    title: str | None
    author: str | None
    tags: list[str]
    raw: str


def reply_to_id(message: Any) -> int | None:
    value = getattr(message, "reply_to_message_id", None)
    if value is None:
        value = getattr(message, "reply_to_msg_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def grouped_id(message: Any) -> int | None:
    value = getattr(message, "media_group_id", None)
    if value is None:
        value = getattr(message, "grouped_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def message_sender_id(message: Any) -> int | None:
    raw = getattr(message, "sender_id", None)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    user = getattr(message, "from_user", None)
    if user is not None and getattr(user, "id", None) is not None:
        return int(user.id)
    chat = getattr(message, "sender_chat", None)
    if chat is not None and getattr(chat, "id", None) is not None:
        return int(chat.id)
    return None


def _document(message: Any) -> Any | None:
    doc = getattr(message, "document", None)
    if doc is not None:
        return doc
    media = getattr(message, "media", None)
    return getattr(media, "document", None) if media is not None else None


def _has_photo(message: Any) -> bool:
    if getattr(message, "photo", None) is not None:
        return True
    media = getattr(message, "media", None)
    if media is None:
        return False
    return type(media).__name__ in {"MessageMediaPhoto", "Photo"}


def document_filename(message: Any) -> str | None:
    doc = _document(message)
    if not doc:
        return None
    direct = getattr(doc, "file_name", None)
    if direct:
        return str(direct)
    for attr in getattr(doc, "attributes", None) or []:
        name = getattr(attr, "file_name", None)
        if name:
            return str(name)
    return None


def parse_filter_keywords(raw: str | None) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    text = str(raw or "").replace("，", ",").replace("、", ",")
    for line in text.splitlines():
        for part in line.split(","):
            item = part.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                items.append(item)
    return items


def video_duration_seconds(message: Any) -> int | None:
    video = getattr(message, "video", None)
    duration = getattr(video, "duration", None) if video is not None else None
    if duration is None:
        media = getattr(message, "media", None)
        duration = getattr(media, "duration", None) if media is not None else None
    if duration is None:
        doc = _document(message)
        duration = getattr(doc, "duration", None) if doc is not None else None
        if duration is None:
            for attr in getattr(doc, "attributes", None) or []:
                if type(attr).__name__ == "DocumentAttributeVideo":
                    duration = getattr(attr, "duration", None)
                    break
    try:
        value = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None


def should_accept_outbound_video(
    *,
    text: str = "",
    duration: int | None = None,
    enabled: bool = True,
    binding_enabled: bool = True,
    block_keywords: list[str] | None = None,
    allow_keywords: list[str] | None = None,
    min_seconds: int = 0,
    max_seconds: int = 0,
) -> bool:
    """Whether a source video should be forwarded to the outbound channel."""
    if not enabled or not binding_enabled:
        return False
    blob = str(text or "")
    lowered = blob.lower()
    for keyword in block_keywords or []:
        if keyword and keyword.lower() in lowered:
            return False
    allow = [item for item in (allow_keywords or []) if item]
    if allow and not any(item.lower() in lowered for item in allow):
        return False
    if min_seconds and duration is not None and duration < int(min_seconds):
        return False
    if max_seconds and duration is not None and duration > int(max_seconds):
        return False
    return True


def is_video_message(message: Any) -> bool:
    """Telegram video / mp4. Goes to the outbound channel only, never the manga site."""
    if getattr(message, "video", None) is not None:
        return True
    media = getattr(message, "media", None)
    if media is not None and type(media).__name__ in {"MessageMediaVideo", "Video"}:
        return True
    doc = _document(message)
    if doc is None:
        return False
    mime = (getattr(doc, "mime_type", None) or "").lower()
    if mime.startswith("video/"):
        return True
    name = (document_filename(message) or "").lower()
    if any(name.endswith(ext) for ext in VIDEO_EXTENSIONS):
        return True
    for attr in getattr(doc, "attributes", None) or []:
        if type(attr).__name__ == "DocumentAttributeVideo":
            return True
    return False


def is_blocked_document(message: Any) -> bool:
    """True if media is archive/audio/etc. Videos are accepted separately."""
    if is_video_message(message):
        return False
    doc = _document(message)
    if doc is None:
        return False
    mime = (getattr(doc, "mime_type", None) or "").lower()
    for prefix in BLOCKED_MIME_PREFIXES:
        if mime.startswith(prefix) or mime == prefix.rstrip("/"):
            return True
    for attr in getattr(doc, "attributes", None) or []:
        if type(attr).__name__ == "DocumentAttributeAudio":
            return True
    name = (document_filename(message) or "").lower()
    for ext in BLOCKED_EXTENSIONS:
        if name.endswith(ext):
            return True
    return False


def is_image_message(message: Any, *, accept_image_documents: bool = True) -> bool:
    if is_video_message(message):
        return False
    if _has_photo(message):
        return True
    if is_blocked_document(message):
        return False
    doc = _document(message)
    if doc is None:
        return False
    mime = (getattr(doc, "mime_type", None) or "").lower()
    if mime.startswith("image/"):
        return True
    if accept_image_documents:
        name = (document_filename(message) or "").lower()
        return name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))
    return False


def message_text(message: Any) -> str:
    for attr in ("caption", "text", "message", "raw_text"):
        value = getattr(message, attr, None)
        if value:
            return str(value).strip()
    return ""


def is_spam_caption(text: str | None) -> bool:
    if not text:
        return False
    for pat in SPAM_TEXT_PATTERNS:
        if pat.search(text):
            return True
    return False


def is_channel_or_bot_sender(message: Any) -> bool:
    """Channel posts / bots are usually content; regular users often comments."""
    if getattr(message, "sender_chat", None) is not None:
        return True
    if getattr(message, "post", False):
        return True

    user = getattr(message, "from_user", None)
    if user is not None and (
        getattr(user, "is_bot", False) or getattr(user, "bot", False)
    ):
        return True

    # When Telethon has not resolved the sender entity yet, the peer type is
    # still available on the update and is enough to recognize channel posts.
    from_id = getattr(message, "from_id", None)
    if type(from_id).__name__ == "PeerChannel":
        return True

    sender = getattr(message, "sender", None)
    if sender is None:
        return False
    if getattr(sender, "bot", False) or getattr(sender, "is_bot", False):
        return True
    return bool(getattr(sender, "broadcast", False))


def should_skip_user_comment(
    message: Any,
    *,
    ignore_user_comments: bool,
    allowed_sender_ids: set[int],
) -> bool:
    """
    Drop ordinary user chat (comments like 'w') while keeping channel/bot posts.

    Rules when ignore_user_comments is True:
    - an explicit sender allowlist always wins
    - channel/bot senders are kept unless the caption is a download promotion
    - discussion albums (Telegram max 10 photos) are kept even as replies
    - a non-reply user title card is kept so a user-posted source can be used
    - single-image user replies are skipped
    """
    sender_id = message_sender_id(message)
    if allowed_sender_ids and sender_id is not None and sender_id in allowed_sender_ids:
        return False
    if allowed_sender_ids and (sender_id is None or sender_id not in allowed_sender_ids):
        # Strict allowlist mode
        return True

    text = message_text(message)
    if is_spam_caption(text):
        return True

    if is_channel_or_bot_sender(message) or not ignore_user_comments:
        return False

    if reply_to_id(message) is not None:
        # 讨论帖里的相册就是漫画正文；Telegram 一组最多 10 张，会分批发完。
        if grouped_id(message) is not None:
            return False
        # 无配文的单张续页也是正文；带短评的图（如「w」）仍当评论丢掉。
        if not text:
            return False
        return True

    # A non-reply album is commonly a user-posted source: only the first item
    # carries the title caption, while the remaining pages are captionless.
    if grouped_id(message) is not None:
        return False

    if TITLE_CARD_CAPTION.search(text):
        return False

    return True


def parse_caption(text: str | None) -> ParsedCaption:
    """
    Parse channel title cards like:
      [点点滴滴]浮乱看护士-臭脚控治疗篇 01（上）
      #女护士 #丝袜 #白丝

    Title = content outside all [brackets] (and without hashtags).
    Author = first [bracket] group (e.g. Ryx / 点点滴滴).
    """
    raw = (text or "").strip()
    if not raw:
        return ParsedCaption(title=None, author=None, tags=[], raw="")

    tags = re.findall(r"#([^\s#]+)", raw)
    seen_tags: list[str] = []
    for tag in tags:
        if tag not in seen_tags:
            seen_tags.append(tag)
    tags = seen_tags
    author = None
    author_line = AUTHOR_LINE.search(raw)
    if author_line:
        author = author_line.group(1).strip(" []【】") or None
    book_title = _book_cover_title(raw)
    if book_title:
        # 书封帖（漫画本子）没有作者栏：#Lula 是标签，方括号也不当作者。
        return ParsedCaption(title=book_title[:200], author=author, tags=tags, raw=raw)
    if not author:
        m = re.search(r"(?:\[([^\[\]]+)\]|【([^【】]+)】)", raw)
        if m:
            author = (m.group(1) or m.group(2)).strip() or None

    # Remove hashtag tokens
    cleaned = re.sub(r"#\S+", " ", raw)
    # Remove all [bracket] segments (author tags)
    cleaned = re.sub(BRACKET_SEGMENT, " ", cleaned)
    # Drop common boilerplate lines
    lines = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in BOILERPLATE_LINE_PATTERNS):
            continue
        if re.search(r"内容(?:和压缩包)?放在评论区|无法滑动翻页|telegram\s*x", line, re.I):
            continue
        if is_spam_caption(line):
            continue
        lines.append(line)
    title = " ".join(lines)
    title = clean_display_title(title)
    if not title:
        title = None
    else:
        title = title[:200]
    return ParsedCaption(title=title, author=author, tags=tags, raw=raw)


def _book_cover_title(text: str) -> str | None:
    """《补习老师的勾心斗角》 +幕后图 共 338 P -> 补习老师的勾心斗角 +幕后图
    《我妻柒柒》改 第十话 -> 我妻柒柒 改 第十话
    """
    for line in str(text or "").splitlines():
        match = BOOK_COVER_TITLE.search(line)
        if not match:
            continue
        title = (match.group("title") or "").strip()
        extra = _book_cover_extra(match.group("extra") or "")
        if extra:
            title = f"{title} {extra}".strip()
        title = clean_display_title(title)
        if title:
            return title
    return None


def _book_cover_extra(raw: str) -> str:
    extra = re.sub(r"#\S+", " ", str(raw or ""))
    extra = re.sub(r"\s+", " ", extra).strip()
    extra = PAGE_COUNT_SUFFIX.sub("", extra).strip(" -_|")
    if not extra:
        return ""
    match = _CHAPTER_EXTRA.match(extra)
    if match:
        return match.group(0).strip()
    if len(extra) <= 12:
        return extra
    return ""


def caption_page_hint(text: str | None) -> int | None:
    match = PAGE_COUNT_HINT.search(str(text or ""))
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def caption_plus_extras(text: str | None) -> str:
    """只保留书封行里短的 +幕后图 / +视频，丢掉长简介。"""
    first = str(text or "").splitlines()[0] if text else ""
    extras: list[str] = []
    for match in _SHORT_PLUS.finditer(first):
        token = (match.group(1) or "").strip()
        if token and f"+{token}" not in extras:
            extras.append(f"+{token}")
    return " ".join(extras)


def _fold_title(value: str) -> str:
    return re.sub(r"[\s·・—\-_.．]+", "", value).lower()


def merge_reader_title(
    caption_title: str | None,
    html_title: str | None,
    raw_caption: str | None = None,
) -> str | None:
    """频道书封名 + 在线阅读页 <title> 里的话数/上下篇。"""
    cap = clean_display_title(caption_title)
    html = clean_display_title(html_title)
    extras = caption_plus_extras(raw_caption)
    picked = cap
    if html:
        cap_f = _fold_title(cap)
        html_f = _fold_title(html)
        if cap_f and re.fullmatch(r"[a-z0-9]{2,20}", cap_f) and re.search(
            r"[\u4e00-\u9fff]", html
        ):
            picked = html
        elif not cap:
            picked = html
        elif html_f.startswith(cap_f) and len(html_f) > len(cap_f):
            picked = html
        elif cap_f.startswith(html_f):
            picked = cap
        elif cap_f[:4] and html_f.startswith(cap_f[:4]):
            if "话" in cap and "话" not in html:
                picked = cap
            else:
                picked = html if len(html_f) >= len(cap_f) else cap
    if extras:
        for extra in extras.split():
            if extra and extra not in picked:
                picked = f"{picked} {extra}".strip()
    return picked or None


def clean_display_title(text: str | None) -> str:
    """去掉「原作: / 角色:」模板尾巴（空值或带值），只留真正的作品名。"""
    title = re.sub(r"\s+", " ", str(text or "")).strip(" -_|")
    if not title:
        return ""
    prev = None
    while prev != title:
        prev = title
        title = TRAILING_META.sub("", title).strip(" -_|")
        title = TRAILING_EMPTY_META.sub("", title).strip(" -_|")
    return title


def extract_title_from_text(text: str | None) -> str | None:
    return parse_caption(text).title
