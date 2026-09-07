from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from backend.core.auth import get_current_user
from backend.models.user import User
from backend.services.manga.config import load_manga_settings
from backend.services.manga.hmw.archive import ArchiveError
from backend.services.manga.hmw.catalog import get_entry, list_catalog
from backend.services.manga.hmw.lib.api_client import APIError
from backend.services.manga.hmw.lib.config import ConfigError, hmw_configured
from backend.services.manga.hmw.lib.discovery import DiscoveryError
from backend.services.manga.hmw.manage import (
    delete_chapter as manage_delete_chapter,
)
from backend.services.manga.hmw.manage import (
    delete_manga as manage_delete_manga,
)
from backend.services.manga.hmw.manage import (
    fetch_manga_detail,
    sync_catalog,
)
from backend.services.manga.hmw.manage import (
    update_chapter as manage_update_chapter,
)
from backend.services.manga.hmw.manage import (
    update_manga as manage_update_manga,
)
from backend.services.manga.hmw.paths import PathEscapeError, hmw_dirs
from backend.services.manga.hmw.service import (
    apply_chapter_overrides,
    build_manga_payload,
    list_sources,
    make_clients,
    probe_status,
    scan_source,
    unzip_source,
)
from backend.services.manga.hmw.telegram_album import pull_discussion_album
from backend.services.manga.hmw.telegram_zip import (
    TelegramZipError,
    download_archive_doc,
    list_archive_docs,
)
from backend.services.manga.hmw.worker import (
    HmwBusyError,
    get_hmw_runner,
    list_jobs,
    load_job,
    merge_status,
)

logger = logging.getLogger("backend.manga.hmw.api")
router = APIRouter()

UPLOAD_MAX_BYTES = 200 * 1024 * 1024


class ChapterMapIn(BaseModel):
    directory: str
    number: float | None = None
    title: str | None = None
    action: str = "upsert"


class MangaMetaIn(BaseModel):
    language: str = "zh"
    title: str
    slug: str
    author: str = ""
    status: str = "completed"
    description: str = ""
    alternative_title: str = ""
    genre_names: list[str] = Field(default_factory=list)
    genre_ids: list[int] = Field(default_factory=list)
    year: int | None = None
    source_url: str = ""


class PathIn(BaseModel):
    path: str


class UnzipIn(BaseModel):
    name: str
    password: str | None = None


class TelegramDocsQuery(BaseModel):
    account: str = ""
    chat: str
    limit: int = 50


class TelegramDownloadIn(BaseModel):
    account: str = ""
    chat: str
    message_id: int


class TelegramAlbumIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    account: str = ""
    title: str | None = None
    manga_title: str | None = None


class PublishIn(BaseModel):
    source_path: str
    manga: MangaMetaIn
    chapters: list[ChapterMapIn] = Field(default_factory=list)
    cover_path: str | None = None
    task_id: str | None = None


class MangaUpdateIn(BaseModel):
    title: str | None = None
    slug: str | None = None
    author: str | None = None
    status: str | None = None
    description: str | None = None
    alternative_title: str | None = None
    year: int | None = None
    source_url: str | None = None
    expected_version: str | None = None


class ChapterUpdateIn(BaseModel):
    number: float | None = None
    title: str | None = None
    expected_version: str | None = None
    manga_id: int | None = None


class CatalogSyncIn(BaseModel):
    query: str = ""
    language: str = "zh"
    limit: int = 100


def _settings():
    return load_manga_settings()


def _http(exc: Exception, default_status: int = 400) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, APIError) and exc.status_code in {400, 401, 403, 404, 409, 412, 422}:
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    mapping = {
        PathEscapeError: status.HTTP_400_BAD_REQUEST,
        ArchiveError: status.HTTP_400_BAD_REQUEST,
        DiscoveryError: status.HTTP_400_BAD_REQUEST,
        ConfigError: status.HTTP_400_BAD_REQUEST,
        TelegramZipError: status.HTTP_400_BAD_REQUEST,
        HmwBusyError: status.HTTP_409_CONFLICT,
        FileNotFoundError: status.HTTP_404_NOT_FOUND,
        APIError: status.HTTP_502_BAD_GATEWAY,
        ValueError: status.HTTP_400_BAD_REQUEST,
    }
    code = mapping.get(type(exc), default_status)
    return HTTPException(status_code=code, detail=str(exc))


