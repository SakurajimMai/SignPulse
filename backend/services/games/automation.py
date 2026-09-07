from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Any

from backend.utils.atomic_io import read_json_safe, write_json_atomic
from backend.utils.time import utc_now_iso_z

from .apate import apate_available
from .archive import find_7z_bin
from .catalog import catalog_has_source
from .config import GamesSettings, load_games_settings
from .paths import games_dirs
from .telegram import discover_telegram_posts, resolve_account
from .worker import (
    GamesBusyError,
    finalize_published_job,
    get_games_runner,
    load_job,
)

logger = logging.getLogger("backend.games.automation")

STATE_VERSION = 1
SCAN_LIMIT = 100
SETTLE_SECONDS = 15
MAX_DONE_RECORDS = 1000


def automation_state_path(settings: GamesSettings):
    return games_dirs(settings).root / "automation.json"


def _default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "channels": {},
        "items": {},
        "last_scan_at": None,
        "last_success_at": None,
        "last_error": None,
    }


def load_automation_state(settings: GamesSettings) -> dict[str, Any]:
    raw = read_json_safe(automation_state_path(settings), _default_state())
    if not isinstance(raw, dict):
        return _default_state()
    state = _default_state()
    state.update(raw)
    if not isinstance(state.get("channels"), dict):
        state["channels"] = {}
    if not isinstance(state.get("items"), dict):
        state["items"] = {}
    state["version"] = STATE_VERSION
    return state


def save_automation_state(settings: GamesSettings, state: dict[str, Any]) -> None:
    write_json_atomic(automation_state_path(settings), state)


def automation_requirements(settings: GamesSettings) -> dict[str, Any]:
    try:
        account = resolve_account(settings)
    except Exception:
        account = ""
    cloud_ready = bool(
        (settings.baidu_enabled and settings.baidu_cookie)
        or settings.pikpak_refresh_token
        or (settings.pikpak_username and settings.pikpak_password)
        or settings.terabox_cookie
        or settings.quark_cookie
        or (
            settings.openlist_url
            and settings.openlist_token
            and any(
                (
                    settings.openlist_baidu_path,
                    settings.openlist_pikpak_path,
                    settings.openlist_terabox_path,
                    settings.openlist_quark_path,
                )
            )
        )
    )
    checks = {
        "telegram_account": bool(account),
        "channels": bool(settings.source_channel_list),
        "wordpress": bool(
            settings.wp_url and settings.wp_user and settings.wp_app_password
        ),
        "cloud": cloud_ready,
        "sevenzip": bool(find_7z_bin()),
        "apate": bool(not settings.apate_enabled or apate_available(settings)),
    }
    labels = {
        "telegram_account": "Telegram 登录账号",
        "channels": "监听频道",
        "wordpress": "WordPress 账号与应用密码",
        "cloud": "至少一个网盘上传目标",
        "sevenzip": "7z 工具",
        "apate": "Apate 工具",
    }
    missing = [key for key, ready in checks.items() if not ready]
    return {
        **checks,
        "ready": not missing,
        "missing": missing,
        "message": ""
        if not missing
        else "缺少：" + "、".join(labels[key] for key in missing),
        "account": account or None,
    }


def _retry_delay(attempt: int) -> int:
    return min(3600, 30 * (2 ** max(int(attempt) - 1, 0)))


