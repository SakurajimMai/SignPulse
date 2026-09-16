from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

_URL = re.compile(r"https?://[^\s<>\[\]（）()]+", re.I)
_LABEL = re.compile(
    r"^\s*(?:【(?P<bracket>[^】]+)】)?\s*"
    r"(?P<key>游戏名称|游戏名|游戏title|游戏标题|名称|标题|title|"
    r"类型|标签|genre|分类|分類|"
    r"简介|介紹|介绍|游戏简介|概要|游戏概要|概述|"
    r"品牌|厂商|会社|社团名|"
    r"解压密码|解壓密碼|密码|密碼|提取码|提取碼|password|"
    r"语言|語言|平台|格式|文件格式|文件容量|文件大小|容量|"
    r"发售日|发行日|版本)"
    r"\s*[:：]?\s*(?P<value>.*)\s*$",
    re.I,
)
_TAG_SPLIT = re.compile(r"[,/|｜、，;；\s]+")
_JUNK_PREFIX = re.compile(
    r"^[\s\-—–·•\*#【\[\(（🔄❗⚠️⭐🌟✨🤍🤩〰️▎❗️❤💕🎀📥⬇️📍➗※★☆🍀]+"
)
_JUNK_LINE = re.compile(
    r"(?:入正地址|下载地址|下載地址|通常版本|ntr版本|"
    r"文件容量|文件大小|文件格式|"
    r"语言\s*[:：]|語言\s*[:：]|"
    r"tg\s*下载|tg\s*下載|"
    r"领取优惠|点此入正|點此入正)",
    re.I,
)
_PROMO_LINE = re.compile(
    r"(?:个人汉化|個人漢化|漢化組|汉化组|汉化补丁|"
    r"喜欢可支持|喜歡可支持|支持一下|求订阅|求訂閱|"
    r"求三连|欢迎订阅|歡迎訂閱|關注頻道|关注频道|"
    r"关注公众号|防失联|加群推广|请支持|打赏|贊助|赞助|"
    r"点个关注|點個關注|喜欢的话|"
    r"(?:^|[\s\(\[（【❗️!！])ps\s*[:：])",
    re.I,
)
_INTRO_KEYS = {
    "简介",
    "介紹",
    "介绍",
    "游戏简介",
    "概要",
    "游戏概要",
    "概述",
}
_SKIP_TAG_KEYS = {
    "语言",
    "語言",
    "格式",
    "文件格式",
    "文件容量",
    "文件大小",
    "容量",
}
_SKIP_TAGS = {
    "中文",
    "英文",
    "日文",
    "俄语",
    "俄語",
    "繁中",
    "简中",
    "chinese",
    "english",
    "japanese",
    "russian",
    "7z",
    "7z-zstd",
    "zstd",
    "apk",
    "zip",
    "rar",
}
_HASHTAG_ONLY = re.compile(r"^(?:#[^\s#]+\s*)+$")
_BRACKET_TITLE = re.compile(r"^【(?P<tag>[^】]+)】\s*(?P<title>.+)$")
_TITLE_DECORATION = re.compile(
    r"^[^\w\u3400-\u9fff]+|[^\w\u3400-\u9fff.!?。！？)）\]】]+$"
)
_HASHTAG = re.compile(r"#([^#\s]+)")
_STUDIO_IN_TEXT = re.compile(r"这是由\s*[\[【](?P<studio>[^\]】]+)[\]】]")


@dataclass
class ParsedGame:
    title: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    studio: str = ""
    password: str = ""
    urls: list[str] = field(default_factory=list)
    category_ids: list[int] = field(default_factory=list)
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "studio": self.studio,
            "password": self.password,
            "urls": list(self.urls),
            "category_ids": list(self.category_ids),
        }


def _clean_line(line: str) -> str:
    return _JUNK_PREFIX.sub("", str(line or "")).strip()


def _is_junk_line(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    unlabeled = _JUNK_PREFIX.sub("", text).strip()
    if _HASHTAG_ONLY.match(text) or _HASHTAG_ONLY.match(unlabeled):
        return True
    if _JUNK_LINE.search(text) or _JUNK_LINE.search(unlabeled):
        return True
    if _PROMO_LINE.search(text) or _PROMO_LINE.search(unlabeled):
        return True
    letters = re.sub(r"[^\w\u3400-\u9fff]+", "", unlabeled or text)
    return not letters


def clean_game_summary(text: str | None) -> str:
    """发布前再剥一层推广/入正/下载行，避免表单里残留频道话术。"""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        unlabeled = _clean_line(stripped)
        if _is_junk_line(stripped) or _is_junk_line(unlabeled):
            continue
        lines.append(stripped)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:8000]


