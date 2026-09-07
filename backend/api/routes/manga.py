from __future__ import annotations

import json
import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.auth import get_current_user
from backend.models.user import User
from backend.services.manga.config import (
    CHAPTER_MAX_PAGES_CAP,
    load_manga_settings,
    manga_config_public,
    save_manga_settings,
)
from backend.services.manga.db import Chapter, Manga, get_session, list_mangas
from backend.services.manga.publisher import (
    _find_chapter_by_source_key,
    publish_chapter,
)
from backend.services.manga.runtime import get_manga_runtime
from backend.services.manga.schemas import (
    ChapterDetail,
    ChapterSummary,
    MangaDetail,
    MangaListResponse,
    MangaSummary,
    ManualPublishRequest,
    PageOut,
)

logger = logging.getLogger("backend.manga.api")
router = APIRouter()


class SourceBindingRequest(BaseModel):
    channel: str = ""
    discussion: str = ""
    ingest_mode: str = "auto"
    forward_videos: bool = True
    ingest_enabled: bool = True


class MangaSettingsRequest(BaseModel):
    enabled: bool | None = None
    telegram_account_name: str | None = None
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_session_file: str | None = None
    history_backfill_limit: int | None = Field(default=None, ge=0, le=10000)
    tg_source_chats: str | None = None
    source_bindings: list[SourceBindingRequest] | None = None
    tg_allowed_sender_ids: str | None = None
    discussion_only: bool | None = None
    ignore_user_comments: bool | None = None
    chapter_idle_seconds: float | None = Field(default=None, gt=0)
    chapter_reply_idle_seconds: float | None = Field(default=None, gt=0)
    chapter_max_pages: int | None = Field(default=None, ge=1, le=CHAPTER_MAX_PAGES_CAP)
    accept_image_documents: bool | None = None
    cfbed_upload_url: str | None = None
    cfbed_auth_code: str | None = None
    cfbed_api_token: str | None = None
    cfbed_extra_query: str | None = None
    cfbed_public_base: str | None = None
    cfbed_file_field: str | None = None
    cfbed_retry_delay_seconds: float | None = Field(default=None, gt=0)
    site_publish_url: str | None = None
    site_publish_secret: str | None = None
    outbound_enabled: bool | None = None
    outbound_channel: str | None = None
    outbound_preview_count: int | None = Field(default=None, ge=1, le=10)
    outbound_button_text: str | None = Field(default=None, max_length=40)
    outbound_site_base: str | None = None
    outbound_bot_token: str | None = None
    outbound_forward_videos: bool | None = None
    outbound_video_block_keywords: str | None = Field(default=None, max_length=4000)
    outbound_video_allow_keywords: str | None = Field(default=None, max_length=4000)
    outbound_video_min_seconds: int | None = Field(default=None, ge=0, le=86400)
    outbound_video_max_seconds: int | None = Field(default=None, ge=0, le=86400)
    ehentai_enabled: bool | None = None
    ehentai_cookie: str | None = None
    ehentai_exhentai: bool | None = None
    ehentai_search: str | None = Field(default=None, max_length=4000)
    ehentai_cats: str | None = Field(default=None, max_length=16)
    ehentai_max_pages: int | None = Field(default=None, ge=1, le=2000)
    ehentai_search_pages: int | None = Field(default=None, ge=1, le=10)
    ehentai_delay_seconds: float | None = Field(default=None, ge=0)
    ehentai_gallery_delay_seconds: float | None = Field(default=None, ge=0)
    ehentai_poll_seconds: float | None = Field(default=None, ge=60)
    ehentai_translation_url: str | None = Field(default=None, max_length=500)
    ehentai_translation_auto: bool | None = None
    hmw_api_url: str | None = Field(default=None, max_length=500)
    hmw_publisher_token: str | None = None
    hmw_s3_endpoint: str | None = Field(default=None, max_length=500)
    hmw_s3_region: str | None = Field(default=None, max_length=80)
    hmw_s3_bucket: str | None = Field(default=None, max_length=200)
    hmw_s3_access_key: str | None = None
    hmw_s3_secret_key: str | None = None
    hmw_s3_public_url: str | None = Field(default=None, max_length=500)
    hmw_s3_prefix: str | None = Field(default=None, max_length=200)
    hmw_avif_quality: int | None = Field(default=None, ge=1, le=100)
    hmw_convert_workers: int | None = Field(default=None, ge=1, le=4)
    hmw_upload_workers: int | None = Field(default=None, ge=1, le=32)
    hmw_request_timeout: int | None = Field(default=None, ge=1, le=600)
    ai_enabled: bool | None = None
    ai_refine_prompt: str | None = Field(default=None, max_length=8000)


class WorkerActionResponse(BaseModel):
    success: bool
    message: str
    status: dict[str, Any]


def _parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = value.replace("，", ",").replace("、", ",").split(",")
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()][:30]


