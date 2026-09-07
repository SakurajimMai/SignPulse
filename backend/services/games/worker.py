from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.services.alerts import schedule_alert
from backend.services.manga.hmw.telegram_link import safe_folder_name
from backend.utils.time import utc_now_iso_z

from .apate import ApateError, apate_available, disguise_to_mp4, probe_apate
from .archive import extract_tree, find_7z_bin, pack_7z_parts, strip_ads
from .catalog import upsert_entry
from .cleanup import cleanup_job_files, cleanup_source_archives
from .clouds import (
    CLOUD_TARGETS,
    PreparedUpload,
    baidu_title_folder,
    cloud_status,
    cloud_upload_phases,
    normalize_upload_targets,
    upload_targets,
)
from .config import GamesSettings, load_games_settings, optional_float
from .keepalive import load_keepalive_state
from .parser import clean_game_summary
from .paths import games_dirs, relative_to_games, resolve_games_path
from .telegram import pull_telegram_post, resolve_account
from .wordpress import (
    build_post_html,
    create_post,
    extract_image_urls,
    get_post,
    normalize_pay_modo,
    parse_category_input,
    parse_tag_input,
    probe_wordpress,
    resolve_tag_ids,
    upload_media,
)

logger = logging.getLogger("backend.games.worker")


def _job_label(job: dict[str, Any]) -> str:
    return str(job.get("title") or job.get("id") or "游戏")


def _job_alert_fingerprint(job: dict[str, Any]) -> str:
    for key in ("source_key", "source_url", "id"):
        value = str(job.get(key) or "").strip()
        if value:
            return value
    return _job_label(job)


def _alert_pipeline_fail(
    job: dict[str, Any], error: str, *, stage: str | None = None
) -> None:
    failed_stage = str(stage or job.get("stage") or "").strip()
    if failed_stage == "pulling":
        rule_id = "games_source_pull_fail"
        label = "游戏来源帖子拉取失败"
    elif failed_stage in {"extracting", "packing", "disguise"}:
        rule_id = "games_archive_fail"
        label = "游戏文件处理失败"
    elif failed_stage == "publishing":
        rule_id = "games_site_publish_fail"
        label = "游戏 WordPress 发布失败"
    else:
        rule_id = "games_archive_fail"
        label = "游戏发布流程失败"
    schedule_alert(
        rule_id,
        title=f"{label}：{_job_label(job)}",
        detail=f"stage={failed_stage or 'unknown'}\n{error}",
        fingerprint=_job_alert_fingerprint(job),
    )


def _alert_cloud_fail(job: dict[str, Any], error: str) -> None:
    schedule_alert(
        "games_cloud_fail",
        title=f"游戏网盘失败：{_job_label(job)}",
        detail=error,
        fingerprint=_job_alert_fingerprint(job),
    )


def _alert_cloud_errors(job: dict[str, Any]) -> None:
    errors = job.get("cloud_errors") if isinstance(job.get("cloud_errors"), dict) else {}
    for provider, error in errors.items():
        schedule_alert(
            "games_cloud_fail",
            title=f"游戏网盘失败：{provider}/{_job_label(job)}",
            detail=str(error),
            fingerprint=f"{provider}:{_job_alert_fingerprint(job)}",
        )


class GamesBusyError(RuntimeError):
    pass


class GamesCloudUploadError(RuntimeError):
    pass


def _job_path(settings: GamesSettings, job_id: str) -> Path:
    return games_dirs(settings).tasks / f"{job_id}.json"