def _uniq(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def split_tags(raw: str) -> list[str]:
    parts: list[str] = []
    for item in _TAG_SPLIT.split(str(raw or "")):
        value = item.strip().lstrip("#").strip()
        if value and value.casefold() not in _SKIP_TAGS:
            parts.append(value[:40])
    return _uniq(parts)[:12]


def suggest_categories(blob: str) -> list[int]:
    text = str(blob or "").casefold()
    ids: list[int] = []
    android = any(token in text for token in ("安卓", "android", ".apk", "apk"))
    pc = any(token in text for token in ("#pc", " pc", "pc+", "windows", "win版"))
    native = any(token in text for token in ("原生", "日文原版", "未汉化", "未漢化"))
    if pc or not android:
        ids.append(640)
    if android:
        ids.append(641)
    if native:
        ids.append(639)
    else:
        ids.append(637)
    return ids


def _strip_urls(text: str) -> str:
    return _URL.sub("", text).strip()


def sanitize_game_title(raw: str, fallback: str = "未命名游戏") -> str:
    """展示标题只清控制符和首尾装饰，保留 / 等分隔。"""
    text = _TITLE_DECORATION.sub("", _strip_urls(raw or "")).strip()
    text = re.sub(r"[\x00-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or fallback)[:160]


def parse_game_caption(text: str | None) -> ParsedGame:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    parsed = ParsedGame(raw=raw)
    if not raw:
        return parsed

    parsed.urls = _uniq(_URL.findall(raw))
    fields: dict[str, str] = {}
    body_lines: list[str] = []
    first_title = ""

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            if body_lines and body_lines[-1] != "":
                body_lines.append("")
            continue
        unlabeled = _clean_line(stripped)
        if _is_junk_line(stripped) or _is_junk_line(unlabeled):
            continue
        match = _LABEL.match(stripped) or _LABEL.match(unlabeled)
        if match:
            key = match.group("key").casefold()
            value = (match.group("value") or "").strip()
            bracket = (match.group("bracket") or "").strip()
            if bracket and not first_title:
                first_title = bracket
            if value:
                fields[key] = value
            elif key not in _INTRO_KEYS:
                body_lines.append(unlabeled or key)
            continue
        bracketed = _BRACKET_TITLE.match(stripped)
        if bracketed and not first_title:
            first_title = bracketed.group("title").strip()
            tag = bracketed.group("tag").strip()
            if tag:
                parsed.tags = _uniq([*parsed.tags, *split_tags(tag)])
            body_lines.append(stripped)
            continue
        cleaned = _clean_line(stripped)
        if cleaned and not first_title:
            first_title = cleaned
        body_lines.append(stripped)

    title = (
        fields.get("游戏名称")
        or fields.get("游戏名")
        or fields.get("游戏title")
        or fields.get("游戏标题")
        or fields.get("名称")
        or fields.get("标题")
        or fields.get("title")
        or first_title
    )
    parsed.title = sanitize_game_title(title or "")
    parsed.studio = (
        fields.get("品牌")
        or fields.get("厂商")
        or fields.get("会社")
        or fields.get("社团名")
        or ""
    ).strip().lstrip("#")
    if not parsed.studio:
        studio_match = _STUDIO_IN_TEXT.search(raw)
        if studio_match:
            parsed.studio = studio_match.group("studio").strip()
    parsed.password = (
        fields.get("解压密码")
        or fields.get("解壓密碼")
        or fields.get("密码")
        or fields.get("密碼")
        or fields.get("提取码")
        or fields.get("提取碼")
        or fields.get("password")
        or ""
    ).strip()

    field_tags: list[str] = []
    for key in ("类型", "标签", "genre", "分类", "分類"):
        field_tags.extend(split_tags(fields.get(key) or ""))
    line_tags: list[str] = []
    for line in raw.split("\n"):
        unlabeled = _clean_line(line)
        match = _LABEL.match(line.strip()) or _LABEL.match(unlabeled)
        if match and match.group("key").casefold() in _SKIP_TAG_KEYS:
            continue
        if _JUNK_LINE.search(line) or _JUNK_LINE.search(unlabeled):
            continue
        if _PROMO_LINE.search(line) or _PROMO_LINE.search(unlabeled):
            continue
        line_tags.extend(
            tag.strip(".,，。;；").lstrip("#")[:40] for tag in _HASHTAG.findall(line)
        )
    parsed.tags = [
        tag
        for tag in _uniq([*parsed.tags, *field_tags, *line_tags])
        if tag.casefold() not in _SKIP_TAGS
    ][:12]

    summary_bits: list[str] = []
    for key in ("简介", "介紹", "介绍", "游戏简介", "概要", "游戏概要", "概述"):
        if fields.get(key):
            summary_bits.append(fields[key])
    leftover = "\n".join(body_lines).strip()
    leftover = _strip_urls(leftover)
    if leftover:
        # 去掉已用作标题的首行，避免简介重复
        leftover_lines = leftover.split("\n")
        title_key = _TITLE_DECORATION.sub("", _clean_line(parsed.title)).strip()
        if leftover_lines and _TITLE_DECORATION.sub(
            "", _clean_line(leftover_lines[0])
        ).strip() == title_key:
            leftover_lines = leftover_lines[1:]
        leftover = "\n".join(
            line
            for line in leftover_lines
            if line.strip()
            and not _is_junk_line(line)
            and not _is_junk_line(_clean_line(line))
        ).strip()
        if leftover:
            summary_bits.append(leftover)
    parsed.summary = clean_game_summary(
        "\n\n".join(item.strip() for item in summary_bits if item.strip())
    )
    parsed.category_ids = suggest_categories(f"{parsed.title}\n{parsed.summary}\n{raw}")
    if not parsed.tags:
        blob = f"{parsed.title}\n{parsed.summary}".upper()
        for token in ("SLG", "RPG", "ADV", "GAL", "NTR", "3D"):
            if re.search(rf"\b{token}\b", blob) or token in blob:
                parsed.tags.append(token)
    return parsed


def host_label(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    if "baidu" in host or "pan.baidu" in host:
        return "baidu"
    if "pikpak" in host or "mypikpak" in host:
        return "pikpak"
    if "terabox" in host or "dubox" in host:
        return "terabox"
    if "quark" in host:
        return "quark"
    return "other"
