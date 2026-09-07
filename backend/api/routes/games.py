from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.core.auth import get_current_user
from backend.models.user import User
from backend.services.games.apate import ApateError
from backend.services.games.archive import ArchiveError
from backend.services.games.automation import get_games_automation
from backend.services.games.catalog import delete_entry, list_catalog
from backend.services.games.cleanup import cleanup_job_files
from backend.services.games.config import (
    games_config_public,
    load_games_settings,
    save_games_settings,
)
from backend.services.games.keepalive import (
    KEEP_INTERVAL_HOURS,
    load_keepalive_state,
    refresh_cloud_sessions,
)
from backend.services.games.paths import (
    PathEscapeError,
    games_dirs,
    relative_to_games,
    resolve_games_path,
)
from backend.services.games.telegram import GamesTelegramError
from backend.services.games.wordpress import (
    DEFAULT_CATEGORY_CHOICES,
    WordPressError,
    list_categories,
)
from backend.services.games.worker import (
    GamesBusyError,
    get_games_runner,
    list_jobs,
    load_job,
    new_job,
    probe_status,
    save_job,
)
from backend.services.manga.hmw.telegram_link import safe_folder_name

logger = logging.getLogger("backend.games.api")
router = APIRouter()

UPLOAD_MAX_BYTES = 8 * 1024 * 1024 * 1024


class GamesSettingsRequest(BaseModel):
    wp_url: str | None = Field(default=None, max_length=500)
    wp_user: str | None = Field(default=None, max_length=120)
    wp_app_password: str | None = None
    wp_default_categories: str | None = Field(default=None, max_length=120)
    wp_default_tags: str | None = Field(default=None, max_length=200)
    wp_pay_enabled: bool | None = None
    wp_pay_modo: str | None = Field(default=None, max_length=20)
    wp_pay_price: float | None = Field(default=None, ge=0, le=9999)
    wp_points_price: float | None = Field(default=None, ge=0, le=999999)
    wp_vip1_price: float | None = Field(default=None, ge=0, le=9999)
    wp_vip2_price: float | None = Field(default=None, ge=0, le=9999)
    wp_vip1_points: float | None = Field(default=None, ge=0, le=999999)
    wp_vip2_points: float | None = Field(default=None, ge=0, le=999999)
    wp_pay_extra_template: str | None = Field(default=None, max_length=4000)
    wp_apate_url: str | None = Field(default=None, max_length=500)
    wp_status: str | None = Field(default=None, max_length=20)
    telegram_account_name: str | None = Field(default=None, max_length=80)
    telegram_source_channels: str | None = Field(default=None, max_length=1000)
    auto_publish_enabled: bool | None = None
    telegram_poll_seconds: int | None = Field(default=None, ge=10, le=3600)
    telegram_backfill_limit: int | None = Field(default=None, ge=0, le=100)
    auto_retry_limit: int | None = Field(default=None, ge=1, le=10)
    extract_passwords: str | None = Field(default=None, max_length=400)
    pack_password: str | None = Field(default=None, max_length=120)
    split_volume_mb: int | None = Field(default=None, ge=256, le=4096)
    ad_keywords: str | None = Field(default=None, max_length=2000)
    apate_enabled: bool | None = None
    apate_bin: str | None = Field(default=None, max_length=400)
    baidu_enabled: bool | None = None
    baidu_cookie: str | None = None
    baidu_remote_dir: str | None = Field(default=None, max_length=200)
    pikpak_username: str | None = Field(default=None, max_length=200)
    pikpak_password: str | None = None
    pikpak_refresh_token: str | None = None
    pikpak_folder_id: str | None = Field(default=None, max_length=80)
    pikpak_remote_dir: str | None = Field(default=None, max_length=200)
    terabox_cookie: str | None = None
    terabox_remote_dir: str | None = Field(default=None, max_length=200)
    quark_cookie: str | None = None
    quark_folder_id: str | None = Field(default=None, max_length=80)
    quark_remote_dir: str | None = Field(default=None, max_length=200)
    openlist_url: str | None = Field(default=None, max_length=500)
    openlist_token: str | None = None
    openlist_baidu_path: str | None = Field(default=None, max_length=200)
    openlist_pikpak_path: str | None = Field(default=None, max_length=200)
    openlist_terabox_path: str | None = Field(default=None, max_length=200)
    openlist_quark_path: str | None = Field(default=None, max_length=200)
    cleanup_after_publish: bool | None = None
    ai_enabled: bool | None = None
    ai_refine_prompt: str | None = Field(default=None, max_length=8000)
    ai_skip_non_games: bool | None = None


class PullIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    account: str = ""


class PublishIn(BaseModel):
    job_id: str
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=8000)
    tags: list[str] | str | None = None
    categories: list[int] | str | None = None
    category_ids: list[int] | None = None
    price: float | None = Field(default=None, ge=0, le=9999)
    points_price: float | None = Field(default=None, ge=0, le=999999)
    vip1_price: float | None = Field(default=None, ge=0, le=9999)
    vip2_price: float | None = Field(default=None, ge=0, le=9999)
    vip1_points: float | None = Field(default=None, ge=0, le=999999)
    vip2_points: float | None = Field(default=None, ge=0, le=999999)
    pay_modo: str | None = Field(default=None, max_length=20)
    pay_enabled: bool | None = None
    apate: bool | None = None
    extract_password: str | None = Field(default=None, max_length=120)
    pack_password: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=20)
    links: dict[str, str] | None = None
    clouds: list[str] | str | None = None


def _settings():
    return load_games_settings()