def _summary(manga: Manga) -> MangaSummary:
    return MangaSummary(
        id=manga.id,
        slug=manga.slug,
        title=manga.title,
        author=manga.author,
        tags=_parse_tags(manga.tags),
        description=manga.description,
        cover_url=manga.cover_url,
        chapter_count=manga.chapter_count,
        page_count=manga.page_count,
        source_chat_title=manga.source_chat_title,
        updated_at=manga.updated_at,
    )


async def _find_manga(session: AsyncSession, key: str) -> Manga | None:
    if key.isdigit():
        result = await session.execute(select(Manga).where(Manga.id == int(key)))
    else:
        result = await session.execute(select(Manga).where(Manga.slug == key))
    return result.scalar_one_or_none()


async def _manga_detail(session: AsyncSession, key: str, published_only: bool = True) -> MangaDetail:
    stmt = select(Manga).where(Manga.id == int(key) if key.isdigit() else Manga.slug == key).options(selectinload(Manga.chapters))
    if published_only:
        stmt = stmt.where(Manga.is_published.is_(True))
    result = await session.execute(stmt)
    manga = result.scalar_one_or_none()
    if manga is None:
        raise HTTPException(status_code=404, detail="Manga not found")
    chapters = [
        ChapterSummary(
            id=chapter.id,
            number=chapter.number,
            title=chapter.title,
            page_count=chapter.page_count,
            created_at=chapter.created_at,
        )
        for chapter in sorted(manga.chapters, key=lambda item: item.number)
        if chapter.is_published or not published_only
    ]
    return MangaDetail(**_summary(manga).dict(), chapters=chapters)


@router.get("/manga/status")
async def manga_status(current_user: User = Depends(get_current_user)):
    del current_user
    runtime = get_manga_runtime()
    if not runtime.initialized:
        await runtime.initialize()
    return runtime.status()


@router.get("/manga/settings")
def get_manga_settings(
    reveal: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
):
    del current_user
    return manga_config_public(load_manga_settings(), reveal=reveal)


@router.patch("/manga/settings")
async def patch_manga_settings(
    request: MangaSettingsRequest,
    current_user: User = Depends(get_current_user),
):
    del current_user
    fields_set = getattr(request, "__fields_set__", set())
    updates = {name: getattr(request, name) for name in fields_set}
    if "source_bindings" in updates and updates["source_bindings"] is not None:
        updates["source_bindings"] = [
            item.dict() if hasattr(item, "dict") else item
            for item in updates["source_bindings"]
        ]
    settings = save_manga_settings(updates)
    runtime = get_manga_runtime()
    await runtime.reload()
    return {"success": True, "message": "漫画设置已保存", "settings": manga_config_public(settings), "status": runtime.status()}


@router.post("/manga/worker/start", response_model=WorkerActionResponse)
async def start_manga_worker(current_user: User = Depends(get_current_user)):
    del current_user
    try:
        result = await get_manga_runtime().start()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkerActionResponse(success=True, message="漫画监听已启动", status=result)


@router.post("/manga/worker/stop", response_model=WorkerActionResponse)
async def stop_manga_worker(current_user: User = Depends(get_current_user)):
    del current_user
    runtime = get_manga_runtime()
    await runtime.stop()
    return WorkerActionResponse(success=True, message="漫画监听已停止", status=runtime.status())


@router.get("/manga/ehentai/status")
async def ehentai_status(current_user: User = Depends(get_current_user)):
    del current_user
    runtime = get_manga_runtime()
    if not runtime.initialized:
        await runtime.initialize()
    return runtime.ehentai_status()


@router.post("/manga/ehentai/start", response_model=WorkerActionResponse)
async def start_ehentai_worker(current_user: User = Depends(get_current_user)):
    del current_user
    try:
        result = await get_manga_runtime().start_ehentai()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkerActionResponse(success=True, message="E-Hentai 监听已启动", status=result)


@router.post("/manga/ehentai/stop", response_model=WorkerActionResponse)
async def stop_ehentai_worker(current_user: User = Depends(get_current_user)):
    del current_user
    runtime = get_manga_runtime()
    result = await runtime.stop_ehentai(persist_disabled=True)
    return WorkerActionResponse(success=True, message="E-Hentai 监听已停止", status=result)


@router.post("/manga/ehentai/run-once", response_model=WorkerActionResponse)
async def run_ehentai_pass(current_user: User = Depends(get_current_user)):
    del current_user
    try:
        result = await get_manga_runtime().request_ehentai_pass()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkerActionResponse(success=True, message="已开始立即搜索", status=result)


@router.post("/manga/ehentai/backfill")
async def backfill_ehentai_worker(current_user: User = Depends(get_current_user)):
    del current_user
    try:
        result = await get_manga_runtime().backfill_ehentai()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report = result.get("backfill") or {}
    updated = int(report.get("updated") or 0)
    return {
        "success": True,
        "message": f"E-Hentai 已补全 {updated} 本",
        "status": result,
    }


