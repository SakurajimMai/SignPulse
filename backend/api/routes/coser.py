from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.core.auth import get_current_user
from backend.models.user import User
from backend.services.coser.catalog import delete_entry, list_catalog
from backend.services.coser.config import (
    coser_config_public,
    load_coser_settings,
    save_coser_settings,
)
from backend.services.coser.paths import (
    PathEscapeError,
    coser_dirs,
    relative_to_coser,
    resolve_coser_path,
)
from backend.services.coser.s3 import CoserS3Error
from backend.services.coser.site import CoserSiteClient, CoserSiteError
from backend.services.coser.worker import (
    CoserBusyError,
    get_coser_runner,
    list_jobs,
    load_job,
    new_job,
    probe_status,
    save_job,
)
from backend.services.games.apate import ApateError
from backend.services.games.archive import ArchiveError
from backend.services.games.telegram import GamesTelegramError
from backend.services.manga.hmw.telegram_link import safe_folder_name

router = APIRouter()
UPLOAD_MAX_BYTES = 8 * 1024 * 1024 * 1024


class CoserSettingsRequest(BaseModel):
    site_url: str | None = Field(default=None, max_length=500)
    site_email: str | None = Field(default=None, max_length=200)
    site_password: str | None = None
    s3_endpoint: str | None = Field(default=None, max_length=500)
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str | None = Field(default=None, max_length=200)
    s3_region: str | None = Field(default=None, max_length=80)
    s3_public_url: str | None = Field(default=None, max_length=500)
    s3_prefix: str | None = Field(default=None, max_length=120)
    telegram_account_name: str | None = Field(default=None, max_length=80)
    telegram_source_channels: str | None = Field(default=None, max_length=1000)
    extract_passwords: str | None = Field(default=None, max_length=400)
    pack_password: str | None = Field(default=None, max_length=120)
    split_volume_mb: int | None = Field(default=None, ge=256, le=4096)
    ad_keywords: str | None = Field(default=None, max_length=2000)
    apate_enabled: bool | None = None
    baidu_remote_dir: str | None = Field(default=None, max_length=200)
    pikpak_remote_dir: str | None = Field(default=None, max_length=200)
    terabox_remote_dir: str | None = Field(default=None, max_length=200)
    quark_remote_dir: str | None = Field(default=None, max_length=200)
    cleanup_after_publish: bool | None = None


class PullIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    account: str = ""


class PublishIn(BaseModel):
    job_id: str
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=8000)
    coser_id: int | None = None
    coser_name: str | None = Field(default=None, max_length=120)
    is_r18: bool | None = None
    apate: bool | None = None
    extract_password: str | None = Field(default=None, max_length=120)
    pack_password: str | None = Field(default=None, max_length=120)
    links: dict[str, str] | None = None
    clouds: list[str] | str | None = None


class CoserCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def _settings():
    return load_coser_settings()


def _http(exc: Exception, default_status: int = 400) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    mapping = {
        PathEscapeError: status.HTTP_400_BAD_REQUEST,
        ArchiveError: status.HTTP_400_BAD_REQUEST,
        ApateError: status.HTTP_400_BAD_REQUEST,
        CoserSiteError: status.HTTP_502_BAD_GATEWAY,
        CoserS3Error: status.HTTP_502_BAD_GATEWAY,
        GamesTelegramError: status.HTTP_400_BAD_REQUEST,
        CoserBusyError: status.HTTP_409_CONFLICT,
        FileNotFoundError: status.HTTP_404_NOT_FOUND,
        ValueError: status.HTTP_400_BAD_REQUEST,
    }
    code = mapping.get(type(exc), default_status)
    return HTTPException(status_code=code, detail=str(exc))


@router.get("/coser/settings")
def get_coser_settings(
    reveal: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
):
    del current_user
    return coser_config_public(_settings(), reveal=reveal)


@router.patch("/coser/settings")
def patch_coser_settings(
    request: CoserSettingsRequest,
    current_user: User = Depends(get_current_user),
):
    del current_user
    fields_set = getattr(request, "__fields_set__", set())
    updates = {name: getattr(request, name) for name in fields_set}
    settings = save_coser_settings(updates)
    return {
        "success": True,
        "message": "Coser 发布设置已保存",
        "settings": coser_config_public(settings),
    }


