from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from backend.services.manga.config import MangaSettings

from .archive import ArchiveError, extract_archive
from .lib.api_client import PublisherAPIClient
from .lib.config import ConfigError, UploaderConfig, hmw_configured
from .lib.discovery import (
    DiscoveryError,
    SourceDiscovery,
    discover_source,
    validate_chapter_numbers,
)
from .lib.models import ChapterCandidate
from .lib.storage import S3Storage
from .paths import hmw_dirs, is_archive, resolve_source_path, resolve_under


def public_site_url(language: str, slug: str) -> str:
    lang = (language or "zh").strip() or "zh"
    return f"https://www.hmw.app/{lang}/manga/{quote(slug, safe='')}"


def relative_to_hmw(settings: MangaSettings, path: Path) -> str:
    root = hmw_dirs(settings).root
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def list_sources(settings: MangaSettings) -> list[dict[str, Any]]:
    dirs = hmw_dirs(settings)
    items: list[dict[str, Any]] = []
    for folder, kind_hint in ((dirs.inbox, "inbox"), (dirs.extracted, "extracted")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
            if path.name.startswith("."):
                continue
            if path.is_dir():
                items.append(
                    {
                        "name": path.name,
                        "path": relative_to_hmw(settings, path),
                        "kind": "directory",
                        "location": kind_hint,
                        "size": None,
                    }
                )
            elif is_archive(path):
                items.append(
                    {
                        "name": path.name,
                        "path": relative_to_hmw(settings, path),
                        "kind": "archive",
                        "location": kind_hint,
                        "size": path.stat().st_size,
                    }
                )
    return items


def unzip_source(settings: MangaSettings, name: str, password: str | None = None) -> dict[str, Any]:
    dirs = hmw_dirs(settings)
    source = resolve_under(dirs.inbox, name)
    if not is_archive(source):
        raise ArchiveError("只能解压 inbox 中的 zip / 7z")
    dest = dirs.extracted / source.stem
    extract_archive(source, dest, password=password or None)
    return {
        "name": dest.name,
        "path": relative_to_hmw(settings, dest),
        "kind": "directory",
        "location": "extracted",
    }


def discovery_payload(settings: MangaSettings, source: SourceDiscovery) -> dict[str, Any]:
    return {
        "root": relative_to_hmw(settings, source.root),
        "cover": relative_to_hmw(settings, source.cover),
        "chapters": [
            {
                "directory": chapter.directory.name,
                "name": chapter.name,
                "number": chapter.number,
                "title": chapter.title,
                "pages": len(chapter.images),
                "action": chapter.action,
            }
            for chapter in source.chapters
        ],
        "pages": sum(len(chapter.images) for chapter in source.chapters),
    }


def scan_source(settings: MangaSettings, user_path: str) -> dict[str, Any]:
    root = resolve_source_path(settings, user_path)
    if not root.is_dir():
        raise DiscoveryError("发布源必须是目录")
    source = discover_source(root)
    return discovery_payload(settings, source)


def apply_chapter_overrides(
    source: SourceDiscovery,
    chapters: list[dict[str, Any]] | None,
    cover_path: str | Path | None = None,
) -> SourceDiscovery:
    if cover_path:
        cover = Path(cover_path)
        if cover.is_file():
            source.cover = cover
    if not chapters:
        validate_chapter_numbers(chapter.number for chapter in source.chapters)
        return source
    by_name = {chapter.directory.name: chapter for chapter in source.chapters}
    mapped: list[ChapterCandidate] = []
    seen: set[str] = set()
    for item in chapters:
        name = Path(str(item.get("directory") or item.get("name") or "")).name.strip()
        if not name or name in seen:
            continue
        base = by_name.get(name)
        if base is None:
            raise DiscoveryError(f"扫描结果中没有章节目录：{name}")
        action = str(item.get("action") or "upsert").strip() or "upsert"
        if action not in {"upsert", "skip"}:
            action = "upsert"
        number = item.get("number", base.number)
        try:
            number_value = float(number)
        except (TypeError, ValueError) as exc:
            raise DiscoveryError(f"章节号无效：{name}") from exc
        title = str(item.get("title") or base.title).strip() or base.title
        mapped.append(
            ChapterCandidate(
                directory=base.directory,
                name=base.name,
                number=number_value,
                title=title,
                images=base.images,
                action=action,  # type: ignore[arg-type]
            )
        )
        seen.add(name)
    if not mapped:
        raise DiscoveryError("没有可发布的章节")
    active = [chapter for chapter in mapped if chapter.action != "skip"]
    validate_chapter_numbers(chapter.number for chapter in active)
    source.chapters = mapped
    return source


def build_manga_payload(
    settings: MangaSettings,
    manga: dict[str, Any],
    *,
    genre_ids: list[int] | None = None,
    cover_url: str = "",
) -> dict[str, Any]:
    slug = str(manga.get("slug") or manga.get("title") or "").strip()
    title = str(manga.get("title") or slug).strip()
    if not slug or not title:
        raise ConfigError("标题和 slug 不能为空")
    language = str(manga.get("language") or "zh").strip() or "zh"
    status = str(manga.get("status") or "completed").strip() or "completed"
    if status not in {"ongoing", "completed", "hiatus"}:
        status = "completed"
    year = manga.get("year")
    try:
        year_value = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year_value = None
    return {
        "language": language,
        "title": title,
        "alternative_title": str(manga.get("alternative_title") or ""),
        "slug": slug,
        "description": str(manga.get("description") or ""),
        "cover_url": cover_url,
        "author": str(manga.get("author") or ""),
        "status": status,
        "source_url": str(manga.get("source_url") or ""),
        "genre_ids": list(genre_ids or manga.get("genre_ids") or []),
        "year": year_value,
    }


def make_clients(settings: MangaSettings):
    cfg = UploaderConfig.from_manga_settings(settings)
    api = PublisherAPIClient(cfg.api_url, cfg.publisher_token, timeout=cfg.request_timeout)
    storage = S3Storage(cfg)
    return cfg, api, storage


async def probe_status(settings: MangaSettings) -> dict[str, Any]:
    configured = hmw_configured(settings)
    payload: dict[str, Any] = {
        "configured": configured,
        "api_ok": False,
        "s3_ok": False,
        "error": None,
    }
    if not configured:
        payload["error"] = "未配置 Publisher 令牌或对象存储"
        return payload
    cfg, api, storage = make_clients(settings)
    try:
        async with api:
            health = await api.health()
            payload["api_ok"] = bool(health)
            if isinstance(health, dict) and health.get("status") not in (None, "ok", "healthy"):
                payload["api_ok"] = health.get("status") == "ok"
    except Exception as exc:
        payload["error"] = str(exc)
        return payload
    try:
        payload["s3_ok"] = bool(storage.check_connection())
        if not payload["s3_ok"] and not payload["error"]:
            payload["error"] = "对象存储无法访问"
    except Exception as exc:
        payload["error"] = str(exc)
    return payload
