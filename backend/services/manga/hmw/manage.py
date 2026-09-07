"""HMW 漫画管理：改资料 / 改章节目录 / 删除，并顺带清 CDN、更新本机目录。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.services.manga.config import MangaSettings

from .catalog import remove_entry, upsert_from_detail
from .lib.api_client import APIError
from .service import make_clients

logger = logging.getLogger("backend.manga.hmw.manage")

MANGA_EDIT_KEYS = (
    "language",
    "title",
    "alternative_title",
    "slug",
    "description",
    "cover_url",
    "author",
    "status",
    "source_url",
    "genre_ids",
    "year",
)


async def cleanup_old_urls(storage, urls: list[Any] | None) -> list[str]:
    keys: list[str] = []
    for url in urls or []:
        text = str(url or "").strip()
        if not text:
            continue
        key = storage.key_from_public_url(text)
        if key:
            keys.append(key)
    if not keys:
        return []
    return await asyncio.to_thread(storage.delete_keys, keys)


def _detail_version(detail: dict[str, Any]) -> str:
    return str(detail.get("version") or (detail.get("manga") or {}).get("version") or "")


async def fetch_manga_detail(
    settings: MangaSettings,
    manga_id: int,
    *,
    extras: dict[str, Any] | None = None,
    published: bool = False,
) -> dict[str, Any]:
    _cfg, api, _storage = make_clients(settings)
    async with api:
        detail = await api.get_manga(int(manga_id))
    if not isinstance(detail, dict):
        raise APIError("主站返回的漫画详情无效")
    entry = upsert_from_detail(settings, detail, extras=extras, published=published)
    return {"manga": detail, "catalog": entry}


async def update_manga(settings: MangaSettings, manga_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    _cfg, api, storage = make_clients(settings)
    async with api:
        detail = await api.get_manga(int(manga_id))
        expected = str(patch.get("expected_version") or _detail_version(detail) or "")
        manga = {key: detail.get(key) for key in MANGA_EDIT_KEYS}
        nested = detail.get("manga") if isinstance(detail.get("manga"), dict) else None
        if nested:
            for key in MANGA_EDIT_KEYS:
                if manga.get(key) in (None, "") and nested.get(key) not in (None, ""):
                    manga[key] = nested.get(key)
        for key in MANGA_EDIT_KEYS:
            if key in patch and patch[key] is not None:
                manga[key] = patch[key]
        title = str(manga.get("title") or "").strip()
        slug = str(manga.get("slug") or "").strip()
        if not title or not slug:
            raise ValueError("标题和 slug 不能为空")
        manga["title"] = title
        manga["slug"] = slug
        manga["author"] = str(manga.get("author") or "").strip()
        manga["description"] = str(manga.get("description") or "").strip()
        manga["alternative_title"] = str(manga.get("alternative_title") or "").strip()
        manga["status"] = str(manga.get("status") or "completed").strip() or "completed"
        result = await api.update_manga(
            int(manga_id),
            {"manga": manga, "expected_version": expected},
        )
        old_urls = (result or {}).get("old_urls") or []
        cleanup_errors = await cleanup_old_urls(storage, old_urls)
        refreshed = await api.get_manga(int(manga_id))
    entry = upsert_from_detail(settings, refreshed)
    return {
        "manga": refreshed,
        "catalog": entry,
        "old_urls": old_urls,
        "cleanup_errors": cleanup_errors,
    }


async def delete_manga(
    settings: MangaSettings,
    manga_id: int,
    expected_version: str | None = None,
) -> dict[str, Any]:
    _cfg, api, storage = make_clients(settings)
    async with api:
        version = (expected_version or "").strip()
        if not version:
            detail = await api.get_manga(int(manga_id))
            version = _detail_version(detail)
        result = await api.delete_manga(int(manga_id), version)
        old_urls = (result or {}).get("old_urls") or []
        cleanup_errors = await cleanup_old_urls(storage, old_urls)
    remove_entry(settings, int(manga_id))
    return {"result": result or {}, "old_urls": old_urls, "cleanup_errors": cleanup_errors}


async def update_chapter(
    settings: MangaSettings,
    chapter_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    manga_id = payload.get("manga_id")
    number = payload.get("number")
    title = payload.get("title")
    expected = str(payload.get("expected_version") or "").strip()
    body: dict[str, Any] = {}
    if number is not None and number != "":
        body["number"] = float(number)
    if title is not None:
        body["title"] = str(title).strip()
    if not body:
        raise ValueError("章节号和章节名至少改一项")
    _cfg, api, storage = make_clients(settings)
    async with api:
        if not expected and manga_id:
            detail = await api.get_manga(int(manga_id))
            expected = _detail_version(detail)
        if not expected:
            raise ValueError("缺少 expected_version")
        body["expected_version"] = expected
        result = await api.update_chapter(int(chapter_id), body)
        old_urls = (result or {}).get("old_urls") or []
        cleanup_errors = await cleanup_old_urls(storage, old_urls)
        refreshed = None
        if manga_id:
            refreshed = await api.get_manga(int(manga_id))
    entry = upsert_from_detail(settings, refreshed) if isinstance(refreshed, dict) else None
    return {
        "result": result or {},
        "manga": refreshed,
        "catalog": entry,
        "old_urls": old_urls,
        "cleanup_errors": cleanup_errors,
    }


async def delete_chapter(
    settings: MangaSettings,
    chapter_id: int,
    *,
    expected_version: str | None = None,
    manga_id: int | None = None,
) -> dict[str, Any]:
    _cfg, api, storage = make_clients(settings)
    async with api:
        version = (expected_version or "").strip()
        if not version and manga_id:
            detail = await api.get_manga(int(manga_id))
            version = _detail_version(detail)
        if not version:
            raise ValueError("缺少 expected_version")
        result = await api.delete_chapter(int(chapter_id), version)
        old_urls = (result or {}).get("old_urls") or []
        cleanup_errors = await cleanup_old_urls(storage, old_urls)
        refreshed = None
        if manga_id:
            refreshed = await api.get_manga(int(manga_id))
    if isinstance(refreshed, dict):
        entry = upsert_from_detail(settings, refreshed)
    else:
        entry = None
    return {
        "result": result or {},
        "manga": refreshed,
        "catalog": entry,
        "old_urls": old_urls,
        "cleanup_errors": cleanup_errors,
    }


async def sync_catalog(
    settings: MangaSettings,
    *,
    query: str = "",
    language: str = "zh",
    limit: int = 100,
) -> dict[str, Any]:
    imported: list[dict[str, Any]] = []
    page = 1
    total = 0
    cap = max(1, min(int(limit or 100), 200))
    _cfg, api, _storage = make_clients(settings)
    async with api:
        while len(imported) < cap:
            result = await api.search_manga(
                query=(query or "").strip(),
                language=(language or "").strip() or None,
                page=page,
                page_size=min(50, cap),
            )
            result = result or {}
            items = result.get("items") or result.get("results") or []
            try:
                total = int(result.get("total") or total)
            except (TypeError, ValueError):
                total = total
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                manga_id = item.get("id")
                if manga_id is None:
                    continue
                try:
                    detail = await api.get_manga(int(manga_id))
                except APIError:
                    logger.warning("同步 HMW 漫画 %s 详情失败", manga_id, exc_info=True)
                    detail = item
                imported.append(upsert_from_detail(settings, detail))
                if len(imported) >= cap:
                    break
            page += 1
            if page > 20:
                break
    return {"imported": len(imported), "total": total, "data": imported}