async def _resolve_genres(settings, names: list[str], language: str) -> list[int]:
    cleaned = [str(item).strip() for item in names if str(item).strip()]
    if not cleaned:
        return []
    _cfg, api, _storage = make_clients(settings)
    async with api:
        resolved = await api.resolve_genres(language, cleaned)
    ids: list[int] = []
    for item in resolved or []:
        if isinstance(item, dict) and item.get("id") is not None:
            ids.append(int(item["id"]))
    return ids


def _account_or_default(settings, account: str) -> str:
    name = (account or settings.telegram_account_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请选择已登录的 Telegram 账号")
    return name


@router.get("/manga/hmw/status")
async def hmw_status(
    probe: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    job = get_hmw_runner().status()
    if probe:
        health = await probe_status(settings)
    else:
        health = {
            "configured": hmw_configured(settings),
            "api_ok": None,
            "s3_ok": None,
            "error": None,
        }
    return merge_status(job, health)


@router.get("/manga/hmw/library")
async def hmw_library(
    query: str = Query(default=""),
    language: str = Query(default="zh"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    try:
        _cfg, api, _storage = make_clients(settings)
        async with api:
            return await api.search_manga(query=query, language=language, page=page, page_size=page_size)
    except Exception as exc:
        raise _http(exc) from exc


@router.get("/manga/hmw/library/{manga_id}")
async def hmw_library_detail(
    manga_id: int,
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    try:
        _cfg, api, _storage = make_clients(settings)
        async with api:
            return await api.get_manga(manga_id)
    except Exception as exc:
        raise _http(exc) from exc


@router.put("/manga/hmw/library/{manga_id}")
async def hmw_library_update(
    manga_id: int,
    body: MangaUpdateIn,
    current_user: User = Depends(get_current_user),
):
    del current_user
    payload = body.dict(exclude_unset=True) if hasattr(body, "dict") else body.model_dump(exclude_unset=True)
    try:
        return await manage_update_manga(_settings(), manga_id, payload)
    except Exception as exc:
        raise _http(exc) from exc


@router.delete("/manga/hmw/library/{manga_id}")
async def hmw_library_delete(
    manga_id: int,
    expected_version: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    del current_user
    try:
        return await manage_delete_manga(_settings(), manga_id, expected_version)
    except Exception as exc:
        raise _http(exc) from exc


@router.put("/manga/hmw/chapters/{chapter_id}")
async def hmw_chapter_update(
    chapter_id: int,
    body: ChapterUpdateIn,
    current_user: User = Depends(get_current_user),
):
    del current_user
    payload = body.dict(exclude_unset=True) if hasattr(body, "dict") else body.model_dump(exclude_unset=True)
    try:
        return await manage_update_chapter(_settings(), chapter_id, payload)
    except Exception as exc:
        raise _http(exc) from exc


@router.delete("/manga/hmw/chapters/{chapter_id}")
async def hmw_chapter_delete(
    chapter_id: int,
    expected_version: str | None = Query(default=None),
    manga_id: int | None = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    del current_user
    try:
        return await manage_delete_chapter(
            _settings(),
            chapter_id,
            expected_version=expected_version,
            manga_id=manga_id,
        )
    except Exception as exc:
        raise _http(exc) from exc


@router.get("/manga/hmw/catalog")
def hmw_catalog(
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=12, ge=1, le=48),
    current_user: User = Depends(get_current_user),
):
    del current_user
    return list_catalog(_settings(), q=q, page=page, limit=limit)


@router.post("/manga/hmw/catalog/sync")
async def hmw_catalog_sync(
    body: CatalogSyncIn,
    current_user: User = Depends(get_current_user),
):
    del current_user
    try:
        return await sync_catalog(
            _settings(),
            query=body.query,
            language=body.language,
            limit=body.limit,
        )
    except Exception as exc:
        raise _http(exc) from exc


@router.get("/manga/hmw/catalog/{manga_id}")
async def hmw_catalog_detail(
    manga_id: int,
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    try:
        return await fetch_manga_detail(settings, manga_id)
    except Exception as exc:
        local = get_entry(settings, manga_id)
        if local is None:
            raise _http(exc, status.HTTP_404_NOT_FOUND) from exc
        return {"manga": local, "catalog": local, "offline": True, "error": str(exc)}


@router.get("/manga/hmw/genres")
async def hmw_genres(
    language: str = Query(default="zh"),
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    try:
        _cfg, api, _storage = make_clients(settings)
        async with api:
            return await api.genres(language or "zh")
    except Exception as exc:
        raise _http(exc) from exc


@router.get("/manga/hmw/sources")
def hmw_sources(current_user: User = Depends(get_current_user)):
    del current_user
    return {"data": list_sources(_settings())}


@router.post("/manga/hmw/sources/unzip")
def hmw_unzip(body: UnzipIn, current_user: User = Depends(get_current_user)):
    del current_user
    try:
        return unzip_source(_settings(), body.name, password=body.password)
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/manga/hmw/sources/upload")
async def hmw_upload(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    del current_user
    filename = Path(file.filename or "upload.zip").name
    suffix = Path(filename).suffix.casefold()
    if suffix not in {".zip", ".7z"}:
        raise HTTPException(status_code=400, detail="仅支持 zip / 7z")
    dest = hmw_dirs(_settings()).inbox / filename
    size = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > UPLOAD_MAX_BYTES:
                    handle.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail="浏览器上传上限 200MB，更大的包请放到 data/hmw/inbox",
                    )
                handle.write(chunk)
    finally:
        await file.close()
    return {
        "name": dest.name,
        "path": str(dest.relative_to(hmw_dirs(_settings()).root)),
        "kind": "archive",
        "location": "inbox",
        "size": size,
    }


@router.get("/manga/hmw/telegram/docs")
async def hmw_telegram_docs(
    chat: str = Query(...),
    account: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=80),
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    try:
        items = await list_archive_docs(
            settings,
            account=_account_or_default(settings, account),
            chat=chat,
            limit=limit,
        )
        return {"data": items}
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/manga/hmw/telegram/download")
async def hmw_telegram_download(
    body: TelegramDownloadIn,
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    try:
        return await download_archive_doc(
            settings,
            account=_account_or_default(settings, body.account),
            chat=body.chat,
            message_id=body.message_id,
        )
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/manga/hmw/telegram/album")
async def hmw_telegram_album(
    body: TelegramAlbumIn,
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    try:
        return await pull_discussion_album(
            settings,
            account=_account_or_default(settings, body.account),
            url=body.url.strip(),
            title=(body.title or "").strip() or None,
            manga_title=(body.manga_title or "").strip() or None,
        )
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/manga/hmw/scan")
def hmw_scan(body: PathIn, current_user: User = Depends(get_current_user)):
    del current_user
    try:
        return scan_source(_settings(), body.path)
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/manga/hmw/preflight")
async def hmw_preflight(body: PublishIn, current_user: User = Depends(get_current_user)):
    del current_user
    settings = _settings()
    try:
        scan = scan_source(settings, body.source_path)
        from backend.services.manga.hmw.lib.discovery import discover_source
        from backend.services.manga.hmw.paths import resolve_source_path

        source = discover_source(resolve_source_path(settings, body.source_path))
        cover = resolve_source_path(settings, body.cover_path) if body.cover_path else None
        source = apply_chapter_overrides(
            source,
            [item.dict() if hasattr(item, "dict") else item.model_dump() for item in body.chapters],
            cover_path=cover,
        )
        manga_in = body.manga.dict() if hasattr(body.manga, "dict") else body.manga.model_dump()
        genre_ids = list(manga_in.get("genre_ids") or [])
        if manga_in.get("genre_names") and not genre_ids:
            genre_ids = await _resolve_genres(settings, manga_in["genre_names"], manga_in.get("language") or "zh")
        cfg, api, storage = make_clients(settings)
        manga_payload = build_manga_payload(
            settings,
            manga_in,
            genre_ids=genre_ids,
            cover_url=storage.public_url(storage.cover_key(str(manga_in["slug"]))),
        )
        async with api:
            plan = await api.preflight(
                {
                    "manga": manga_payload,
                    "chapters": [
                        {
                            "number": chapter.number,
                            "title": chapter.title,
                            "action": chapter.action,
                        }
                        for chapter in source.chapters
                    ],
                }
            )
        return {"scan": scan, "preflight": plan, "manga": manga_payload, "genre_ids": genre_ids}
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/manga/hmw/jobs")
async def hmw_start_job(body: PublishIn, current_user: User = Depends(get_current_user)):
    del current_user
    settings = _settings()
    try:
        manga_in = body.manga.dict() if hasattr(body.manga, "dict") else body.manga.model_dump()
        genre_ids = list(manga_in.get("genre_ids") or [])
        if manga_in.get("genre_names") and not genre_ids:
            genre_ids = await _resolve_genres(
                settings, manga_in["genre_names"], manga_in.get("language") or "zh"
            )
        chapters = [
            item.dict() if hasattr(item, "dict") else item.model_dump() for item in body.chapters
        ]
        snapshot = await get_hmw_runner().start(
            settings,
            source_path=body.source_path,
            manga=manga_in,
            chapters=chapters,
            cover_path=body.cover_path,
            task_id=body.task_id,
            genre_ids=genre_ids,
        )
        return {"success": True, "message": "HMW 发布任务已启动", "job": snapshot}
    except Exception as exc:
        raise _http(exc) from exc


@router.get("/manga/hmw/jobs")
def hmw_jobs(current_user: User = Depends(get_current_user)):
    del current_user
    current = get_hmw_runner().status()
    return {"current": current, "data": list_jobs(_settings())}


@router.get("/manga/hmw/jobs/{task_id}")
def hmw_job_detail(task_id: str, current_user: User = Depends(get_current_user)):
    del current_user
    current = get_hmw_runner().status()
    if current.get("task_id") == task_id:
        return current
    try:
        from backend.services.manga.hmw.worker import job_to_dict

        return job_to_dict(load_job(_settings(), task_id))
    except Exception as exc:
        raise _http(exc, status.HTTP_404_NOT_FOUND) from exc


@router.post("/manga/hmw/jobs/{task_id}/cancel")
async def hmw_job_cancel(task_id: str, current_user: User = Depends(get_current_user)):
    del current_user
    current = get_hmw_runner().status()
    if current.get("task_id") != task_id or not current.get("running"):
        raise HTTPException(status_code=409, detail="没有匹配的运行中任务")
    return {"success": True, "job": await get_hmw_runner().cancel()}


@router.post("/manga/hmw/jobs/{task_id}/resume")
async def hmw_job_resume(task_id: str, current_user: User = Depends(get_current_user)):
    del current_user
    settings = _settings()
    try:
        state = load_job(settings, task_id)
        manga = dict(state.manga_payload or {})
        if not manga.get("slug"):
            manga["slug"] = state.manga_slug
        chapters = list(state.chapter_mappings or [])
        snapshot = await get_hmw_runner().start(
            settings,
            source_path=relative_source(settings, state.source_path),
            manga=manga,
            chapters=chapters,
            task_id=task_id,
            genre_ids=list(manga.get("genre_ids") or []),
        )
        return {"success": True, "message": "已继续发布", "job": snapshot}
    except Exception as exc:
        raise _http(exc) from exc


def relative_source(settings, source_path: str) -> str:
    dirs = hmw_dirs(settings)
    path = Path(source_path)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(dirs.root))
        except ValueError:
            return source_path
    return source_path