def _http(exc: Exception, default_status: int = 400) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    mapping = {
        PathEscapeError: status.HTTP_400_BAD_REQUEST,
        ArchiveError: status.HTTP_400_BAD_REQUEST,
        ApateError: status.HTTP_400_BAD_REQUEST,
        WordPressError: status.HTTP_502_BAD_GATEWAY,
        GamesTelegramError: status.HTTP_400_BAD_REQUEST,
        GamesBusyError: status.HTTP_409_CONFLICT,
        FileNotFoundError: status.HTTP_404_NOT_FOUND,
        ValueError: status.HTTP_400_BAD_REQUEST,
    }
    code = mapping.get(type(exc), default_status)
    return HTTPException(status_code=code, detail=str(exc))


@router.get("/games/settings")
def get_games_settings(
    reveal: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
):
    del current_user
    return games_config_public(_settings(), reveal=reveal)


@router.patch("/games/settings")
async def patch_games_settings(
    request: GamesSettingsRequest,
    current_user: User = Depends(get_current_user),
):
    del current_user
    fields_set = getattr(request, "__fields_set__", set())
    updates = {name: getattr(request, name) for name in fields_set}
    settings = save_games_settings(updates)
    await get_games_automation().reload()
    return {
        "success": True,
        "message": "游戏发布设置已保存",
        "settings": games_config_public(settings),
    }


@router.get("/games/status")
async def games_status(
    probe: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    job = get_games_runner().status()
    automation = get_games_automation().status(settings)
    health = (
        await probe_status(settings)
        if probe
        else {
            "configured": bool(
                settings.wp_user and settings.wp_app_password and settings.wp_url
            ),
            "keepalive": load_keepalive_state(settings),
        }
    )
    return {**health, **job, "automation": automation}


@router.post("/games/clouds/refresh")
async def games_cloud_refresh(current_user: User = Depends(get_current_user)):
    del current_user
    state = await refresh_cloud_sessions()
    return {
        "success": True,
        "message": f"已检查网盘登录态，之后每 {KEEP_INTERVAL_HOURS} 小时自动续期",
        "keepalive": state,
    }


@router.post("/games/automation/scan")
async def games_automation_scan(current_user: User = Depends(get_current_user)):
    del current_user
    service = get_games_automation()
    await service.start_if_enabled()
    return await service.scan_now()


@router.post("/games/automation/retry")
async def games_automation_retry(current_user: User = Depends(get_current_user)):
    del current_user
    service = get_games_automation()
    result = service.retry_failed()
    await service.start_if_enabled()
    return result


@router.get("/games/categories")
async def games_categories(current_user: User = Depends(get_current_user)):
    del current_user
    settings = _settings()
    try:
        items = await list_categories(settings)
    except Exception:
        items = list(DEFAULT_CATEGORY_CHOICES)
    return {"data": items}


@router.get("/games/jobs")
def games_jobs(
    limit: int = Query(default=24, ge=1, le=80),
    current_user: User = Depends(get_current_user),
):
    del current_user
    return {"data": list_jobs(_settings(), limit=limit)}


@router.get("/games/jobs/{job_id}")
def games_job(job_id: str, current_user: User = Depends(get_current_user)):
    del current_user
    try:
        return load_job(_settings(), job_id)
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/games/pull")
async def games_pull(body: PullIn, current_user: User = Depends(get_current_user)):
    del current_user
    try:
        return await get_games_runner().start_pull(
            _settings(), url=body.url, account=body.account
        )
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/games/publish")
async def games_publish(
    body: PublishIn, current_user: User = Depends(get_current_user)
):
    del current_user
    payload = (
        body.dict(exclude_unset=True)
        if hasattr(body, "dict")
        else body.model_dump(exclude_unset=True)
    )
    job_id = str(payload.pop("job_id") or "")
    try:
        return await get_games_runner().start_publish(
            _settings(), job_id=job_id, payload=payload
        )
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/games/jobs/{job_id}/cancel")
async def games_cancel(job_id: str, current_user: User = Depends(get_current_user)):
    del current_user
    runner = get_games_runner()
    if str(runner.snapshot.get("id") or "") not in {"", job_id}:
        raise HTTPException(status_code=409, detail="当前运行的不是这个任务")
    return await runner.cancel()


@router.post("/games/jobs/{job_id}/cleanup")
def games_cleanup(job_id: str, current_user: User = Depends(get_current_user)):
    del current_user
    settings = _settings()
    try:
        job = load_job(settings, job_id)
        return cleanup_job_files(settings, job)
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/games/upload")
async def games_upload(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    filename = safe_folder_name(file.filename or "upload.bin", fallback="upload.bin")
    job = new_job(
        settings, stage="ready", message="本地文件已接收", title=Path(filename).stem
    )
    dest_dir = games_dirs(settings).inbox / str(job["id"]) / "archives"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    size = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="文件过大")
                handle.write(chunk)
        job["archives"] = [
            {
                "path": relative_to_games(settings, dest),
                "name": filename,
                "size": size,
            }
        ]
        job["work_dir"] = relative_to_games(settings, dest_dir.parent)
        save_job(settings, job)
        return job
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise _http(exc) from exc


@router.get("/games/catalog")
def games_catalog(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=12, ge=1, le=48),
    q: str = Query(default=""),
    current_user: User = Depends(get_current_user),
):
    del current_user
    return list_catalog(_settings(), page=page, limit=limit, query=q)


@router.delete("/games/catalog/{key}")
def games_catalog_delete(key: str, current_user: User = Depends(get_current_user)):
    del current_user
    if not delete_entry(_settings(), key):
        raise HTTPException(status_code=404, detail="目录项不存在")
    return {"success": True}


@router.get("/games/file")
def games_file(
    path: str = Query(..., min_length=1, max_length=500),
    current_user: User = Depends(get_current_user),
):
    del current_user
    try:
        resolved = resolve_games_path(_settings(), path)
    except Exception as exc:
        raise _http(exc) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(resolved)
