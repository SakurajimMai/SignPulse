from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from backend.services.alerts import schedule_alert
from backend.services.manga.config import MangaSettings, load_manga_settings

from .catalog import list_catalog, upsert_from_detail
from .cleanup import cleanup_published_source
from .lib.state import TaskState, TaskStateStore
from .paths import hmw_dirs, resolve_source_path
from .service import (
    apply_chapter_overrides,
    build_manga_payload,
    make_clients,
    public_site_url,
    relative_to_hmw,
)
from .workflow import PanelPublishWorkflow

logger = logging.getLogger("backend.manga.hmw")


@dataclass
class HmwJobSnapshot:
    task_id: str | None = None
    running: bool = False
    stage: str = "idle"
    current: int = 0
    total: int = 0
    message: str = ""
    error: str = ""
    source_path: str = ""
    slug: str = ""
    title: str = ""
    language: str = "zh"
    public_url: str | None = None
    manga_id: int | None = None
    chapter_index: int = 0
    chapter_total: int = 0
    chapter_title: str = ""
    live_chapters: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "running": self.running,
            "stage": self.stage,
            "current": self.current,
            "total": self.total,
            "message": self.message,
            "error": self.error,
            "source_path": self.source_path,
            "slug": self.slug,
            "title": self.title,
            "language": self.language,
            "public_url": self.public_url,
            "manga_id": self.manga_id,
            "chapter_index": self.chapter_index,
            "chapter_total": self.chapter_total,
            "chapter_title": self.chapter_title,
            "live_chapters": list(self.live_chapters),
        }


class HmwBusyError(RuntimeError):
    pass


