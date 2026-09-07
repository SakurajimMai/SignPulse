from __future__ import annotations

import math
import threading
from typing import Any

from backend.utils.atomic_io import read_json_safe, write_json_atomic
from backend.utils.time import utc_now_iso_z

from .config import CoserSettings
from .keys import titles_match
from .paths import coser_dirs

_lock = threading.RLock()


def catalog_path(settings: CoserSettings):
    return coser_dirs(settings).root / "catalog.json"


def _load(settings: CoserSettings) -> dict[str, Any]:
    raw = read_json_safe(catalog_path(settings), default={"items": []})
    if not isinstance(raw, dict):
        return {"items": []}
    items = raw.get("items")
    if not isinstance(items, list):
        items = []
    return {"items": [item for item in items if isinstance(item, dict)]}


def _save(settings: CoserSettings, data: dict[str, Any]) -> None:
    write_json_atomic(catalog_path(settings), {"items": list(data.get("items") or [])})


def upsert_entry(settings: CoserSettings, entry: dict[str, Any]) -> dict[str, Any]:
    now = utc_now_iso_z()
    payload = {
        "id": entry.get("id"),
        "title": str(entry.get("title") or ""),
        "summary": str(entry.get("summary") or "")[:400],
        "cover_url": entry.get("cover_url"),
        "public_url": entry.get("public_url"),
        "coser_id": entry.get("coser_id"),
        "coser_name": entry.get("coser_name") or "",
        "is_r18": bool(entry.get("is_r18")),
        "links": dict(entry.get("links") or {}),
        "source_url": entry.get("source_url") or "",
        "source_key": entry.get("source_key") or "",
        "job_id": entry.get("job_id") or "",
        "apate": bool(entry.get("apate")),
        "cdn_folder": entry.get("cdn_folder") or "",
        "image_count": entry.get("image_count") or 0,
        "updated_at": entry.get("updated_at") or now,
        "created_at": entry.get("created_at") or now,
    }
    with _lock:
        data = _load(settings)
        items = data["items"]
        replaced = False
        for index, item in enumerate(items):
            same_id = payload["id"] and item.get("id") == payload["id"]
            same_job = payload["job_id"] and item.get("job_id") == payload["job_id"]
            if same_id or same_job:
                payload["created_at"] = item.get("created_at") or payload["created_at"]
                items[index] = payload
                replaced = True
                break
        if not replaced:
            items.insert(0, payload)
        _save(settings, data)
    return payload


def find_existing_entry(
    settings: CoserSettings,
    *,
    work_id: int | None = None,
    coser_id: int | None = None,
    title: str = "",
    source_url: str = "",
    job_id: str = "",
) -> dict[str, Any] | None:
    """本地任务层：按作品编号 / Coser+标题 / 来源帖 找已发布记录。"""
    needle_source = str(source_url or "").strip()
    needle_job = str(job_id or "").strip()
    with _lock:
        items = list(_load(settings)["items"])
    if work_id:
        for item in items:
            if int(item.get("id") or 0) == int(work_id):
                return item
    if needle_job:
        for item in items:
            if str(item.get("job_id") or "") == needle_job and item.get("id"):
                return item
    if needle_source:
        for item in items:
            if str(item.get("source_url") or "").strip() == needle_source and item.get("id"):
                return item
    if coser_id and title:
        for item in items:
            same_coser = int(item.get("coser_id") or 0) == int(coser_id)
            if same_coser and titles_match(str(item.get("title") or ""), title) and item.get("id"):
                return item
    return None


def list_catalog(
    settings: CoserSettings,
    *,
    page: int = 1,
    limit: int = 12,
    query: str = "",
) -> dict[str, Any]:
    page = max(int(page or 1), 1)
    limit = max(1, min(int(limit or 12), 48))
    needle = str(query or "").strip().casefold()
    with _lock:
        items = list(_load(settings)["items"])
    if needle:
        items = [
            item
            for item in items
            if needle in str(item.get("title") or "").casefold()
            or needle in str(item.get("coser_name") or "").casefold()
            or needle in str(item.get("summary") or "").casefold()
        ]
    total = len(items)
    total_pages = max(1, math.ceil(total / limit)) if total else 0
    start = (page - 1) * limit
    return {
        "data": items[start : start + limit],
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
    }


def delete_entry(settings: CoserSettings, key: str) -> bool:
    with _lock:
        data = _load(settings)
        before = len(data["items"])
        data["items"] = [
            item
            for item in data["items"]
            if str(item.get("id") or "") != str(key)
            and str(item.get("job_id") or "") != str(key)
        ]
        _save(settings, data)
        return len(data["items"]) != before