@router.get("/coser/status")
async def coser_status(
    probe: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    job = get_coser_runner().status()
    health = (
        await probe_status(settings)
        if probe
        else {
            "configured": bool(
                settings.site_email
                and settings.site_password
                and settings.site_url
                and settings.s3_endpoint
                and settings.s3_access_key
                and settings.s3_secret_key
                and settings.s3_bucket
            )
        }
    )
    return {**health, **job}


@router.get("/coser/cosers")
async def list_site_cosers(
    q: str = Query(default=""),
    current_user: User = Depends(get_current_user),
):
    del current_user
    try:
        items = await CoserSiteClient().list_cosers(_settings(), query=q)
    except Exception as exc:
        raise _http(exc) from exc
    return {"data": items}


@router.post("/coser/cosers")
async def create_site_coser(
    body: CoserCreateIn, current_user: User = Depends(get_current_user)
):
    del current_user
    try:
        item = await CoserSiteClient().create_coser(_settings(), body.name)
    except Exception as exc:
        raise _http(exc) from exc
    return item


@router.get("/coser/jobs")
def coser_jobs(
    limit: int = Query(default=24, ge=1, le=80),
    current_user: User = Depends(get_current_user),
):
    del current_user
    return {"data": list_jobs(_settings(), limit=limit)}


@router.get("/coser/jobs/{job_id}")
def coser_job(job_id: str, current_user: User = Depends(get_current_user)):
    del current_user
    try:
        return load_job(_settings(), job_id)
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/coser/pull")
async def coser_pull(body: PullIn, current_user: User = Depends(get_current_user)):
    del current_user
    try:
        return await get_coser_runner().start_pull(
            _settings(), url=body.url, account=body.account
        )
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/coser/publish")
async def coser_publish(body: PublishIn, current_user: User = Depends(get_current_user)):
    del current_user
    payload = (
        body.dict(exclude_unset=True)
        if hasattr(body, "dict")
        else body.model_dump(exclude_unset=True)
    )
    job_id = str(payload.pop("job_id") or "")
    try:
        return await get_coser_runner().start_publish(
            _settings(), job_id=job_id, payload=payload
        )
    except Exception as exc:
        raise _http(exc) from exc


@router.post("/coser/jobs/{job_id}/cancel")
async def coser_cancel(job_id: str, current_user: User = Depends(get_current_user)):
    del current_user
    runner = get_coser_runner()
    if str(runner.snapshot.get("id") or "") not in {"", job_id}:
        raise HTTPException(status_code=409, detail="当前运行的不是这个任务")
    return await runner.cancel()


@router.post("/coser/upload")
async def coser_upload(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    del current_user
    settings = _settings()
    filename = safe_folder_name(file.filename or "upload.bin", fallback="upload.bin")
    job = new_job(
        settings, stage="ready", message="本地文件已接收", title=Path(filename).stem
    )
    dest_dir = coser_dirs(settings).inbox / str(job["id"]) / "archives"
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
                "path": relative_to_coser(settings, dest),
                "name": filename,
                "size": size,
            }
        ]
        job["work_dir"] = relative_to_coser(settings, dest_dir.parent)
        save_job(settings, job)
        return job
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise _http(exc) from exc


@router.get("/coser/catalog")
def coser_catalog(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=12, ge=1, le=48),
    q: str = Query(default=""),
    current_user: User = Depends(get_current_user),
):
    del current_user
    return list_catalog(_settings(), page=page, limit=limit, query=q)


@router.delete("/coser/catalog/{key}")
def coser_catalog_delete(key: str, current_user: User = Depends(get_current_user)):
    del current_user
    if not delete_entry(_settings(), key):
        raise HTTPException(status_code=404, detail="目录项不存在")
    return {"success": True}


@router.get("/coser/file")
def coser_file(
    path: str = Query(..., min_length=1, max_length=500),
    current_user: User = Depends(get_current_user),
):
    del current_user
    try:
        resolved = resolve_coser_path(_settings(), path)
    except Exception as exc:
        raise _http(exc) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(resolved)