@router.get("/manga/ehentai/translations")
async def ehentai_translations(current_user: User = Depends(get_current_user)):
    del current_user
    runtime = get_manga_runtime()
    if not runtime.initialized:
        await runtime.initialize()
    from backend.services.manga.ehentai.catalog import translation_status

    return translation_status(runtime.current_settings().data_dir).as_dict()


@router.post("/manga/ehentai/translations/refresh")
async def refresh_ehentai_translations(current_user: User = Depends(get_current_user)):
    del current_user
    try:
        result = await get_manga_runtime().refresh_ehentai_translations(force=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "message": "EhTagTranslation 词库已更新",
        "status": result,
        "translations": result.get("translations") or {},
    }


@router.get("/manga/catalog", response_model=MangaListResponse)
async def manga_catalog(
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
    q: str | None = Query(None),
    source: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    del current_user
    rows, total = await list_mangas(
        session, page=page, limit=limit, q=q, published_only=False, source=source
    )
    return MangaListResponse(
        data=[_summary(item) for item in rows],
        page=page,
        limit=limit,
        total=total,
        total_pages=max(1, math.ceil(total / limit)) if total else 0,
    )


@router.get("/manga/catalog/{key}", response_model=MangaDetail)
async def manga_catalog_detail(
    key: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    del current_user
    return await _manga_detail(session, key, published_only=False)


@router.get("/mangas", response_model=MangaListResponse)
async def public_mangas(
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
    q: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    rows, total = await list_mangas(session, page=page, limit=limit, q=q, published_only=True)
    return MangaListResponse(
        data=[_summary(item) for item in rows],
        page=page,
        limit=limit,
        total=total,
        total_pages=max(1, math.ceil(total / limit)) if total else 0,
    )


@router.get("/mangas/{key}", response_model=MangaDetail)
async def public_manga_detail(key: str, session: AsyncSession = Depends(get_session)):
    return await _manga_detail(session, key, published_only=True)


@router.get("/mangas/{key}/chapters/{number}", response_model=ChapterDetail)
async def public_chapter(key: str, number: int, session: AsyncSession = Depends(get_session)):
    manga = await _find_manga(session, key)
    if manga is None or not manga.is_published:
        raise HTTPException(status_code=404, detail="Manga not found")
    result = await session.execute(
        select(Chapter).where(
            Chapter.manga_id == manga.id,
            Chapter.number == number,
            Chapter.is_published.is_(True),
        ).options(selectinload(Chapter.pages))
    )
    chapter = result.scalar_one_or_none()
    if chapter is None:
        raise HTTPException(status_code=404, detail="Chapter not found")
    pages = [
        PageOut(index=page.index, image_url=page.image_url, width=page.width, height=page.height)
        for page in sorted(chapter.pages, key=lambda item: item.index)
    ]
    return ChapterDetail(
        id=chapter.id,
        number=chapter.number,
        title=chapter.title,
        page_count=chapter.page_count,
        created_at=chapter.created_at,
        pages=pages,
    )


def _valid_publish_secret(provided: str | None) -> bool:
    expected = load_manga_settings().site_publish_secret
    return bool(expected and provided and provided == expected)


@router.post("/manga/publish")
async def publish_manga(
    body: ManualPublishRequest,
    x_manga_publish_key: str | None = Header(default=None, alias="X-Manga-Publish-Key"),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    token = x_manga_publish_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not _valid_publish_secret(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid manga publish key")
    if not body.image_urls:
        raise HTTPException(status_code=400, detail="image_urls required")
    source_key = body.source_key or f"manual:{body.title}:{len(body.image_urls)}:{body.image_urls[0][-40:]}"
    published = await publish_chapter(
        session,
        title=body.title,
        chapter_title=body.chapter_title,
        source_key=source_key,
        source_chat_id=body.source_chat_id,
        source_chat_title=body.source_chat_title,
        author=body.author,
        tags=body.tags,
        image_urls=body.image_urls,
        message_ids=[],
    )
    if published is None:
        duplicate = await _find_chapter_by_source_key(session, source_key)
        if duplicate is None:
            return {"status": "duplicate", "source_key": source_key}
        manga = await session.get(Manga, duplicate.manga_id)
        return {
            "status": "duplicate",
            "source_key": source_key,
            "manga_id": duplicate.manga_id,
            "chapter_id": duplicate.id,
            "slug": manga.slug if manga else "",
            "number": duplicate.number,
            "page_count": duplicate.page_count,
        }
    return {
        "status": "ok",
        "manga_id": published.manga_id,
        "chapter_id": published.chapter_id,
        "slug": published.slug,
        "number": published.number,
        "page_count": published.page_count,
    }