class HmwJobRunner:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self.snapshot = HmwJobSnapshot()

    def status(self) -> dict[str, Any]:
        task = self._task
        if task is not None and task.done():
            self.snapshot.running = False
            self._task = None
        return self.snapshot.as_dict()

    def _progress(self, stage: str, current: int, total: int, message: str) -> None:
        self.snapshot.stage = stage
        self.snapshot.current = current
        self.snapshot.total = total
        prefix = ""
        if self.snapshot.chapter_total:
            title = self.snapshot.chapter_title or ""
            prefix = f"第{self.snapshot.chapter_index}/{self.snapshot.chapter_total}话"
            if title:
                prefix += f" {title}"
            prefix += " · "
        self.snapshot.message = prefix + message

    async def start(
        self,
        settings: MangaSettings,
        *,
        source_path: str,
        manga: dict[str, Any],
        chapters: list[dict[str, Any]] | None = None,
        cover_path: str | None = None,
        task_id: str | None = None,
        genre_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise HmwBusyError("已有 HMW 发布任务在运行")
            self._cancel = asyncio.Event()
            job_id = task_id or uuid4().hex
            self.snapshot = HmwJobSnapshot(
                task_id=job_id,
                running=True,
                stage="queued",
                message="任务已排队",
                source_path=source_path,
                slug=str(manga.get("slug") or ""),
                title=str(manga.get("title") or ""),
                language=str(manga.get("language") or "zh"),
            )
            self._task = asyncio.create_task(
                self._run(
                    settings,
                    source_path=source_path,
                    manga=manga,
                    chapters=chapters,
                    cover_path=cover_path,
                    task_id=job_id,
                    genre_ids=genre_ids,
                ),
                name=f"hmw-publish-{job_id[:8]}",
            )
            return self.status()

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
                logger.exception("取消 HMW 任务时出错")
        self.snapshot.running = False
        if self.snapshot.stage not in {"cleaned", "committed", "cancelled"}:
            self.snapshot.stage = "cancelled"
            self.snapshot.error = self.snapshot.error or "任务已取消"
            self.snapshot.message = "任务已取消"
        return self.status()

    async def _run(
        self,
        settings: MangaSettings,
        *,
        source_path: str,
        manga: dict[str, Any],
        chapters: list[dict[str, Any]] | None,
        cover_path: str | None,
        task_id: str,
        genre_ids: list[int] | None,
    ) -> None:
        from .lib.discovery import discover_source
        from .lib.models import SourceDiscovery

        cfg, api, storage = make_clients(settings)
        dirs = hmw_dirs(settings)
        store = TaskStateStore(dirs.tasks)
        root = resolve_source_path(settings, source_path)
        source = discover_source(root)
        resolved_cover = None
        if cover_path:
            resolved_cover = resolve_source_path(settings, cover_path)
        source = apply_chapter_overrides(source, chapters, cover_path=resolved_cover)
        payload = build_manga_payload(settings, manga, genre_ids=genre_ids)
        self.snapshot.slug = payload["slug"]
        self.snapshot.title = payload["title"]
        self.snapshot.language = payload["language"]
        self.snapshot.source_path = relative_to_hmw(settings, root)
        active = [chapter for chapter in source.chapters if chapter.action != "skip"]
        if not active:
            raise RuntimeError("没有可发布的章节")
        self.snapshot.chapter_total = len(active)
        workflow = PanelPublishWorkflow(
            cfg,
            api,
            storage,
            store,
            temp_root=dirs.temp,
            progress=self._progress,
            convert_workers=cfg.convert_workers,
            cancel_event=self._cancel,
        )
        cover_url = payload.get("cover_url") or storage.public_url(storage.cover_key(payload["slug"]))
        last_state: TaskState | None = None
        try:
            async with api:
                for index, chapter in enumerate(active):
                    if self._cancel.is_set():
                        raise asyncio.CancelledError()
                    self.snapshot.chapter_index = index + 1
                    self.snapshot.chapter_title = chapter.title
                    self.snapshot.running = True
                    child_id = f"{task_id}-ch{chapter.number:g}"
                    child_path = store.path_for(child_id)
                    if child_path.exists():
                        existing = store.load(child_id)
                        if existing.stage in {"committed", "cleaned"}:
                            self._apply_state(existing, payload)
                            self._note_live_chapter(chapter, existing)
                            await self._touch_catalog(settings, api, payload)
                            continue
                    one = SourceDiscovery(root=source.root, cover=source.cover, chapters=[chapter])
                    manga_one = {**payload, "cover_url": cover_url}
                    include_cover = index == 0 or not bool(self.snapshot.live_chapters)
                    await self._progress(
                        "queued",
                        index,
                        len(active),
                        "开始转换并提交这一话",
                    )
                    last_state = await workflow.execute(
                        one,
                        manga_one,
                        confirm=lambda _plan: True,
                        task_id=child_id,
                        include_cover=include_cover,
                    )
                    self._apply_state(last_state, payload)
                    self._note_live_chapter(chapter, last_state)
                    await self._touch_catalog(settings, api, payload)
                    self.snapshot.running = True
                    self.snapshot.public_url = public_site_url(payload["language"], payload["slug"])
            if last_state is not None:
                self._apply_state(last_state, payload)
            live = len(self.snapshot.live_chapters)
            self.snapshot.message = f"已上线 {live}/{len(active)} 话"
            if live >= len(active) and not self.snapshot.error:
                try:
                    cleaned = cleanup_published_source(settings, root)
                    if cleaned.get("deleted"):
                        self.snapshot.message += "，已清理本地章节文件"
                except Exception:
                    logger.exception("发布成功后清理本地章节失败")
            self.snapshot.stage = "cleaned"
        except asyncio.CancelledError:
            self.snapshot.running = False
            self.snapshot.stage = "cancelled"
            self.snapshot.error = "任务已取消"
            self.snapshot.message = "任务已取消"
            raise
        except Exception as exc:
            logger.exception("HMW 发布失败")
            self.snapshot.running = False
            self.snapshot.error = str(exc)
            self.snapshot.message = str(exc)
            failed_stage = self.snapshot.stage
            if not self.snapshot.stage or self.snapshot.stage == "queued":
                self.snapshot.stage = "failed"
            if failed_stage == "converting":
                alert_rule = "manga_hmw_convert_fail"
            elif failed_stage in {"uploading", "verifying"}:
                alert_rule = "manga_hmw_storage_fail"
            elif failed_stage == "committing":
                alert_rule = "manga_site_fail"
            else:
                alert_rule = "manga_hmw_source_fail"
            schedule_alert(
                alert_rule,
                title=f"HMW 发布失败：{self.snapshot.title or self.snapshot.source_path or '漫画'}",
                detail=f"stage={failed_stage or 'unknown'}\n{exc}",
                fingerprint=str(
                    self.snapshot.source_path
                    or source_path
                    or self.snapshot.task_id
                    or "hmw"
                ),
            )
        finally:
            self.snapshot.running = False

    def _apply_state(self, state: TaskState, manga: dict[str, Any]) -> None:
        result = state.publish_result or {}
        manga_out = result.get("manga") if isinstance(result.get("manga"), dict) else result
        manga_id = result.get("manga_id")
        if manga_id is None and isinstance(manga_out, dict):
            manga_id = manga_out.get("id") or manga_out.get("manga_id")
        try:
            manga_id = int(manga_id) if manga_id is not None else None
        except (TypeError, ValueError):
            manga_id = None
        self.snapshot.task_id = state.task_id
        self.snapshot.stage = state.stage
        self.snapshot.error = state.error or ""
        self.snapshot.slug = state.manga_slug or manga.get("slug") or ""
        self.snapshot.manga_id = manga_id
        if state.stage in {"committed", "cleaned"}:
            self.snapshot.public_url = public_site_url(
                str(manga.get("language") or "zh"), self.snapshot.slug
            )
        self.snapshot.running = False

    def _note_live_chapter(self, chapter, state: TaskState) -> None:
        pages = sum(1 for item in state.uploaded_objects if item.kind == "page")
        entry = {
            "number": chapter.number,
            "title": chapter.title,
            "pages": pages or len(chapter.images),
        }
        self.snapshot.live_chapters = [
            item for item in self.snapshot.live_chapters if item.get("number") != chapter.number
        ] + [entry]

    async def _touch_catalog(self, settings: MangaSettings, api, payload: dict[str, Any]) -> None:
        extras = {
            "source_path": self.snapshot.source_path,
            "slug": self.snapshot.slug,
            "title": self.snapshot.title,
            "language": self.snapshot.language,
            "author": payload.get("author"),
            "cover_url": payload.get("cover_url"),
            "status": payload.get("status"),
            "description": payload.get("description"),
        }
        try:
            manga_id = self.snapshot.manga_id
            if manga_id:
                try:
                    detail = await api.get_manga(int(manga_id))
                    upsert_from_detail(settings, detail, extras=extras, published=True)
                    return
                except Exception:
                    logger.exception("刷新 HMW 漫画详情失败，改用本地快照写入目录")
            upsert_from_detail(
                settings,
                {
                    "id": manga_id,
                    "slug": self.snapshot.slug,
                    "title": self.snapshot.title,
                    "language": self.snapshot.language,
                    "author": payload.get("author"),
                    "cover_url": payload.get("cover_url"),
                    "status": payload.get("status"),
                    "description": payload.get("description"),
                    "chapters": [
                        {
                            "number": item.get("number"),
                            "title": item.get("title"),
                            "page_count": item.get("pages"),
                        }
                        for item in self.snapshot.live_chapters
                    ],
                },
                extras=extras,
                published=True,
            )
        except Exception:
            logger.exception("写入 HMW 本地发布目录失败")


_runner = HmwJobRunner()


def get_hmw_runner() -> HmwJobRunner:
    return _runner


def job_to_dict(state: TaskState) -> dict[str, Any]:
    result = state.publish_result or {}
    manga_id = result.get("manga_id")
    manga_out = result.get("manga") if isinstance(result.get("manga"), dict) else None
    if manga_id is None and isinstance(manga_out, dict):
        manga_id = manga_out.get("id")
    language = str((state.manga_payload or {}).get("language") or "zh")
    public_url = None
    if state.stage in {"committed", "cleaned"} and state.manga_slug:
        public_url = public_site_url(language, state.manga_slug)
    payload = state.manga_payload or {}
    return {
        "task_id": state.task_id,
        "stage": state.stage,
        "error": state.error,
        "source_path": state.source_path,
        "slug": state.manga_slug,
        "title": payload.get("title"),
        "author": payload.get("author"),
        "cover_url": payload.get("cover_url"),
        "uploaded": len(state.uploaded_objects),
        "manga_id": manga_id,
        "public_url": public_url,
        "preflight": {
            "manga_id": (state.preflight or {}).get("manga_id"),
            "manga_action": (state.preflight or {}).get("manga_action"),
            "chapters": [
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "action": item.get("action"),
                }
                for item in (state.preflight or {}).get("chapters") or []
            ],
        }
        if state.preflight
        else None,
    }


