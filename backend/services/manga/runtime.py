from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from backend.services.alerts import schedule_alert

from .config import MangaSettings, load_manga_settings, save_manga_settings
from .db import close_db, init_db

logger = logging.getLogger("backend.manga.runtime")

# 监听进程异常退出后自动拉起的等待；测试可改成 0
EHENTAI_RESTART_DELAY = 5.0


@dataclass
class MangaRuntime:
    settings: MangaSettings | None = None
    worker: Any | None = None
    worker_task: asyncio.Task | None = None
    last_error: str | None = None
    initialized: bool = False
    ehentai_worker: Any | None = None
    ehentai_task: asyncio.Task | None = None
    ehentai_error: str | None = None
    _ehentai_stopping: bool = False
    _ehentai_generation: int = 0

    async def initialize(self) -> MangaSettings:
        self.settings = load_manga_settings()
        self.settings.ensure_dirs()
        await init_db(self.settings)
        try:
            from .ehentai.catalog import apply_catalog

            apply_catalog(self.settings.data_dir)
        except Exception:
            logger.debug("EhTagTranslation 词库缓存未加载", exc_info=True)
        self.initialized = True
        return self.settings

    def current_settings(self) -> MangaSettings:
        if self.settings is None:
            self.settings = load_manga_settings()
        return self.settings

    def status(self) -> dict[str, Any]:
        settings = self.current_settings()
        task = self.worker_task
        if task and task.done() and self.worker is not None:
            self.last_error = self.last_error or "Telegram worker stopped"
            self.worker = None
            self.worker_task = None
        if self.worker is not None:
            worker_status = (
                "running" if self.worker_task and not self.worker_task.done() else "starting"
            )
        else:
            worker_status = "disabled" if not settings.enabled else "stopped"
        account = (settings.telegram_account_name or "").strip()
        return {
            "enabled": settings.enabled,
            "worker_status": worker_status,
            "last_error": self.last_error,
            "source_chats": len(settings.source_chat_list),
            "site_publish_enabled": bool(
                settings.site_publish_url and settings.site_publish_secret
            ),
            "outbound_publish_enabled": bool(
                settings.outbound_enabled and settings.outbound_channel.strip()
            ),
            "outbound_bot_configured": bool(settings.outbound_bot_token.strip()),
            "outbound_forward_videos": bool(settings.outbound_forward_videos),
            "imgbed_configured": bool(settings.cfbed_upload_url),
            "telegram_configured": bool(account),
            "telegram_account_name": account or None,
            "source_bindings": len(settings.binding_list),
            "telegram_authorized": self.worker is not None,
            "ehentai": self.ehentai_status(),
            "hmw": self.hmw_status(),
        }

    async def start_if_enabled(self) -> dict[str, Any]:
        settings = await self.initialize() if not self.initialized else self.current_settings()
        if settings.ehentai_enabled:
            try:
                await self.start_ehentai()
            except Exception as exc:
                self.ehentai_error = str(exc)
                logger.warning("E-Hentai 未能自动启动: %s", exc)
                schedule_alert(
                    "manga_ehentai_worker_fail",
                    title="E-Hentai 未能自动启动",
                    detail=str(exc),
                    fingerprint="start",
                )
        if not settings.enabled:
            return self.status()
        if not (settings.telegram_account_name or "").strip():
            self.last_error = "请在漫画设置中选择 /accounts 里已登录的采集账号"
            logger.warning(self.last_error)
            return self.status()
        return await self.start()

    async def start(self) -> dict[str, Any]:
        if not self.initialized:
            await self.initialize()
        if self.worker is not None and self.worker_task and not self.worker_task.done():
            return self.status()

        from .telegram_worker import TelegramMangaWorker

        settings = self.current_settings()
        settings.ensure_dirs()
        self.last_error = None
        worker = TelegramMangaWorker(settings)
        try:
            await worker.start()
        except Exception as exc:
            await worker.stop()
            self.last_error = str(exc)
            logger.exception("漫画 Telegram worker 启动失败")
            schedule_alert(
                "manga_ingest_worker_fail",
                title="漫画频道采集启动失败",
                detail=str(exc),
                fingerprint="start",
            )
            raise

        self.worker = worker

        async def serve() -> None:
            try:
                await worker.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("漫画 Telegram worker 运行失败")
                schedule_alert(
                    "manga_ingest_worker_fail",
                    title="漫画频道采集运行失败",
                    detail=str(exc),
                    fingerprint="worker",
                )
            finally:
                await worker.stop()

        self.worker_task = asyncio.create_task(serve(), name="manga-telegram-worker")
        logger.info("漫画 Telegram worker 已启动")
        return self.status()

    async def stop(self, *, close_database: bool = False) -> None:
        worker = self.worker
        task = self.worker_task
        self.worker = None
        self.worker_task = None
        if worker is not None:
            await worker.stop()
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if close_database:
            await close_db()
        logger.info("漫画 Telegram worker 已停止")

    def _telegram_client(self) -> Any:
        worker = self.worker
        return getattr(worker, "client", None) if worker is not None else None

    def hmw_status(self) -> dict[str, Any]:
        from backend.services.manga.hmw.lib.config import hmw_configured
        from backend.services.manga.hmw.worker import get_hmw_runner

        settings = self.current_settings()
        payload = get_hmw_runner().status()
        payload["configured"] = hmw_configured(settings)
        return payload

    def ehentai_status(self) -> dict[str, Any]:
        settings = self.current_settings()
        task = self.ehentai_task
        if task and task.done() and self.ehentai_worker is not None:
            self.ehentai_error = self.ehentai_error or "E-Hentai worker stopped"
            self.ehentai_worker = None
            self.ehentai_task = None
        from .ehentai.catalog import translation_status

        translations = translation_status(settings.data_dir).as_dict()
        if self.ehentai_worker is not None:
            payload = self.ehentai_worker.status()
            if self.ehentai_error:
                payload["last_error"] = payload.get("last_error") or self.ehentai_error
            payload["translations"] = translations
            return payload
        from .ehentai.worker import poll_seconds

        return {
            "worker_status": "disabled" if not settings.ehentai_enabled else "stopped",
            "last_error": self.ehentai_error,
            "cookie_configured": bool((settings.ehentai_cookie or "").strip()),
            "exhentai": bool(settings.ehentai_exhentai),
            "search_count": 0,
            "current": {},
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "recent": [],
            "translations": translations,
            "phase": "idle",
            "next_pass_at": None,
            "poll_seconds": poll_seconds(settings),
            "listening": False,
        }

    async def start_ehentai(self) -> dict[str, Any]:
        if not self.initialized:
            await self.initialize()
        if self.ehentai_worker is not None and self.ehentai_task and not self.ehentai_task.done():
            return self.ehentai_status()
        from .ehentai.worker import EhentaiWorker

        settings = self.current_settings()
        settings.ensure_dirs()
        if not settings.ehentai_enabled:
            # 启动监听即视为开启；否则开关未保存时会误报「请先启用」
            settings = save_manga_settings({"ehentai_enabled": True})
            self.settings = settings
            logger.info("已自动打开 E-Hentai 持续监听")
        from .ehentai.parser import split_searches

        if not split_searches(settings.ehentai_search):
            raise RuntimeError("请至少填写一条 E-Hentai 搜索词")
        if not settings.cfbed_upload_url:
            raise RuntimeError("请先配置图床，E-Hentai 图片要上传后再发主站")
        if settings.ehentai_exhentai and not (settings.ehentai_cookie or "").strip():
            raise RuntimeError("ExHentai 需要填写 cookie")
        from .ehentai.catalog import refresh_catalog

        if settings.ehentai_translation_auto:
            try:
                await refresh_catalog(
                    settings.data_dir,
                    source_url=settings.ehentai_translation_url,
                )
            except Exception as exc:
                logger.warning("EhTagTranslation 词库自动更新失败，使用现有缓存: %s", exc)
        self.ehentai_error = None
        worker = EhentaiWorker(settings, self._telegram_client)
        self.ehentai_worker = worker
        self._ehentai_generation += 1
        generation = self._ehentai_generation
        self.ehentai_task = asyncio.create_task(
            self._serve_ehentai(worker, generation), name="manga-ehentai-worker"
        )
        logger.info("E-Hentai 漫画监听已启动")
        return self.ehentai_status()

    async def _serve_ehentai(self, worker: Any, generation: int) -> None:
        crashed = False
        try:
            await worker.run_until_stopped()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            crashed = True
            self.ehentai_error = str(exc)
            logger.exception("E-Hentai worker 运行失败")
            schedule_alert(
                "manga_ehentai_worker_fail",
                title="E-Hentai 监听运行失败",
                detail=str(exc),
                fingerprint="worker",
            )
        finally:
            await worker.stop()
            if self.ehentai_worker is worker:
                self.ehentai_worker = None
                self.ehentai_task = None
            should_restart = (
                crashed
                and generation == self._ehentai_generation
                and not self._ehentai_stopping
                and bool(self.current_settings().ehentai_enabled)
            )
            if should_restart:
                logger.warning("E-Hentai 监听异常退出，将自动拉起")
                asyncio.create_task(
                    self._restart_ehentai(generation), name="manga-ehentai-restart"
                )

    async def _restart_ehentai(self, generation: int) -> None:
        await asyncio.sleep(max(float(EHENTAI_RESTART_DELAY), 0.0))
        if generation != self._ehentai_generation or self._ehentai_stopping:
            return
        settings = load_manga_settings()
        self.settings = settings
        if not settings.ehentai_enabled:
            return
        if self.ehentai_worker is not None and self.ehentai_task and not self.ehentai_task.done():
            return
        try:
            await self.start_ehentai()
        except Exception as exc:
            self.ehentai_error = str(exc)
            logger.exception("E-Hentai 自动拉起失败")

    async def stop_ehentai(self, *, persist_disabled: bool = False) -> dict[str, Any]:
        self._ehentai_stopping = True
        self._ehentai_generation += 1
        try:
            if persist_disabled:
                self.settings = save_manga_settings({"ehentai_enabled": False})
            worker = self.ehentai_worker
            task = self.ehentai_task
            self.ehentai_worker = None
            self.ehentai_task = None
            if worker is not None:
                await worker.stop()
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            logger.info("E-Hentai 漫画监听已停止")
            return self.ehentai_status()
        finally:
            self._ehentai_stopping = False

    async def request_ehentai_pass(self) -> dict[str, Any]:
        settings = self.current_settings()
        if not settings.ehentai_enabled:
            raise RuntimeError("请先打开持续监听")
        task = self.ehentai_task
        worker = self.ehentai_worker
        if worker is None or task is None or task.done():
            return await self.start_ehentai()
        request = getattr(worker, "request_pass", None)
        if callable(request):
            request()
            logger.info("E-Hentai 已触发立即搜索")
        return self.ehentai_status()

    async def backfill_ehentai(self, source_keys: list[str] | None = None) -> dict[str, Any]:
        if not self.initialized:
            await self.initialize()
        from .ehentai.worker import EhentaiWorker

        settings = self.current_settings()
        worker = self.ehentai_worker
        created = worker is None
        if created:
            worker = EhentaiWorker(settings, self._telegram_client)
        try:
            report = await worker.backfill_incomplete(source_keys)
        finally:
            if created:
                await worker.stop()
        payload = self.ehentai_status()
        payload["backfill"] = report
        return payload

    async def refresh_ehentai_translations(self, *, force: bool = True) -> dict[str, Any]:
        if not self.initialized:
            await self.initialize()
        settings = self.current_settings()
        from .ehentai.catalog import refresh_catalog

        status = await refresh_catalog(
            settings.data_dir,
            source_url=settings.ehentai_translation_url,
            force=force,
        )
        payload = self.ehentai_status()
        payload["translations"] = status.as_dict()
        return payload

    async def reload(self) -> dict[str, Any]:
        was_running = self.worker is not None
        await self.stop()
        await self.stop_ehentai()
        self.settings = load_manga_settings()
        self.initialized = False
        await self.initialize()
        if was_running and self.settings.enabled:
            await self.start()
        # 启用即常驻轮询，不要求上次已经点过「开始采集」
        if self.settings.ehentai_enabled:
            try:
                await self.start_ehentai()
            except Exception as exc:
                self.ehentai_error = str(exc)
                logger.warning("E-Hentai 未能随配置重载启动: %s", exc)
        return self.status()


_runtime: MangaRuntime | None = None


def get_manga_runtime() -> MangaRuntime:
    global _runtime
    if _runtime is None:
        _runtime = MangaRuntime()
    return _runtime
