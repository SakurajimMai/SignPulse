from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.services.games.apate import (
    ApateError,
    apate_available,
    disguise_to_mp4,
    probe_apate,
)
from backend.services.games.archive import (
    extract_tree,
    find_7z_bin,
    pack_7z_parts,
    strip_ads,
)
from backend.services.games.clouds import (
    CLOUD_TARGETS,
    PreparedUpload,
    baidu_title_folder,
    cloud_status,
    normalize_upload_targets,
    upload_targets,
)
from backend.services.games.telegram import pull_telegram_post, resolve_account
from backend.services.manga.hmw.telegram_link import safe_folder_name
from backend.utils.time import utc_now_iso_z

from .catalog import find_existing_entry, upsert_entry
from .cleanup import cleanup_job_files
from .config import CoserSettings, load_coser_settings, overlay_games_settings
from .gallery import collect_source_images, gallery_local_dir, sync_gallery
from .keys import titles_match
from .paths import coser_dirs, relative_to_coser, resolve_coser_path
from .s3 import CoserS3Error, probe_s3, s3_configured
from .site import CoserSiteClient, CoserSiteError, public_work_url

logger = logging.getLogger("backend.coser.worker")


class CoserBusyError(RuntimeError):
    pass


class CoserCloudUploadError(RuntimeError):
    pass


def _job_path(settings: CoserSettings, job_id: str) -> Path:
    return coser_dirs(settings).tasks / f"{job_id}.json"