class GamesAutomationService:
    def __init__(self) -> None:
        self.settings: GamesSettings | None = None
        self.state: dict[str, Any] = _default_state()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._pass_lock = asyncio.Lock()
        self._active_source = ""

    def current_settings(self) -> GamesSettings:
        if self.settings is None:
            self.settings = load_games_settings()
            self.state = load_automation_state(self.settings)
        return self.settings

    def _save(self) -> None:
        save_automation_state(self.current_settings(), self.state)

    def _prune(self) -> None:
        items = self.state.get("items") or {}
        done = sorted(
            (
                (key, item)
                for key, item in items.items()
                if isinstance(item, dict) and item.get("status") == "done"
            ),
            key=lambda pair: str(pair[1].get("updated_at") or ""),
            reverse=True,
        )
        for key, _item in done[MAX_DONE_RECORDS:]:
            items.pop(key, None)

    def status(self, settings: GamesSettings | None = None) -> dict[str, Any]:
        current = settings or self.current_settings()
        task = self._task
        running = bool(task is not None and not task.done())
        if task is not None and task.done() and not task.cancelled():
            try:
                exc = task.exception()
            except Exception:
                exc = None
            if exc:
                self.state["last_error"] = str(exc)
        items = [
            item
            for item in (self.state.get("items") or {}).values()
            if isinstance(item, dict)
        ]
        counts = {
            "queued": sum(
                item.get("status") in {"pending", "processing", "failed"}
                for item in items
            ),
            "failed": sum(
                item.get("status") in {"failed", "exhausted"} for item in items
            ),
            "processed": sum(
                item.get("status") in {"done", "skipped"} for item in items
            ),
            "skipped": sum(item.get("status") == "skipped" for item in items),
        }
        recent = sorted(
            items,
            key=lambda item: str(
                item.get("updated_at") or item.get("discovered_at") or ""
            ),
            reverse=True,
        )[:8]
        disk = shutil.disk_usage(games_dirs(current).root)
        return {
            "enabled": bool(current.auto_publish_enabled),
            "listening": running,
            "worker_status": "running"
            if running
            else "disabled"
            if not current.auto_publish_enabled
            else "stopped",
            "channels": list(current.source_channel_list),
            "poll_seconds": current.telegram_poll_seconds,
            "backfill_limit": current.telegram_backfill_limit,
            "retry_limit": current.auto_retry_limit,
            "concurrency": 1,
            "processing_mode": "serial",
            "active_source": self._active_source or None,
            "last_scan_at": self.state.get("last_scan_at"),
            "last_success_at": self.state.get("last_success_at"),
            "last_error": self.state.get("last_error"),
            "requirements": automation_requirements(current),
            "disk": {
                "free_bytes": disk.free,
                "total_bytes": disk.total,
                "free_gb": round(disk.free / (1024**3), 1),
            },
            "recent": [
                {
                    key: item.get(key)
                    for key in (
                        "source_key",
                        "url",
                        "status",
                        "attempts",
                        "job_id",
                        "error",
                        "caption",
                        "public_url",
                        "updated_at",
                    )
                }
                for item in recent
            ],
            **counts,
        }

    async def start_if_enabled(self) -> dict[str, Any]:
        settings = self.current_settings()
        if not settings.auto_publish_enabled:
            return self.status(settings)
        return await self.start()

    async def start(self) -> dict[str, Any]:
        settings = self.current_settings()
        if self._task is not None and not self._task.done():
            return self.status(settings)
        requirements = automation_requirements(settings)
        if not requirements["ready"]:
            self.state["last_error"] = requirements["message"]
            self._save()
            return self.status(settings)
        self.state = load_automation_state(settings)
        self._wake = asyncio.Event()
        self.state["last_error"] = None
        self._task = asyncio.create_task(self._serve(), name="games-automation")
        logger.info(
            "游戏自动发布监听已启动 account=%s channels=%s poll=%ss backfill=%s",
            requirements.get("account"),
            ",".join(settings.source_channel_list),
            settings.telegram_poll_seconds,
            settings.telegram_backfill_limit,
        )
        return self.status(settings)

    async def stop(self, *, cancel_job: bool = False) -> dict[str, Any]:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if cancel_job:
            runner = get_games_runner()
            snapshot = runner.status()
            if snapshot.get("automatic") and snapshot.get("running"):
                await runner.cancel()
        self._active_source = ""
        logger.info("游戏自动发布监听已停止")
        return self.status()

    async def reload(self) -> dict[str, Any]:
        await self.stop()
        self.settings = load_games_settings()
        self.state = load_automation_state(self.settings)
        return await self.start_if_enabled()

    def request_scan(self) -> dict[str, Any]:
        self._wake.set()
        return self.status()

    async def scan_now(self) -> dict[str, Any]:
        settings = self.current_settings()
        requirements = automation_requirements(settings)
        return await self.run_pass(
            process_items=bool(settings.auto_publish_enabled and requirements["ready"])
        )

    def retry_failed(self) -> dict[str, Any]:
        changed = False
        now = utc_now_iso_z()
        for item in (self.state.get("items") or {}).values():
            if not isinstance(item, dict) or item.get("status") not in {
                "failed",
                "exhausted",
            }:
                continue
            item.update(
                {
                    "status": "pending",
                    "attempts": 0,
                    "next_retry_at": 0,
                    "error": "",
                    "updated_at": now,
                }
            )
            changed = True
        if changed:
            self.state["last_error"] = None
            self._save()
            self._wake.set()
        return self.status()

    async def _serve(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.run_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state["last_error"] = str(exc)
                try:
                    self._save()
                except Exception:
                    logger.exception("保存游戏自动发布异常状态失败")
                logger.exception("游戏自动发布本轮执行失败，将在下轮重试")
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=max(int(self.current_settings().telegram_poll_seconds), 10),
                )
            except asyncio.TimeoutError:
                pass

    async def run_pass(self, *, process_items: bool = True) -> dict[str, Any]:
        async with self._pass_lock:
            return await self._run_pass(process_items=process_items)

    async def _run_pass(self, *, process_items: bool) -> dict[str, Any]:
        settings = self.current_settings()
        self.state["last_scan_at"] = utc_now_iso_z()
        self.state["last_error"] = None
        await self._reconcile_processing(settings)
        for channel in settings.source_channel_list:
            try:
                await self._scan_channel(settings, channel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state["last_error"] = str(exc)
                logger.exception("扫描游戏频道失败 channel=%s", channel)
        self._save()

        if process_items:
            while True:
                item = self._next_item(settings)
                if item is None:
                    break
                if not await self._process_item(settings, item):
                    break
        self._prune()
        self._save()
        return self.status(settings)

    async def _scan_channel(self, settings: GamesSettings, channel: str) -> None:
        channels = self.state.setdefault("channels", {})
        channel_state = channels.setdefault(channel, {})
        first_scan = "cursor" not in channel_state
        cursor = int(channel_state.get("cursor") or 0)
        history_limit = max(
            SCAN_LIMIT,
            min(int(settings.telegram_backfill_limit) * 12, 2000),
        )
        result = await discover_telegram_posts(
            settings,
            channel,
            limit=history_limit,
            settle_seconds=SETTLE_SECONDS,
            after_message_id=0 if first_scan else cursor,
        )
        latest_settled_id = int(result.get("latest_settled_id") or 0)
        posts = [item for item in result.get("posts") or [] if isinstance(item, dict)]
        if first_scan and settings.telegram_backfill_limit <= 0:
            selected: list[dict[str, Any]] = []
            cursor = latest_settled_id
        elif first_scan:
            selected = posts[-settings.telegram_backfill_limit :]
            cursor = latest_settled_id
        else:
            selected = [
                item for item in posts if int(item.get("max_message_id") or 0) > cursor
            ]
            cursor = max(cursor, latest_settled_id)
        channel_state.update(
            {
                "cursor": cursor,
                "chat_id": result.get("chat_id"),
                "username": result.get("username"),
                "last_scan_at": utc_now_iso_z(),
                "error": "",
            }
        )

        records = self.state.setdefault("items", {})
        now = utc_now_iso_z()
        for post in selected:
            source_key = str(post.get("source_key") or "")
            url = str(post.get("url") or "")
            if not source_key or not url or source_key in records:
                continue
            already_published = catalog_has_source(
                settings, source_key=source_key, source_url=url
            )
            records[source_key] = {
                "source_key": source_key,
                "channel": channel,
                "post_id": post.get("post_id"),
                "url": url,
                "caption": str(post.get("caption") or "")[:300],
                "status": "done" if already_published else "pending",
                "attempts": 0,
                "next_retry_at": 0,
                "job_id": None,
                "error": "",
                "discovered_at": now,
                "updated_at": now,
            }

    async def _reconcile_processing(self, settings: GamesSettings) -> None:
        runner_status = get_games_runner().status()
        active_job_id = (
            str(runner_status.get("id") or "") if runner_status.get("running") else ""
        )
        for item in (self.state.get("items") or {}).values():
            if not isinstance(item, dict) or item.get("status") != "processing":
                continue
            job_id = str(item.get("job_id") or "")
            if job_id and job_id == active_job_id:
                continue
            job: dict[str, Any] = {}
            if job_id:
                try:
                    job = load_job(settings, job_id)
                except Exception:
                    job = {}
            if job.get("wp_id"):
                try:
                    job = finalize_published_job(settings, job)
                except Exception as exc:
                    self.state["last_error"] = f"已发布文章的本地记录恢复失败：{exc}"
                    logger.exception("恢复已发布游戏任务失败 job_id=%s", job_id)
                self._mark_done(item, job)
            elif job.get("stage") == "done" or catalog_has_source(
                settings,
                source_key=str(item.get("source_key") or ""),
                source_url=str(item.get("url") or ""),
            ):
                self._mark_done(item, job)
            elif job.get("stage") == "skipped":
                self._mark_skipped(item, job)
            elif job.get("stage") in {"failed", "cancelled"}:
                self._mark_failure(
                    settings,
                    item,
                    str(job.get("error") or job.get("message") or "任务失败"),
                )
            else:
                item["status"] = "pending"
                item["updated_at"] = utc_now_iso_z()

    def _next_item(self, settings: GamesSettings) -> dict[str, Any] | None:
        now = time.time()
        candidates = []
        for item in (self.state.get("items") or {}).values():
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            if status == "pending" or (
                status == "failed"
                and int(item.get("attempts") or 0) < settings.auto_retry_limit
                and float(item.get("next_retry_at") or 0) <= now
            ):
                candidates.append(item)
        if not candidates:
            return None
        candidates.sort(key=lambda item: str(item.get("discovered_at") or ""))
        return candidates[0]

    async def _process_item(
        self, settings: GamesSettings, item: dict[str, Any]
    ) -> bool:
        source_key = str(item.get("source_key") or "")
        url = str(item.get("url") or "")
        if catalog_has_source(settings, source_key=source_key, source_url=url):
            self._mark_done(item, {})
            return True
        item["status"] = "processing"
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["updated_at"] = utc_now_iso_z()
        self._active_source = source_key
        self._save()
        runner = get_games_runner()
        try:
            started = await runner.start_auto_publish(
                settings,
                url=url,
                source_key=source_key,
            )
            job_id = str(started.get("id") or "")
            item["job_id"] = job_id or None
            self._save()
            job = await runner.wait_for_job(job_id)
            if job.get("stage") == "skipped":
                self._mark_skipped(item, job)
                return True
            if job.get("stage") != "done" and not job.get("wp_id"):
                raise RuntimeError(
                    str(job.get("error") or job.get("message") or "自动发布失败")
                )
            if job.get("wp_id") and job.get("stage") != "done":
                try:
                    job = finalize_published_job(settings, job)
                except Exception:
                    logger.exception(
                        "自动发布文章已创建，但本地结果记录失败 job_id=%s", job_id
                    )
            self._mark_done(item, job)
            return True
        except asyncio.CancelledError:
            raise
        except GamesBusyError:
            item["status"] = "pending"
            item["attempts"] = max(int(item.get("attempts") or 1) - 1, 0)
            item["updated_at"] = utc_now_iso_z()
            return False
        except Exception as exc:
            self._mark_failure(settings, item, str(exc))
            logger.error("游戏自动发布失败 source=%s error=%s", source_key, exc)
            return True
        finally:
            self._active_source = ""
            self._save()

    def _mark_skipped(self, item: dict[str, Any], job: dict[str, Any]) -> None:
        item.update(
            {
                "status": "skipped",
                "job_id": job.get("id") or item.get("job_id"),
                "error": str(job.get("message") or "AI 已跳过")[:1000],
                "next_retry_at": 0,
                "updated_at": utc_now_iso_z(),
            }
        )

    def _mark_done(self, item: dict[str, Any], job: dict[str, Any]) -> None:
        now = utc_now_iso_z()
        item.update(
            {
                "status": "done",
                "job_id": job.get("id") or item.get("job_id"),
                "error": "",
                "next_retry_at": 0,
                "public_url": job.get("public_url") or item.get("public_url"),
                "updated_at": now,
            }
        )
        self.state["last_success_at"] = now
        self.state["last_error"] = None

    def _mark_failure(
        self, settings: GamesSettings, item: dict[str, Any], error: str
    ) -> None:
        attempts = int(item.get("attempts") or 0)
        exhausted = attempts >= settings.auto_retry_limit
        item.update(
            {
                "status": "exhausted" if exhausted else "failed",
                "error": error[:1000],
                "next_retry_at": 0
                if exhausted
                else time.time() + _retry_delay(attempts),
                "updated_at": utc_now_iso_z(),
            }
        )
        self.state["last_error"] = error[:1000]


_AUTOMATION: GamesAutomationService | None = None


def get_games_automation() -> GamesAutomationService:
    global _AUTOMATION
    if _AUTOMATION is None:
        _AUTOMATION = GamesAutomationService()
    return _AUTOMATION
