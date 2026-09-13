from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_CATEGORY_IDS = ("1",)
DEFAULT_BASE_URL = "https://www.wnacg.com"


@dataclass(frozen=True)
class WnacgCategory:
    id: str
    label: str
    group: str
    group_label: str
    parent_id: str | None = None
    cate_id: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "group_label": self.group_label,
            "parent_id": self.parent_id,
            "cate_id": self.cate_id,
        }


# 与 https://www.wnacg.com 导航分类一致。id=latest 表示「更新」总列表。
CATEGORIES: tuple[WnacgCategory, ...] = (
    WnacgCategory("latest", "最新更新", "latest", "更新", cate_id=None),
    WnacgCategory("5", "同人誌（全部）", "doujin", "同人誌", cate_id=5),
    WnacgCategory("1", "漢化", "doujin", "同人誌", parent_id="5", cate_id=1),
    WnacgCategory("12", "日語", "doujin", "同人誌", parent_id="5", cate_id=12),
    WnacgCategory("16", "English", "doujin", "同人誌", parent_id="5", cate_id=16),
    WnacgCategory("2", "CG畫集", "doujin", "同人誌", parent_id="5", cate_id=2),
    WnacgCategory("37", "AI圖集", "doujin", "同人誌", parent_id="5", cate_id=37),
    WnacgCategory("22", "3D漫畫", "doujin", "同人誌", parent_id="5", cate_id=22),
    WnacgCategory("3", "Cosplay", "doujin", "同人誌", parent_id="5", cate_id=3),
    WnacgCategory("6", "單行本（全部）", "tankobon", "單行本", cate_id=6),
    WnacgCategory("9", "漢化", "tankobon", "單行本", parent_id="6", cate_id=9),
    WnacgCategory("13", "日語", "tankobon", "單行本", parent_id="6", cate_id=13),
    WnacgCategory("17", "English", "tankobon", "單行本", parent_id="6", cate_id=17),
    WnacgCategory("7", "雜誌&短篇（全部）", "magazine", "雜誌&短篇", cate_id=7),
    WnacgCategory("10", "漢化", "magazine", "雜誌&短篇", parent_id="7", cate_id=10),
    WnacgCategory("14", "日語", "magazine", "雜誌&短篇", parent_id="7", cate_id=14),
    WnacgCategory("18", "English", "magazine", "雜誌&短篇", parent_id="7", cate_id=18),
    WnacgCategory("19", "韓漫（全部）", "korean", "韓漫", cate_id=19),
    WnacgCategory("20", "漢化", "korean", "韓漫", parent_id="19", cate_id=20),
    WnacgCategory("21", "其他", "korean", "韓漫", parent_id="19", cate_id=21),
)

CATEGORY_BY_ID = {item.id: item for item in CATEGORIES}
_SPLIT_RE = re.compile(r"[\s,，、;；]+")


def parse_category_ids(raw: str | None) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in _SPLIT_RE.split(str(raw or "")):
        key = item.strip()
        if key not in CATEGORY_BY_ID or key in seen:
            continue
        seen.add(key)
        values.append(key)
    return values


def serialize_category_ids(ids: list[str] | tuple[str, ...] | str | None) -> str:
    if isinstance(ids, str):
        parsed = parse_category_ids(ids)
    else:
        parsed = parse_category_ids(",".join(str(item) for item in (ids or ())))
    return ",".join(parsed)


def resolve_categories(raw: str | None) -> list[WnacgCategory]:
    selected = parse_category_ids(raw)
    if not selected:
        return []
    return [CATEGORY_BY_ID[item] for item in selected]


def normalize_base_url(raw: str | None) -> str:
    url = str(raw or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return DEFAULT_BASE_URL
    return url[:200]


def list_url(category: WnacgCategory, page: int, *, base: str = DEFAULT_BASE_URL) -> str:
    origin = normalize_base_url(base)
    page = max(int(page or 1), 1)
    if category.cate_id is None:
        if page <= 1:
            return f"{origin}/albums.html"
        return f"{origin}/albums-index-page-{page}.html"
    if page <= 1:
        return f"{origin}/albums-index-cate-{category.cate_id}.html"
    return f"{origin}/albums-index-page-{page}-cate-{category.cate_id}.html"


def album_url(aid: int, *, base: str = DEFAULT_BASE_URL) -> str:
    return f"{normalize_base_url(base)}/photos-index-aid-{int(aid)}.html"


def gallery_url(aid: int, *, base: str = DEFAULT_BASE_URL) -> str:
    return f"{normalize_base_url(base)}/photos-gallery-aid-{int(aid)}.html"