def save_job(settings: CoserSettings, job: dict[str, Any]) -> dict[str, Any]:
    job["updated_at"] = utc_now_iso_z()
    path = _job_path(settings, str(job["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def load_job(settings: CoserSettings, job_id: str) -> dict[str, Any]:
    path = _job_path(settings, job_id)
    if not path.is_file():
        raise FileNotFoundError("任务不存在")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise FileNotFoundError("任务损坏")
    return raw


def list_jobs(settings: CoserSettings, limit: int = 40) -> list[dict[str, Any]]:
    directory = coser_dirs(settings).tasks
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


def new_job(settings: CoserSettings, **extra: Any) -> dict[str, Any]:
    job = {
        "id": uuid4().hex,
        "running": False,
        "stage": "idle",
        "current": 0,
        "total": 0,
        "message": "",
        "error": "",
        "title": "",
        "summary": "",
        "coser_id": None,
        "coser_name": "",
        "is_r18": False,
        "apate": settings.apate_enabled,
        "source_url": "",
        "images": [],
        "archives": [],
        "archive_members": [],
        "links": {},
        "share_pwd": "",
        "cloud_errors": {},
        "public_url": None,
        "site_work_id": None,
        "cover_url": None,
        "image_urls": [],
        "gallery_reason": "",
        "gallery_folder": "",
        "gallery_skipped": False,
        "created_at": utc_now_iso_z(),
        "updated_at": utc_now_iso_z(),
    }
    job.update(extra)
    return save_job(settings, job)


def _existing_packed_parts(settings: CoserSettings, job: dict[str, Any]) -> list[Path]:
    parts: list[Path] = []
    for item in job.get("packed_parts") or []:
        raw = item.get("path") if isinstance(item, dict) else item
        if not raw:
            continue
        path = resolve_coser_path(settings, str(raw))
        if path.is_file():
            parts.append(path)
    return parts


def _download_links(job: dict[str, Any], *, pack_password: str, apate_on: bool) -> list[dict[str, Any]]:
    links = job.get("links") if isinstance(job.get("links"), dict) else {}
    share_pwd = str(job.get("share_pwd") or "")
    items: list[dict[str, Any]] = []
    packed = list(job.get("packed_parts") or [])
    size = ""
    if packed:
        first = packed[0]
        raw = first.get("path") if isinstance(first, dict) else first
        size = str(first.get("size") or "") if isinstance(first, dict) else ""
        if size.isdigit():
            n = int(size)
            size = f"{n / (1024 * 1024):.1f}MB" if n else ""
        elif not size and raw:
            size = ""
    for platform, url in links.items():
        if not str(url or "").strip():
            continue
        disguised = bool(apate_on and platform in {"baidu", "quark"})
        items.append(
            {
                "platform": platform,
                "language": "all",
                "url": str(url).strip(),
                "password": share_pwd or None,
                "extract_password": pack_password or None,
                "file_size": size or None,
                "file_format": "mp4" if disguised else "7z",
                "is_disguised": disguised,
                "disguise_note": "Apate MP4，请先还原再解压" if disguised else None,
            }
        )
    return items


def _job_image_paths(settings: CoserSettings, job: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for item in job.get("images") or []:
        raw = item.get("path") if isinstance(item, dict) else str(item)
        if not raw:
            continue
        try:
            path = resolve_coser_path(settings, str(raw))
        except Exception:
            continue
        if path.is_file():
            paths.append(path)
    return paths


def _local_work_id(
    settings: CoserSettings,
    job: dict[str, Any],
    *,
    title: str,
    coser_id: int,
) -> int:
    try:
        current = int(job.get("site_work_id") or 0)
    except (TypeError, ValueError):
        current = 0
    if current > 0:
        return current
    entry = find_existing_entry(
        settings,
        coser_id=coser_id,
        title=title,
        source_url=str(job.get("source_url") or ""),
        job_id=str(job.get("id") or ""),
    )
    if entry and entry.get("id"):
        try:
            return int(entry["id"])
        except (TypeError, ValueError):
            pass
    source_url = str(job.get("source_url") or "").strip()
    for other in list_jobs(settings, limit=80):
        try:
            other_id = int(other.get("site_work_id") or 0)
        except (TypeError, ValueError):
            other_id = 0
        if other_id <= 0:
            continue
        if source_url and str(other.get("source_url") or "").strip() == source_url:
            return other_id
        if int(other.get("coser_id") or 0) == int(coser_id) and titles_match(
            str(other.get("title") or ""), title
        ):
            return other_id
    return 0


async def probe_status(settings: CoserSettings) -> dict[str, Any]:
    games = overlay_games_settings(settings)
    site = CoserSiteClient()
    site_probe = await site.probe(settings)
    s3_probe = await asyncio.to_thread(probe_s3, settings)
    seven = find_7z_bin()
    apate = probe_apate(games)
    account = ""
    try:
        account = resolve_account(games, "")
    except Exception:
        account = ""
    return {
        "configured": bool(
            settings.site_email
            and settings.site_password
            and settings.site_url
            and s3_configured(settings)
        ),
        "site": site_probe,
        "s3": s3_probe,
        "sevenzip": {"ok": bool(seven), "bin": seven},
        "apate_tool": apate,
        "clouds": cloud_status(games),
        "telegram_account": account,
    }


class CoserJobRunner:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self.snapshot: dict[str, Any] = {"running": False, "stage": "idle"}
        self.site = CoserSiteClient()

    def status(self) -> dict[str, Any]:
        task = self._task
        if task is not None and task.done():
            self.snapshot["running"] = False
            self._task = None
        return dict(self.snapshot)

    def _touch(self, settings: CoserSettings, job: dict[str, Any]) -> dict[str, Any]:
        saved = save_job(settings, job)
        self.snapshot = dict(saved)
        return saved

    async def _progress(
        self,
        settings: CoserSettings,
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
        self, settings: CoserSettings, *, url: str, account: str = ""
    ) -> dict[str, Any]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise CoserBusyError("已有 Coser 任务在运行")
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
                name=f"coser-pull-{job['id'][:8]}",
            )
            return self.status()

    async def start_publish(
        self, settings: CoserSettings, *, job_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise CoserBusyError("已有 Coser 任务在运行")
            job = load_job(settings, job_id)
            job.update({key: value for key, value in payload.items() if value is not None})
            job["running"] = True
            job["stage"] = "queued"
            job["error"] = ""
            job["message"] = "准备解压打包"
            self._cancel = asyncio.Event()
            self._touch(settings, job)
            self._task = asyncio.create_task(
                self._run_publish(settings, job, payload),
                name=f"coser-publish-{job_id[:8]}",
            )
            return self.status()

    async def wait_for_job(self, job_id: str) -> dict[str, Any]:
        task = self._task
        if task is not None and str(self.snapshot.get("id") or "") == str(job_id):
            await asyncio.shield(task)
        return load_job(load_coser_settings(), str(job_id))

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
                logger.exception("取消 Coser 任务时出错")
        self.snapshot["running"] = False
        self.snapshot["stage"] = "cancelled"
        self.snapshot["message"] = "任务已取消"
        self.snapshot["error"] = "任务已取消"
        return self.status()

    async def _run_pull(
        self, settings: CoserSettings, job: dict[str, Any], *, url: str, account: str
    ) -> None:
        try:

            async def progress(stage: str, current: int, total: int, message: str) -> None:
                await self._progress(settings, job, stage, current, total, message)

            await self._progress(settings, job, "pulling", 0, 1, "正在读取频道帖")
            games = overlay_games_settings(settings)
            result = await pull_telegram_post(
                games,
                url,
                account=account or settings.telegram_account_name,
                job_id=str(job["id"]),
                progress=progress,
                dirs=coser_dirs(settings).as_games_dirs(),
            )
            job.update(result)
            job["stage"] = "ready"
            job["running"] = False
            job["message"] = (
                f"已拉取 {len(job.get('images') or [])} 张图，{len(job.get('archives') or [])} 个文件"
            )
            job["error"] = ""
            self._touch(settings, job)
        except asyncio.CancelledError:
            job.update(
                {
                    "running": False,
                    "stage": "cancelled",
                    "error": "任务已取消",
                    "message": "任务已取消",
                }
            )
            self._touch(settings, job)
            raise
        except Exception as exc:
            logger.exception("拉取 Coser 帖失败")
            job.update(
                {
                    "running": False,
                    "stage": "failed",
                    "error": str(exc),
                    "message": str(exc),
                }
            )
            self._touch(settings, job)
        finally:
            self.snapshot["running"] = False

    async def _run_publish(
        self, settings: CoserSettings, job: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        disguise_dir: Path | None = None
        try:
            await self._publish(settings, job, payload)
        except asyncio.CancelledError:
            job.update(
                {
                    "running": False,
                    "stage": "cancelled",
                    "error": "任务已取消",
                    "message": "任务已取消",
                }
            )
            self._touch(settings, job)
            raise
        except CoserCloudUploadError as exc:
            logger.exception("Coser 网盘上传失败")
            job.update(
                {
                    "running": False,
                    "stage": "failed",
                    "error": str(exc),
                    "message": str(exc),
                }
            )
            self._touch(settings, job)
        except Exception as exc:
            logger.exception("Coser 发布失败")
            job.update(
                {
                    "running": False,
                    "stage": "failed",
                    "error": str(exc),
                    "message": str(exc),
                }
            )
            self._touch(settings, job)
        finally:
            if disguise_dir is not None and disguise_dir.exists():
                await asyncio.to_thread(shutil.rmtree, disguise_dir, True)
            self.snapshot["running"] = False

    async def _resolve_coser_name(
        self, settings: CoserSettings, coser_id: int, hinted: str
    ) -> str:
        name = str(hinted or "").strip()
        if name:
            return name
        try:
            people = await self.site.list_cosers(settings)
        except Exception:
            people = []
        for person in people:
            if int(person.get("id") or 0) == int(coser_id):
                return str(person.get("name") or "").strip()
        return ""

    async def _load_site_work(
        self, settings: CoserSettings, work_id: int
    ) -> dict[str, Any] | None:
        if work_id <= 0:
            return None
        try:
            return await self.site.get_work(settings, work_id)
        except CoserSiteError:
            return None
        except Exception:
            logger.warning("读取作品 %s 失败", work_id, exc_info=True)
            return None

    async def _find_site_work(
        self, settings: CoserSettings, *, coser_id: int, title: str
    ) -> dict[str, Any] | None:
        found: list[dict[str, Any]] = []
        for status in ("approved", "pending"):
            try:
                found.extend(
                    await self.site.list_works(
                        settings, coser_id=coser_id, query=title, status=status
                    )
                )
            except Exception:
                logger.warning("搜索 icoser.de 作品失败 status=%s", status, exc_info=True)
        matched = [
            item
            for item in found
            if titles_match(str(item.get("title") or ""), title)
        ]
        if not matched:
            return None
        best = max(matched, key=lambda item: int(item.get("id") or 0))
        detail = await self._load_site_work(settings, int(best.get("id") or 0))
        return detail or best

    async def _resolve_work(
        self,
        settings: CoserSettings,
        job: dict[str, Any],
        *,
        title: str,
        summary: str,
        coser_id: int,
        is_r18: bool,
        create: bool,
    ) -> dict[str, Any]:
        work_id = _local_work_id(settings, job, title=title, coser_id=coser_id)
        work = await self._load_site_work(settings, work_id)
        if work is None:
            work = await self._find_site_work(
                settings, coser_id=coser_id, title=title
            )
        if work and work.get("id"):
            job["site_work_id"] = int(work["id"])
            job["public_url"] = public_work_url(settings, int(work["id"]))
            self._touch(settings, job)
            return work
        if not create:
            return {}
        created = await self.site.create_work(
            settings,
            {
                "title": title,
                "description": summary,
                "coser_id": coser_id,
                "is_r18": is_r18,
                "status": "pending",
                "images": [],
                "photo_count": 0,
            },
        )
        new_id = int(created.get("id") or 0)
        if new_id <= 0:
            raise CoserSiteError("icoser.de 未返回作品 ID")
        job["site_work_id"] = new_id
        job["public_url"] = public_work_url(settings, new_id)
        self._touch(settings, job)
        return created

    async def _publish(
        self, settings: CoserSettings, job: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        title = str(payload.get("title") or job.get("title") or "").strip() or "未命名作品"
        summary = str(payload.get("summary") or job.get("summary") or "").strip()
        try:
            coser_id = int(payload.get("coser_id") or job.get("coser_id") or 0)
        except (TypeError, ValueError):
            coser_id = 0
        if coser_id <= 0:
            raise RuntimeError("请选择 Coser")
        is_r18 = bool(
            payload["is_r18"]
            if payload.get("is_r18") is not None
            else job.get("is_r18")
        )
        apate_on = (
            bool(payload["apate"])
            if payload.get("apate") is not None
            else bool(job.get("apate", settings.apate_enabled))
        )
        pack_password = str(
            payload.get("pack_password") or settings.pack_password or "sakuramai"
        ).strip()
        extract_password = str(payload.get("extract_password") or "").strip()
        job.update(
            {
                "title": title,
                "summary": summary,
                "coser_id": coser_id,
                "coser_name": str(payload.get("coser_name") or job.get("coser_name") or ""),
                "is_r18": is_r18,
                "apate": apate_on,
            }
        )
        games = overlay_games_settings(settings)
        dirs = coser_dirs(settings)
        extract_root = dirs.extracted / str(job["id"])
        extracted_images: list[Path] = []
        packed_parts = _existing_packed_parts(settings, job)
        if packed_parts:
            await self._progress(settings, job, "packing", 1, 1, "复用已打包的 7z")
            split_archive = len(packed_parts) > 1 or packed_parts[0].name.endswith(".001")
        else:
            archives = list(job.get("archives") or [])
            if not archives:
                raise RuntimeError("没有可解压的 Coser 文件")
            passwords = [extract_password, *settings.password_list, *games.password_list]
            await self._progress(
                settings, job, "extracting", 0, max(len(archives), 1), "正在解压"
            )
            first = True
            for index, item in enumerate(archives, start=1):
                raw = item.get("path") if isinstance(item, dict) else str(item)
                source = resolve_coser_path(settings, str(raw))
                target = extract_root if first else extract_root / f"part{index}"
                await self._progress(
                    settings, job, "extracting", index - 1, len(archives), f"解压 {source.name}"
                )
                await asyncio.to_thread(extract_tree, source, target, passwords)
                first = False
            await asyncio.to_thread(strip_ads, extract_root, settings.ad_keyword_list)
            if not any(extract_root.rglob("*")):
                raise RuntimeError("解压后没有可用文件，请检查密码")
            extracted_images = collect_source_images(extract_root)
            packed_name = f"{safe_folder_name(title, fallback='coser')}.7z"
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
            split_archive = len(packed_parts) > 1 or packed_parts[0].name.endswith(".001")
            job["packed_parts"] = [
                {
                    "path": relative_to_coser(settings, part),
                    "name": part.name,
                    "size": part.stat().st_size,
                }
                for part in packed_parts
            ]
            job["packed_dir"] = relative_to_coser(settings, packed_path.parent)
            job["split_archive"] = split_archive
            self._touch(settings, job)

        source_images = extracted_images or collect_source_images(*_job_image_paths(settings, job))
        coser_name = await self._resolve_coser_name(
            settings, coser_id, str(job.get("coser_name") or "")
        )
        if not coser_name:
            raise RuntimeError("无法解析 Coser 名称，无法生成 CDN 路径")
        job["coser_name"] = coser_name
        site_work = await self._resolve_work(
            settings,
            job,
            title=title,
            summary=summary,
            coser_id=coser_id,
            is_r18=is_r18,
            create=bool(source_images),
        )
        work_id = int(site_work.get("id") or job.get("site_work_id") or 0)
        if work_id <= 0:
            raise CoserSiteError("icoser.de 未返回作品 ID")
        if not source_images:
            source_images = collect_source_images(
                gallery_local_dir(
                    settings, coser_name=coser_name, work_id=work_id, title=title
                )
            )
        site_images = [
            str(url).strip()
            for url in (site_work.get("images") or [])
            if str(url).strip()
        ]
        if not source_images and not site_images:
            raise CoserS3Error("没有可上传的图集")

        image_urls = list(site_images)
        if source_images:
            await self._progress(
                settings, job, "uploading_s3", 0, len(source_images), "正在同步图集到 CDN"
            )

            def gallery_progress(current: int, total: int, message: str) -> None:
                job["stage"] = "uploading_s3"
                job["current"] = current
                job["total"] = total
                job["message"] = message
                job["running"] = True
                self._touch(settings, job)

            gallery = await asyncio.to_thread(
                sync_gallery,
                settings,
                sources=source_images,
                work_id=work_id,
                title=title,
                coser_name=coser_name,
                site_images=site_images,
                progress=gallery_progress,
            )
            image_urls = list(gallery.urls)
            job["image_urls"] = image_urls
            job["cover_url"] = gallery.cover
            job["gallery_reason"] = gallery.reason
            job["gallery_folder"] = gallery.folder
            job["gallery_skipped"] = gallery.skipped
            if gallery.skipped:
                await self._progress(
                    settings,
                    job,
                    "uploading_s3",
                    len(image_urls),
                    len(image_urls),
                    "图集已在 CDN，跳过重复上传",
                )
            self._touch(settings, job)
        elif site_images:
            job["image_urls"] = image_urls
            job["cover_url"] = str(site_work.get("cover_image") or image_urls[0])
            job["gallery_reason"] = "reuse"
            job["gallery_skipped"] = True
            self._touch(settings, job)

        if extract_root.exists():
            await asyncio.to_thread(shutil.rmtree, extract_root, True)

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
            if not apate_available(games):
                raise ApateError("百度/夸克上传需要 Apate，但找不到 apate 可执行文件")
            remote_name = (
                f"{baidu_key}.{index:03d}.mp4" if split_archive else f"{baidu_key}.mp4"
            )
            dest = disguise_dir / remote_name
            if dest.is_file():
                return PreparedUpload(dest, remote_name, temporary=False)
            dest.parent.mkdir(parents=True, exist_ok=True)
            await self._progress(
                settings, job, "disguise", index - 1, total, f"正在伪装网盘文件 {index}/{total}"
            )
            await asyncio.to_thread(disguise_to_mp4, games, source, dest)
            if remote_name not in baidu_names:
                baidu_names.append(remote_name)
            return PreparedUpload(dest, remote_name, temporary=False)

        raw_clouds = payload.get("clouds", job.get("clouds"))
        if isinstance(raw_clouds, str):
            raw_clouds = [part.strip() for part in raw_clouds.split(",") if part.strip()]
        selected = (
            normalize_upload_targets(raw_clouds) if raw_clouds is not None else None
        )
        targets = list(CLOUD_TARGETS if selected is None else selected)
        incoming = payload.get("links") if isinstance(payload.get("links"), dict) else {}
        existing = job.get("links") if isinstance(job.get("links"), dict) else {}
        manual = {
            str(key): str(value).strip()
            for key, value in {**existing, **incoming}.items()
            if str(value or "").strip()
        }

        async def cloud_progress(target: str, current: int, total: int, message: str) -> None:
            await self._progress(settings, job, "uploading", current, total, message)

        await self._progress(settings, job, "uploading", 0, len(targets), "开始上传网盘")
        uploaded = await upload_targets(
            games,
            title=title,
            archive_parts=packed_parts,
            prepare_baidu=prepare_disguise,
            prepare_disguise=prepare_disguise,
            manual=manual,
            only=targets,
            progress=cloud_progress,
            share_pwd=str(job.get("share_pwd") or "") or None,
        )
        links = dict(manual)
        links.update(uploaded.get("links") or {})
        errors = dict(uploaded.get("errors") or {})
        job["links"] = links
        job["cloud_errors"] = errors
        job["share_pwd"] = uploaded.get("share_pwd") or job.get("share_pwd") or ""
        job["baidu_upload_names"] = baidu_names
        job["apate_used"] = apate_on
        if errors and not links:
            raise CoserCloudUploadError("；".join(f"{k}: {v}" for k, v in errors.items()))
        self._touch(settings, job)

        await self._progress(settings, job, "publishing", 0, 1, "正在发布到 icoser.de")
        download_links = _download_links(
            job, pack_password=pack_password, apate_on=apate_on
        )
        work_payload = {
            "title": title,
            "description": summary,
            "coser_id": coser_id,
            "cover_image": job.get("cover_url") or (image_urls[0] if image_urls else None),
            "images": image_urls,
            "photo_count": len(image_urls),
            "is_r18": is_r18,
            "status": "approved",
        }
        if download_links:
            work_payload["download_links"] = download_links
        created = await self.site.update_work(settings, work_id, work_payload)
        job["site_work_id"] = work_id
        job["public_url"] = public_work_url(settings, work_id)
        job["cover_url"] = created.get("cover_image") or job.get("cover_url")
        upsert_entry(
            settings,
            {
                "id": work_id,
                "title": title,
                "summary": summary,
                "cover_url": job.get("cover_url"),
                "public_url": job.get("public_url"),
                "coser_id": coser_id,
                "coser_name": job.get("coser_name"),
                "is_r18": is_r18,
                "links": job.get("links") or {},
                "source_url": job.get("source_url"),
                "source_key": job.get("source_key"),
                "job_id": job.get("id"),
                "apate": apate_on,
                "cdn_folder": job.get("gallery_folder") or "",
                "image_count": len(image_urls),
            },
        )
        message = "已发布到 icoser.de"
        if errors:
            failed = "、".join(str(key) for key in errors)
            message = f"已发布到网站，但 {failed} 失败，已保留本地文件便于补传"
        elif settings.cleanup_after_publish:
            cleanup_job_files(settings, job)
            message = "已发布到网站，并清理本地文件"
        job.update(
            {
                "stage": "done",
                "running": False,
                "message": message,
                "error": "",
            }
        )
        self._touch(settings, job)
        if disguise_dir.exists():
            await asyncio.to_thread(shutil.rmtree, disguise_dir, True)


_runner: CoserJobRunner | None = None


def get_coser_runner() -> CoserJobRunner:
    global _runner
    if _runner is None:
        _runner = CoserJobRunner()
    return _runner