def save_job(settings: GamesSettings, job: dict[str, Any]) -> dict[str, Any]:
    job["updated_at"] = utc_now_iso_z()
    path = _job_path(settings, str(job["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def load_job(settings: GamesSettings, job_id: str) -> dict[str, Any]:
    path = _job_path(settings, job_id)
    if not path.is_file():
        raise FileNotFoundError("任务不存在")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise FileNotFoundError("任务损坏")
    return raw


def list_jobs(settings: GamesSettings, limit: int = 40) -> list[dict[str, Any]]:
    directory = games_dirs(settings).tasks
    paths = sorted(
        directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
    )
    items: list[dict[str, Any]] = []
    for path in paths[: max(1, min(limit, 80))]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def new_job(settings: GamesSettings, **extra: Any) -> dict[str, Any]:
    job_id = uuid4().hex
    job = {
        "id": job_id,
        "running": False,
        "stage": "idle",
        "current": 0,
        "total": 0,
        "message": "",
        "error": "",
        "title": "",
        "summary": "",
        "tags": [],
        "category_ids": settings.category_ids,
        "price": settings.wp_pay_price,
        "points_price": settings.wp_points_price,
        "vip1_price": settings.wp_vip1_price,
        "vip2_price": settings.wp_vip2_price,
        "vip1_points": settings.wp_vip1_points,
        "vip2_points": settings.wp_vip2_points,
        "pay_modo": settings.wp_pay_modo,
        "pay_enabled": settings.wp_pay_enabled,
        "apate": settings.apate_enabled,
        "source_url": "",
        "images": [],
        "archives": [],
        "archive_members": [],
        "links": {},
        "share_pwd": "",
        "cloud_errors": {},
        "public_url": None,
        "wp_id": None,
        "cover_url": None,
        "created_at": utc_now_iso_z(),
        "updated_at": utc_now_iso_z(),
    }
    job.update(extra)
    return save_job(settings, job)


def _apply_cloud_result(
    job: dict[str, Any],
    uploaded: dict[str, Any],
    *,
    manual_links: dict[str, str] | None = None,
    apate_on: bool = False,
) -> tuple[dict[str, str], str]:
    links = dict(job.get("links") or {})
    links.update(uploaded.get("links") or {})
    uploaded_links = uploaded.get("links") or {}
    errors = {
        key: value
        for key, value in (job.get("cloud_errors") or {}).items()
        if key not in uploaded_links
    }
    errors.update(uploaded.get("errors") or {})
    job["links"] = links
    job["cloud_errors"] = errors
    if uploaded.get("share_pwd"):
        job["share_pwd"] = uploaded.get("share_pwd")
    folders = dict(job.get("remote_folders") or {})
    folders.update(uploaded.get("folders") or {})
    job["remote_folders"] = folders
    manual_links = manual_links or {}
    job["apate_used"] = bool(
        apate_on
        and any(
            links.get(key) and not manual_links.get(key)
            for key in ("baidu", "quark")
        )
    )
    return links, str(job.get("share_pwd") or "")


def _existing_packed_parts(
    settings: GamesSettings, job: dict[str, Any]
) -> list[Path]:
    parts: list[Path] = []
    for item in job.get("packed_parts") or []:
        raw = item.get("path") if isinstance(item, dict) else item
        if not raw:
            continue
        path = resolve_games_path(settings, str(raw))
        if path.is_file():
            parts.append(path)
    return parts


def _missing_configured_clouds(
    settings: GamesSettings, job: dict[str, Any], payload: dict[str, Any]
) -> list[str]:
    raw_clouds = payload.get("clouds", job.get("clouds"))
    if isinstance(raw_clouds, str):
        raw_clouds = [part.strip() for part in raw_clouds.split(",") if part.strip()]
    selected = (
        normalize_upload_targets(raw_clouds) if raw_clouds is not None else None
    )
    targets = list(CLOUD_TARGETS if selected is None else selected)
    links = job.get("links") if isinstance(job.get("links"), dict) else {}
    statuses = cloud_status(settings)
    missing: list[str] = []
    for target in targets:
        if str(links.get(target) or "").strip():
            continue
        if (statuses.get(target) or {}).get("configured"):
            missing.append(target)
    return missing


def finalize_published_job(
    settings: GamesSettings, job: dict[str, Any]
) -> dict[str, Any]:
    if not job.get("wp_id"):
        raise RuntimeError("WordPress 文章尚未创建")
    job["summary"] = clean_game_summary(job.get("summary") or "")
    upsert_entry(
        settings,
        {
            "id": job.get("wp_id"),
            "title": job.get("title"),
            "summary": job.get("summary"),
            "cover_url": job.get("cover_url"),
            "public_url": job.get("public_url"),
            "tags": job.get("tags") or [],
            "categories": job.get("category_ids") or [],
            "price": job.get("price"),
            "points_price": job.get("points_price"),
            "pay_modo": job.get("pay_modo"),
            "links": job.get("links") or {},
            "source_url": job.get("source_url"),
            "source_key": job.get("source_key"),
            "job_id": job.get("id"),
            "apate": bool(job.get("apate_used") or job.get("packed_mp4")),
        },
    )
    errors = job.get("cloud_errors") if isinstance(job.get("cloud_errors"), dict) else {}
    message = "已发布到网站"
    if errors:
        failed = "、".join(str(key) for key in errors)
        message = f"已发布到网站，但 {failed} 失败，已保留本地文件便于补传"
    job.update(
        {
            "stage": "done",
            "running": False,
            "message": message,
            "error": "",
        }
    )
    return save_job(settings, job)


def probe_tools(settings: GamesSettings) -> dict[str, Any]:
    seven = find_7z_bin()
    apate = probe_apate(settings)
    return {
        "sevenzip": {"ok": bool(seven), "bin": seven},
        "apate_tool": apate,
        "clouds": cloud_status(settings),
        "keepalive": load_keepalive_state(settings),
    }


async def probe_status(settings: GamesSettings) -> dict[str, Any]:
    wp = await probe_wordpress(settings)
    tools = probe_tools(settings)
    account = ""
    try:
        account = resolve_account(settings, "")
    except Exception:
        account = ""
    return {
        "configured": bool(
            settings.wp_user and settings.wp_app_password and settings.wp_url
        ),
        "wp": wp,
        "telegram_account": account,
        **tools,
        "categories": settings.category_ids,
    }


class GamesJobRunner:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self.snapshot: dict[str, Any] = {"running": False, "stage": "idle"}

    def status(self) -> dict[str, Any]:
        task = self._task
        if task is not None and task.done():
            self.snapshot["running"] = False
            self._task = None
        return dict(self.snapshot)

    def _touch(self, settings: GamesSettings, job: dict[str, Any]) -> dict[str, Any]:
        saved = save_job(settings, job)
        self.snapshot = dict(saved)
        return saved

    async def _progress(
        self,
        settings: GamesSettings,
        job: dict[str, Any],
        stage: str,
        current: int,
        total: int,
        message: str,
    ) -> None:
        if self._cancel.is_set():
            raise asyncio.CancelledError()
        job["stage"] = stage
        job["current"] = current
        job["total"] = total
        job["message"] = message
        job["running"] = True
        self._touch(settings, job)

    async def start_pull(
        self, settings: GamesSettings, *, url: str, account: str = ""
    ) -> dict[str, Any]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise GamesBusyError("已有游戏任务在运行")
            job = new_job(
                settings,
                stage="queued",
                running=True,
                message="准备拉取 Telegram",
                source_url=url,
            )
            self._cancel = asyncio.Event()
            self.snapshot = dict(job)
            self._task = asyncio.create_task(
                self._run_pull(settings, job, url=url, account=account),
                name=f"games-pull-{job['id'][:8]}",
            )
            return self.status()

    async def start_publish(
        self, settings: GamesSettings, *, job_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise GamesBusyError("已有游戏任务在运行")
            job = load_job(settings, job_id)
            if "summary" in payload:
                job["summary"] = clean_game_summary(payload.get("summary") or "")
            else:
                job["summary"] = clean_game_summary(job.get("summary") or "")
            if payload.get("title"):
                job["title"] = str(payload.get("title") or "").strip() or job.get("title")
            if job.get("wp_id"):
                missing = _missing_configured_clouds(settings, job, payload)
                packed = _existing_packed_parts(settings, job)
                if missing and packed:
                    job["running"] = True
                    job["stage"] = "queued"
                    job["error"] = ""
                    job["message"] = "正在补传失败的网盘"
                    self._cancel = asyncio.Event()
                    self._touch(settings, job)
                    self._task = asyncio.create_task(
                        self._run_retry_clouds(
                            settings, job, payload, missing, packed
                        ),
                        name=f"games-retry-{job_id[:8]}",
                    )
                    return self.status()
                finalized = await self._refresh_published_job(
                    settings, job, payload
                )
                self.snapshot = dict(finalized)
                return self.status()
            job.update(
                {key: value for key, value in payload.items() if value is not None}
            )
            job["running"] = True
            job["stage"] = "queued"
            job["error"] = ""
            job["message"] = "准备解压打包"
            self._cancel = asyncio.Event()
            self._touch(settings, job)
            self._task = asyncio.create_task(
                self._run_publish(settings, job, payload),
                name=f"games-publish-{job_id[:8]}",
            )
            return self.status()

    async def start_auto_publish(
        self,
        settings: GamesSettings,
        *,
        url: str,
        source_key: str,
        account: str = "",
    ) -> dict[str, Any]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise GamesBusyError("已有游戏任务在运行")
            job = new_job(
                settings,
                stage="queued",
                running=True,
                message="自动发布：准备拉取 Telegram",
                source_url=url,
                source_key=source_key,
                automatic=True,
            )
            self._cancel = asyncio.Event()
            self.snapshot = dict(job)
            self._task = asyncio.create_task(
                self._run_auto_publish(settings, job, url=url, account=account),
                name=f"games-auto-{job['id'][:8]}",
            )
            return self.status()

    async def wait_for_job(self, job_id: str) -> dict[str, Any]:
        task = self._task
        if task is not None and str(self.snapshot.get("id") or "") == str(job_id):
            await asyncio.shield(task)
        return load_job(load_games_settings(), str(job_id))

    async def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("取消游戏任务时出错")
        self.snapshot["running"] = False
        if self.snapshot.get("stage") not in {"done", "cancelled"}:
            self.snapshot["stage"] = "cancelled"
            self.snapshot["error"] = self.snapshot.get("error") or "任务已取消"
            self.snapshot["message"] = "任务已取消"
        settings = load_games_settings()
        job_id = str(self.snapshot.get("id") or "")
        if job_id:
            try:
                job = load_job(settings, job_id)
                job.update(self.snapshot)
                self._touch(settings, job)
            except Exception:
                pass
        return self.status()

    async def _run_pull(
        self, settings: GamesSettings, job: dict[str, Any], *, url: str, account: str
    ) -> None:
        try:

            async def progress(
                stage: str, current: int, total: int, message: str
            ) -> None:
                await self._progress(settings, job, stage, current, total, message)

            await self._progress(settings, job, "pulling", 0, 1, "正在读取频道帖")
            result = await pull_telegram_post(
                settings,
                url,
                account=account,
                job_id=str(job["id"]),
                progress=progress,
            )
            job.update(result)
            job["stage"] = "ready"
            job["running"] = False
            job["message"] = (
                f"已拉取 {len(job.get('images') or [])} 张图，{len(job.get('archives') or [])} 个文件"
            )
            job["error"] = ""
            if not job.get("category_ids"):
                job["category_ids"] = settings.category_ids
            if not job.get("tags"):
                job["tags"] = settings.tag_names
            self._touch(settings, job)
        except asyncio.CancelledError:
            job["running"] = False
            job["stage"] = "cancelled"
            job["error"] = "任务已取消"
            job["message"] = "任务已取消"
            self._touch(settings, job)
            raise
        except Exception as exc:
            logger.exception("拉取游戏帖失败")
            job["running"] = False
            job["stage"] = "failed"
            job["error"] = str(exc)
            job["message"] = str(exc)
            self._touch(settings, job)
            _alert_pipeline_fail(job, str(exc), stage="pulling")
        finally:
            self.snapshot["running"] = False

    async def _run_publish(
        self, settings: GamesSettings, job: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        try:
            await self._publish(settings, job, payload)
        except asyncio.CancelledError:
            job["running"] = False
            job["stage"] = "cancelled"
            job["error"] = "任务已取消"
            job["message"] = "任务已取消"
            self._touch(settings, job)
            raise
        except GamesCloudUploadError as exc:
            logger.exception("游戏网盘上传失败")
            job["running"] = False
            job["stage"] = "failed"
            job["error"] = str(exc)
            job["message"] = str(exc)
            self._touch(settings, job)
            _alert_cloud_fail(job, str(exc))
        except Exception as exc:
            logger.exception("游戏发布失败")
            failed_stage = str(job.get("stage") or "")
            job["running"] = False
            job["stage"] = "failed"
            job["error"] = str(exc)
            job["message"] = str(exc)
            self._touch(settings, job)
            _alert_pipeline_fail(job, str(exc), stage=failed_stage)
        finally:
            self.snapshot["running"] = False

    async def _run_auto_publish(
        self,
        settings: GamesSettings,
        job: dict[str, Any],
        *,
        url: str,
        account: str,
    ) -> None:
        try:

            async def progress(
                stage: str, current: int, total: int, message: str
            ) -> None:
                await self._progress(settings, job, stage, current, total, message)

            await self._progress(
                settings, job, "pulling", 0, 1, "自动发布：正在读取频道帖"
            )
            result = await pull_telegram_post(
                settings,
                url,
                account=account,
                job_id=str(job["id"]),
                progress=progress,
            )
            job.update(result)
            job["automatic"] = True
            if job.get("ai_skip") and getattr(settings, "ai_skip_non_games", True):
                reason = str(job.get("ai_skip_reason") or "AI 判定这不是可发布的游戏帖")
                job.update(
                    {
                        "stage": "skipped",
                        "running": False,
                        "error": "",
                        "message": reason,
                    }
                )
                self._touch(settings, job)
                cleanup_job_files(settings, job)
                return
            if not job.get("category_ids"):
                job["category_ids"] = settings.category_ids
            if not job.get("tags"):
                job["tags"] = settings.tag_names
            if not job.get("archives"):
                raise RuntimeError("自动发布帖子不含可处理的压缩包")
            job["stage"] = "ready"
            job["running"] = True
            job["message"] = "自动发布：拉取完成，准备解压"
            job["error"] = ""
            self._touch(settings, job)
            await self._publish(settings, job, {})
        except asyncio.CancelledError:
            job["running"] = False
            job["stage"] = "cancelled"
            job["error"] = "自动发布任务已取消"
            job["message"] = "自动发布任务已取消"
            self._touch(settings, job)
            cleanup_job_files(settings, job)
            raise
        except GamesCloudUploadError as exc:
            logger.exception("游戏自动发布网盘上传失败")
            job["running"] = False
            job["stage"] = "failed"
            job["error"] = str(exc)
            job["message"] = str(exc)
            self._touch(settings, job)
            cleanup_job_files(settings, job)
            _alert_cloud_fail(job, str(exc))
        except Exception as exc:
            logger.exception("游戏自动发布失败")
            failed_stage = str(job.get("stage") or "")
            job["running"] = False
            job["stage"] = "failed"
            job["error"] = str(exc)
            job["message"] = str(exc)
            self._touch(settings, job)
            cleanup_job_files(settings, job)
            _alert_pipeline_fail(job, str(exc), stage=failed_stage)
        finally:
            self.snapshot["running"] = False

    def _disguise_prepare(
        self,
        settings: GamesSettings,
        job: dict[str, Any],
        *,
        title: str,
        apate_on: bool,
        dest_dir: Path | None = None,
    ):
        baidu_key = baidu_title_folder(title)
        disguise_dir = (dest_dir or games_dirs(settings).packed) / ".disguise-upload"
        baidu_names: list[str] = list(job.get("baidu_upload_names") or [])
        split_archive = bool(job.get("split_archive")) or len(
            job.get("packed_parts") or []
        ) > 1

        async def prepare_disguise(
            source: Path, index: int, total: int
        ) -> PreparedUpload:
            if not apate_on:
                if split_archive:
                    raise ApateError("百度/夸克分卷必须启用 Apate 伪装后才能上传")
                return PreparedUpload(source, source.name)
            if not apate_available(settings):
                raise ApateError("百度/夸克上传需要 Apate，但找不到 apate 可执行文件")
            remote_name = (
                f"{baidu_key}.{index:03d}.mp4" if split_archive else f"{baidu_key}.mp4"
            )
            dest = disguise_dir / remote_name
            if dest.is_file():
                if remote_name not in baidu_names:
                    baidu_names.append(remote_name)
                return PreparedUpload(dest, remote_name, temporary=False)
            dest.parent.mkdir(parents=True, exist_ok=True)
            await self._progress(
                settings,
                job,
                "disguise",
                index - 1,
                total,
                f"正在伪装网盘文件 {index}/{total}",
            )
            try:
                await asyncio.to_thread(disguise_to_mp4, settings, source, dest)
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            if remote_name not in baidu_names:
                baidu_names.append(remote_name)
            return PreparedUpload(dest, remote_name, temporary=False)

        return prepare_disguise, disguise_dir, baidu_names

    async def _refresh_published_job(
        self,
        settings: GamesSettings,
        job: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        summary = clean_game_summary(
            str(payload.get("summary") or job.get("summary") or "")
        )
        title = (
            str(payload.get("title") or job.get("title") or "").strip() or "未命名游戏"
        )
        job["summary"] = summary
        job["title"] = title
        image_urls = [
            str(url).strip()
            for url in (job.get("image_urls") or [])
            if str(url).strip()
        ]
        if not image_urls and job.get("wp_id"):
            existing = await get_post(settings, int(job["wp_id"]))
            content = existing.get("content") or {}
            raw = str(content.get("raw") or content.get("rendered") or "")
            image_urls = extract_image_urls(raw)
        if image_urls:
            job["image_urls"] = image_urls
        pack_password = str(
            payload.get("pack_password")
            or job.get("pack_password")
            or settings.pack_password
            or "sakuramai"
        ).strip()
        share_pwd = str(job.get("share_pwd") or "")
        links = job.get("links") if isinstance(job.get("links"), dict) else {}
        tags = parse_tag_input(payload.get("tags"), job.get("tags") or settings.tag_names)
        categories = parse_category_input(
            payload.get("categories") or payload.get("category_ids"),
            job.get("category_ids") or settings.category_ids,
        )
        pay_enabled = (
            bool(payload["pay_enabled"])
            if payload.get("pay_enabled") is not None
            else bool(job.get("pay_enabled", True))
        )
        content = build_post_html(
            summary=summary,
            image_urls=image_urls,
            title=title,
            pack_password=pack_password,
            links=links,
            apate_used=bool(job.get("apate_used")),
            pay_enabled=pay_enabled,
            pay_in_content=False,
            share_pwd=share_pwd,
        )
        tag_ids = await resolve_tag_ids(settings, tags)
        post = await create_post(
            settings,
            title=title,
            content=content,
            categories=categories,
            tags=tag_ids,
            featured_media=None,
            pay_enabled=pay_enabled,
            price=float(job.get("price") or settings.wp_pay_price or 0),
            points_price=float(
                job.get("points_price") or settings.wp_points_price or 0
            ),
            pay_modo=str(job.get("pay_modo") or settings.wp_pay_modo or "0"),
            links=links,
            pack_password=pack_password,
            apate_used=bool(job.get("apate_used")),
            share_pwd=share_pwd,
            extra_template=settings.wp_pay_extra_template,
            apate_url=settings.wp_apate_url,
            vip1_price=optional_float(job.get("vip1_price")),
            vip2_price=optional_float(job.get("vip2_price")),
            vip1_points=optional_float(job.get("vip1_points")),
            vip2_points=optional_float(job.get("vip2_points")),
            status=str(payload.get("status") or job.get("status") or settings.wp_status or "publish"),
            source_key=str(job.get("source_url") or job.get("source_key") or ""),
        )
        job["wp_id"] = post.get("id") or job.get("wp_id")
        job["public_url"] = str(post.get("link") or job.get("public_url") or "")
        job["tags"] = tags
        job["category_ids"] = categories
        finalized = finalize_published_job(settings, job)
        self.snapshot = dict(finalized)
        return finalized

    async def _run_retry_clouds(
        self,
        settings: GamesSettings,
        job: dict[str, Any],
        payload: dict[str, Any],
        missing: list[str],
        packed_parts: list[Path],
    ) -> None:
        disguise_dir: Path | None = None
        try:
            title = str(job.get("title") or "").strip() or "未命名游戏"
            apate_on = bool(job.get("apate", settings.apate_enabled))
            prepare_disguise, disguise_dir, baidu_names = self._disguise_prepare(
                settings,
                job,
                title=title,
                apate_on=apate_on,
                dest_dir=packed_parts[0].parent,
            )

            async def cloud_progress(
                target: str, current: int, total: int, message: str
            ) -> None:
                await self._progress(settings, job, "uploading", current, total, message)

            await self._progress(
                settings, job, "uploading", 0, len(missing), "开始补传网盘"
            )

            async def persist_upload(partial: dict[str, Any]) -> None:
                _apply_cloud_result(job, partial, apate_on=apate_on)
                job["baidu_upload_names"] = baidu_names
                self._touch(settings, job)

            uploaded = await upload_targets(
                settings,
                title=title,
                archive_parts=packed_parts,
                prepare_baidu=prepare_disguise,
                prepare_disguise=prepare_disguise,
                only=missing,
                progress=cloud_progress,
                share_pwd=str(job.get("share_pwd") or "") or None,
                on_update=persist_upload,
            )
            links = dict(job.get("links") or {})
            links.update(uploaded.get("links") or {})
            errors = {
                key: value
                for key, value in (job.get("cloud_errors") or {}).items()
                if key not in (uploaded.get("links") or {})
            }
            errors.update(uploaded.get("errors") or {})
            job["links"] = links
            job["cloud_errors"] = errors
            job["baidu_upload_names"] = baidu_names
            if uploaded.get("share_pwd") and not job.get("share_pwd"):
                job["share_pwd"] = uploaded.get("share_pwd")
            folders = dict(job.get("remote_folders") or {})
            folders.update(uploaded.get("folders") or {})
            job["remote_folders"] = folders
            await self._refresh_published_job(settings, job, payload)
            _alert_cloud_errors(job)
            if settings.cleanup_after_publish and not job.get("cloud_errors"):
                try:
                    cleaned = cleanup_job_files(settings, job)
                    if cleaned.get("deleted"):
                        job["message"] = "已发布到网站，并清理本地文件"
                        self._touch(settings, job)
                except Exception:
                    logger.exception("补传成功后清理本地游戏文件失败")
        except asyncio.CancelledError:
            job["running"] = False
            job["stage"] = "cancelled"
            job["error"] = "任务已取消"
            job["message"] = "任务已取消"
            self._touch(settings, job)
            raise
        except Exception as exc:
            logger.exception("补传网盘失败")
            job["running"] = False
            job["stage"] = "failed"
            job["error"] = str(exc)
            job["message"] = str(exc)
            self._touch(settings, job)
            _alert_cloud_fail(job, str(exc))
        finally:
            if disguise_dir is not None and disguise_dir.exists():
                await asyncio.to_thread(shutil.rmtree, disguise_dir, True)
            self.snapshot["running"] = False

    async def _publish(
        self, settings: GamesSettings, job: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        title = (
            str(payload.get("title") or job.get("title") or "").strip() or "未命名游戏"
        )
        summary = clean_game_summary(
            str(payload.get("summary") or job.get("summary") or "")
        )
        tags = parse_tag_input(
            payload.get("tags"), job.get("tags") or settings.tag_names
        )
        categories = parse_category_input(
            payload.get("categories") or payload.get("category_ids"),
            job.get("category_ids") or settings.category_ids,
        )
        price = float(
            payload.get("price")
            if payload.get("price") is not None
            else job.get("price") or settings.wp_pay_price
        )
        points_price = float(
            payload.get("points_price")
            if payload.get("points_price") is not None
            else job.get("points_price") or settings.wp_points_price
        )

        def _pick_vip(key: str, fallback: float | None) -> float | None:
            if key in payload:
                return optional_float(payload.get(key))
            if key in job:
                return optional_float(job.get(key))
            return fallback

        vip1_price = _pick_vip("vip1_price", settings.wp_vip1_price)
        vip2_price = _pick_vip("vip2_price", settings.wp_vip2_price)
        vip1_points = _pick_vip("vip1_points", settings.wp_vip1_points)
        vip2_points = _pick_vip("vip2_points", settings.wp_vip2_points)
        pay_modo = normalize_pay_modo(
            str(
                payload.get("pay_modo")
                if payload.get("pay_modo") is not None
                else job.get("pay_modo") or settings.wp_pay_modo
            )
        )
        pay_enabled = (
            bool(payload["pay_enabled"])
            if payload.get("pay_enabled") is not None
            else bool(job.get("pay_enabled", True))
        )
        apate_on = (
            bool(payload["apate"])
            if payload.get("apate") is not None
            else bool(job.get("apate", settings.apate_enabled))
        )
        extract_password = str(
            payload.get("extract_password") or job.get("password") or ""
        ).strip()
        pack_password = str(
            payload.get("pack_password") or settings.pack_password or "sakuramai"
        ).strip()
        existing_links = (
            job.get("links") if isinstance(job.get("links"), dict) else {}
        )
        incoming_links = (
            payload.get("links") if isinstance(payload.get("links"), dict) else {}
        )
        manual_links = {
            str(key): str(value).strip()
            for key, value in {**existing_links, **incoming_links}.items()
            if str(value or "").strip()
        }
        raw_clouds = payload.get("clouds")
        if isinstance(raw_clouds, str):
            raw_clouds = [part.strip() for part in raw_clouds.split(",") if part.strip()]
        selected_clouds = (
            normalize_upload_targets(raw_clouds) if raw_clouds is not None else None
        )
        job.update(
            {
                "title": title,
                "summary": summary,
                "tags": tags,
                "category_ids": categories,
                "price": price,
                "points_price": points_price,
                "vip1_price": vip1_price,
                "vip2_price": vip2_price,
                "vip1_points": vip1_points,
                "vip2_points": vip2_points,
                "pay_modo": pay_modo,
                "pay_enabled": pay_enabled,
                "apate": apate_on,
                "clouds": selected_clouds,
            }
        )

        dirs = games_dirs(settings)
        extract_root = dirs.extracted / str(job["id"])
        packed_parts = _existing_packed_parts(settings, job)
        if packed_parts:
            await self._progress(
                settings, job, "packing", 1, 1, "复用已打包的 7z，跳过解压"
            )
            split_archive = bool(job.get("split_archive")) or (
                len(packed_parts) > 1 or packed_parts[0].name.endswith(".001")
            )
        else:
            archives = list(job.get("archives") or [])
            if not archives:
                raise RuntimeError("没有可解压的游戏文件")
            passwords = [extract_password, *settings.password_list]
            await self._progress(
                settings, job, "extracting", 0, max(len(archives), 1), "正在解压"
            )
            first = True
            for index, item in enumerate(archives, start=1):
                raw = item.get("path") if isinstance(item, dict) else str(item)
                source = resolve_games_path(settings, str(raw))
                target = extract_root if first else extract_root / f"part{index}"
                await self._progress(
                    settings,
                    job,
                    "extracting",
                    index - 1,
                    len(archives),
                    f"解压 {source.name}",
                )
                await asyncio.to_thread(extract_tree, source, target, passwords)
                first = False
                try:
                    source.unlink(missing_ok=True)
                except OSError:
                    logger.warning("解压后未能删除源文件 %s", source)
            removed = await asyncio.to_thread(
                strip_ads, extract_root, settings.ad_keyword_list
            )
            job["extracted_dir"] = relative_to_games(settings, extract_root)
            job["removed_ads"] = removed
            if not any(extract_root.rglob("*")):
                raise RuntimeError("解压后没有可用文件，请检查密码或广告过滤")
            cleaned_sources = cleanup_source_archives(settings, job)
            if cleaned_sources.get("deleted"):
                job["source_archives_cleaned"] = True
                self._touch(settings, job)

            packed_name = f"{safe_folder_name(title, fallback='game')}.7z"
            packed_path = dirs.packed / str(job["id"]) / packed_name
            volume_bytes = int(settings.split_volume_mb) * 1024 * 1024
            await self._progress(settings, job, "packing", 0, 1, "正在打包 7z")
            packed_parts = await asyncio.to_thread(
                pack_7z_parts,
                extract_root,
                packed_path,
                pack_password,
                volume_bytes=volume_bytes,
            )
            split_archive = (
                len(packed_parts) > 1 or packed_parts[0].name.endswith(".001")
            )
            job["packed_7z"] = relative_to_games(settings, packed_parts[0])
            job["packed_dir"] = relative_to_games(settings, packed_path.parent)
            job["packed_parts"] = [
                {
                    "path": relative_to_games(settings, part),
                    "name": part.name,
                    "size": part.stat().st_size,
                }
                for part in packed_parts
            ]
            job["split_archive"] = split_archive
            job["split_volume_mb"] = settings.split_volume_mb
            self._touch(settings, job)

            if extract_root.exists():
                await asyncio.to_thread(shutil.rmtree, extract_root, True)
                job["extracted_cleaned"] = True
                self._touch(settings, job)

        baidu_key = baidu_title_folder(title)
        disguise_dir = packed_parts[0].parent / ".disguise-upload"
        baidu_names: list[str] = list(job.get("baidu_upload_names") or [])

        async def prepare_disguise(
            source: Path, index: int, total: int
        ) -> PreparedUpload:
            if not apate_on:
                if split_archive:
                    raise ApateError("百度/夸克分卷必须启用 Apate 伪装后才能上传")
                return PreparedUpload(source, source.name)
            if not apate_available(settings):
                raise ApateError("百度/夸克上传需要 Apate，但找不到 apate 可执行文件")
            remote_name = (
                f"{baidu_key}.{index:03d}.mp4" if split_archive else f"{baidu_key}.mp4"
            )
            dest = disguise_dir / remote_name
            if dest.is_file():
                if remote_name not in baidu_names:
                    baidu_names.append(remote_name)
                return PreparedUpload(dest, remote_name, temporary=False)
            dest.parent.mkdir(parents=True, exist_ok=True)
            await self._progress(
                settings,
                job,
                "disguise",
                index - 1,
                total,
                f"正在伪装网盘文件 {index}/{total}",
            )
            try:
                await asyncio.to_thread(disguise_to_mp4, settings, source, dest)
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            if remote_name not in baidu_names:
                baidu_names.append(remote_name)
            return PreparedUpload(dest, remote_name, temporary=False)

        async def cloud_progress(
            target: str, current: int, total: int, message: str
        ) -> None:
            await self._progress(settings, job, "uploading", current, total, message)

        async def persist_upload(partial: dict[str, Any]) -> None:
            _apply_cloud_result(
                job, partial, manual_links=manual_links, apate_on=apate_on
            )
            job["baidu_upload_names"] = baidu_names
            self._touch(settings, job)

        async def run_upload(only: list[str] | None) -> dict[str, Any]:
            try:
                return await upload_targets(
                    settings,
                    title=title,
                    archive_parts=packed_parts,
                    prepare_baidu=prepare_disguise,
                    prepare_disguise=prepare_disguise,
                    manual=manual_links,
                    only=only,
                    progress=cloud_progress,
                    share_pwd=str(job.get("share_pwd") or "") or None,
                    on_update=persist_upload,
                )
            except asyncio.CancelledError:
                raise
            except GamesCloudUploadError:
                raise
            except Exception as exc:
                raise GamesCloudUploadError(str(exc)) from exc

        first_only, deferred = cloud_upload_phases(settings, selected_clouds)
        await self._progress(settings, job, "uploading", 0, 4, "开始上传网盘")
        try:
            uploaded = await run_upload(first_only)
            links, share_pwd = _apply_cloud_result(
                job, uploaded, manual_links=manual_links, apate_on=apate_on
            )
            job["baidu_upload_names"] = baidu_names
            self._touch(settings, job)
            if not links and deferred:
                uploaded = await run_upload(deferred)
                links, share_pwd = _apply_cloud_result(
                    job, uploaded, manual_links=manual_links, apate_on=apate_on
                )
                job["baidu_upload_names"] = baidu_names
                deferred = []
                self._touch(settings, job)
            if not links:
                detail = "；".join(
                    f"{key}: {value}"
                    for key, value in (job.get("cloud_errors") or {}).items()
                )
                raise GamesCloudUploadError(f"没有可用的分享链接。{detail}")

            await self._progress(
                settings, job, "publishing", 0, 1, "上传图片并发布 WordPress"
            )
            image_urls: list[str] = []
            featured_id = None
            for index, item in enumerate(job.get("images") or [], start=1):
                raw = item.get("path") if isinstance(item, dict) else str(item)
                if not raw:
                    continue
                path = resolve_games_path(settings, str(raw))
                media = await upload_media(settings, path, f"{title}-{index}")
                url = str(media.get("source_url") or "")
                if url:
                    image_urls.append(url)
                if featured_id is None:
                    try:
                        featured_id = int(media.get("id"))
                    except (TypeError, ValueError):
                        featured_id = None
                await self._progress(
                    settings,
                    job,
                    "publishing",
                    index,
                    max(len(job.get("images") or []), 1),
                    f"已上传图片 {index}",
                )
            if image_urls:
                job["image_urls"] = image_urls

            tag_ids = await resolve_tag_ids(settings, tags)
            content = build_post_html(
                summary=summary,
                image_urls=image_urls,
                title=title,
                pack_password=pack_password,
                links=links,
                apate_used=bool(job.get("apate_used")),
                pay_enabled=pay_enabled,
                pay_in_content=False,
                share_pwd=share_pwd,
            )
            post = await create_post(
                settings,
                title=title,
                content=content,
                categories=categories,
                tags=tag_ids,
                featured_media=featured_id,
                pay_enabled=pay_enabled,
                price=price,
                points_price=points_price,
                pay_modo=pay_modo,
                links=links,
                pack_password=pack_password,
                apate_used=bool(job.get("apate_used")),
                share_pwd=share_pwd,
                extra_template=settings.wp_pay_extra_template,
                apate_url=settings.wp_apate_url,
                vip1_price=vip1_price,
                vip2_price=vip2_price,
                vip1_points=vip1_points,
                vip2_points=vip2_points,
                status=str(payload.get("status") or settings.wp_status or "publish"),
                source_key=str(
                    job.get("source_url") or job.get("source_key") or ""
                ),
            )
            public_url = str(post.get("link") or "")
            job["wp_id"] = post.get("id")
            job["public_url"] = public_url
            job["cover_url"] = image_urls[0] if image_urls else None
            job["stage"] = "finalizing"
            job["running"] = True
            job["message"] = (
                "WordPress 已发布，正在补传百度"
                if deferred
                else "WordPress 已发布，正在记录结果"
            )
            job["error"] = ""
            self._touch(settings, job)
            if deferred:
                await self._progress(
                    settings, job, "uploading", 0, len(deferred), "网站已发布，正在补传百度"
                )
                uploaded = await run_upload(deferred)
                links, share_pwd = _apply_cloud_result(
                    job, uploaded, manual_links=manual_links, apate_on=apate_on
                )
                job["baidu_upload_names"] = baidu_names
                self._touch(settings, job)
                await self._refresh_published_job(settings, job, payload)
            else:
                finalized = finalize_published_job(settings, job)
                self.snapshot = dict(finalized)
            if settings.cleanup_after_publish:
                if job.get("cloud_errors"):
                    job["message"] = "已发布到网站，但有网盘失败，已保留本地文件便于补传"
                    self._touch(settings, job)
                else:
                    try:
                        cleaned = cleanup_job_files(settings, job)
                        if cleaned.get("deleted"):
                            job["message"] = "已发布到网站，并清理本地文件"
                            self._touch(settings, job)
                    except Exception:
                        logger.exception("发布成功后清理本地游戏文件失败")
            _alert_cloud_errors(job)
        finally:
            if disguise_dir.exists():
                await asyncio.to_thread(shutil.rmtree, disguise_dir, True)


_RUNNER: GamesJobRunner | None = None


def get_games_runner() -> GamesJobRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = GamesJobRunner()
    return _RUNNER
