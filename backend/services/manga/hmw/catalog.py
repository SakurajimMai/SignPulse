"""HMW 发布历史本机目录。

与频道采集的 tg_manga.db `mangas` 表隔离，只记通过本面板发到 HMW 的作品，
以及从 Publisher API 同步回来的条目。存储为 data/hmw/catalog.json。
"""
from __future__ import annotations

import math
import threading
from typing import Any

from backend.services.manga.config import MangaSettings
from backend.utils.atomic_io import read_json_safe, write_json_atomic
from backend.utils.time import utc_now_iso_z

from .paths import hmw_dirs
from .service import public_site_url

_lock = threading.RLock()


def catalog_path(settings: MangaSettings):
    return hmw_dirs(settings).root / "catalog.json"


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _load(settings: MangaSettings) -> dict[str, Any]:
    raw = read_json_safe(catalog_path(settings), default={"items": []})
    if not isinstance(raw, dict):
        return {"items": []}
    items = raw.get("items")
    if not isinstance(items, list):
        items = []
    return {"items": [item for item in items if isinstance(item, dict)]}


def _save(settings: MangaSettings, data: dict[str, Any]) -> None:
    write_json_atomic(catalog_path(settings), {"items": list(data.get("items") or [])})


def normalize_chapter(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _as_int(raw.get("id")),
        "number": _as_float(raw.get("number")),
        "title": str(raw.get("title") or ""),
        "page_count": _as_int(raw.get("page_count") or raw.get("pages"), 0) or 0,
    }


def _manga_blob(detail: dict[str, Any]) -> dict[str, Any]:
    nested = detail.get("manga")
    if isinstance(nested, dict):
        return nested
    return detail


def entry_from_detail(
    detail: dict[str, Any],
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extras = extras or {}
    manga = _manga_blob(detail)
    raw_chapters = detail.get("chapters")
    if not isinstance(raw_chapters, list):
        raw_chapters = manga.get("chapters") if isinstance(manga.get("chapters"), list) else []
    chapters = [normalize_chapter(item) for item in raw_chapters if isinstance(item, dict)]
    chapters.sort(key=lambda item: (item["number"], item.get("id") or 0))
    manga_id = _as_int(manga.get("id") or detail.get("id") or extras.get("id"))
    slug = str(manga.get("slug") or extras.get("slug") or "").strip()
    language = str(manga.get("language") or extras.get("language") or "zh").strip() or "zh"
    page_count = sum(int(item.get("page_count") or 0) for item in chapters)
    if not page_count:
        page_count = _as_int(manga.get("page_count") or extras.get("page_count"), 0) or 0
    chapter_count = len(chapters) or _as_int(manga.get("chapter_count") or extras.get("chapter_count"), 0) or 0
    now = utc_now_iso_z()
    public_url = extras.get("public_url") or manga.get("public_url")
    if not public_url and slug:
        public_url = public_site_url(language, slug)
    return {
        "id": manga_id,
        "slug": slug,
        "title": str(manga.get("title") or extras.get("title") or slug),
        "author": str(manga.get("author") or extras.get("author") or ""),
        "language": language,
        "status": str(manga.get("status") or extras.get("status") or ""),
        "cover_url": manga.get("cover_url") or extras.get("cover_url") or None,
        "description": str(manga.get("description") or extras.get("description") or ""),
        "alternative_title": str(manga.get("alternative_title") or extras.get("alternative_title") or ""),
        "year": _as_int(manga.get("year") or extras.get("year")),
        "chapter_count": chapter_count,
        "page_count": page_count,
        "public_url": public_url,
        "updated_at": now,
        "last_published_at": extras.get("last_published_at") or now,
        "source_path": str(extras.get("source_path") or ""),
        "version": str(manga.get("version") or detail.get("version") or extras.get("version") or ""),
        "genre_ids": list(manga.get("genre_ids") or extras.get("genre_ids") or []),
        "source_url": str(manga.get("source_url") or extras.get("source_url") or ""),
        "chapters": chapters,
    }


def _index_of(items: list[dict[str, Any]], entry: dict[str, Any]) -> int | None:
    manga_id = entry.get("id")
    slug = str(entry.get("slug") or "").strip()
    for index, item in enumerate(items):
        if manga_id is not None and item.get("id") == manga_id:
            return index
        if slug and str(item.get("slug") or "").strip() == slug:
            return index
    return None


def upsert_from_detail(
    settings: MangaSettings,
    detail: dict[str, Any],
    extras: dict[str, Any] | None = None,
    *,
    published: bool = False,
) -> dict[str, Any]:
    entry = entry_from_detail(detail, extras)
    with _lock:
        data = _load(settings)
        items = data["items"]
        index = _index_of(items, entry)
        if index is None:
            items.append(entry)
        else:
            previous = items[index]
            if not entry.get("source_path"):
                entry["source_path"] = previous.get("source_path") or ""
            if not published:
                entry["last_published_at"] = (
                    previous.get("last_published_at") or entry.get("last_published_at")
                )
            if not entry.get("cover_url"):
                entry["cover_url"] = previous.get("cover_url")
            items[index] = entry
        _save(settings, data)
    return entry


def get_entry(settings: MangaSettings, manga_id: int) -> dict[str, Any] | None:
    with _lock:
        for item in _load(settings)["items"]:
            if item.get("id") == manga_id:
                return dict(item)
    return None


def remove_entry(settings: MangaSettings, manga_id: int) -> bool:
    with _lock:
        data = _load(settings)
        before = len(data["items"])
        data["items"] = [item for item in data["items"] if item.get("id") != manga_id]
        if len(data["items"]) == before:
            return False
        _save(settings, data)
        return True


def _sort_key(item: dict[str, Any]) -> str:
    return str(item.get("last_published_at") or item.get("updated_at") or "")


def list_catalog(
    settings: MangaSettings,
    *,
    q: str = "",
    page: int = 1,
    limit: int = 12,
) -> dict[str, Any]:
    needle = (q or "").strip().casefold()
    with _lock:
        items = list(_load(settings)["items"])
    if needle:
        items = [
            item
            for item in items
            if needle in str(item.get("title") or "").casefold()
            or needle in str(item.get("slug") or "").casefold()
            or needle in str(item.get("author") or "").casefold()
            or needle in str(item.get("alternative_title") or "").casefold()
        ]
    items.sort(key=_sort_key, reverse=True)
    total = len(items)
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 12), 100))
    start = (page - 1) * limit
    sliced = items[start : start + limit]
    return {
        "data": sliced,
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": max(1, math.ceil(total / limit)) if total else 0,
    }