def list_jobs(settings: MangaSettings | None = None) -> list[dict[str, Any]]:
    settings = settings or load_manga_settings()
    store = TaskStateStore(hmw_dirs(settings).tasks)
    items = [item for item in store.list() if "-ch" not in item.task_id]
    jobs = [job_to_dict(item) for item in items[:30]]
    by_id: dict[Any, dict[str, Any]] = {}
    by_slug: dict[str, dict[str, Any]] = {}
    try:
        for entry in list_catalog(settings, q="", page=1, limit=100).get("data") or []:
            if entry.get("id") is not None:
                by_id[entry["id"]] = entry
            slug = str(entry.get("slug") or "")
            if slug:
                by_slug[slug] = entry
    except Exception:
        logger.exception("读取 HMW 本地目录以补全任务卡片失败")
    for job in jobs:
        hit = by_id.get(job.get("manga_id")) or by_slug.get(str(job.get("slug") or ""))
        if not hit:
            continue
        job["cover_url"] = job.get("cover_url") or hit.get("cover_url")
        job["author"] = job.get("author") or hit.get("author")
        job["manga_id"] = job.get("manga_id") or hit.get("id")
        job["public_url"] = job.get("public_url") or hit.get("public_url")
        job["page_count"] = hit.get("page_count")
        job["chapter_count"] = hit.get("chapter_count")
        job["updated_at"] = hit.get("last_published_at") or hit.get("updated_at")
    return jobs


def merge_status(job: dict[str, Any], probe: dict[str, Any] | None = None) -> dict[str, Any]:
    health = probe or {}
    merged = {
        "configured": bool(health.get("configured")),
        "api_ok": health.get("api_ok"),
        "s3_ok": health.get("s3_ok"),
        "probe_error": health.get("error"),
        **job,
        "job": job,
        "error": job.get("error") or "",
    }
    return merged


def load_job(settings: MangaSettings, task_id: str) -> TaskState:
    return TaskStateStore(hmw_dirs(settings).tasks).load(task_id)
