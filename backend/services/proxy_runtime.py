"""Apply saved proxy settings and rotate long-lived outbound clients."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from typing import Any, Mapping

logger = logging.getLogger("backend.proxy_runtime")

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_INITIAL_PROXY_ENV = {key: os.environ.get(key) for key in _PROXY_ENV_KEYS}
_ENV_LOCK = threading.RLock()
_RECONCILE_LOCK: asyncio.Lock | None = None
_RECONCILE_TASK: asyncio.Task[Any] | None = None
_RECONCILE_PENDING = False
_APPLIED_FINGERPRINT = ""


def _runtime_settings() -> dict[str, Any]:
    from backend.services.config import get_config_service

    return get_config_service().get_proxy_runtime_settings()


def _fingerprint(runtime: Mapping[str, Any]) -> str:
    material = "\0".join(
        (
            "1" if runtime.get("enabled") else "0",
            str(runtime.get("url") or ""),
            str(runtime.get("no_proxy") or ""),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def apply_proxy_environment(
    runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically apply the saved proxy to libraries that honor proxy env vars.

    The returned status is deliberately token-free and safe for logs/tests.
    Disabling the panel setting restores the environment inherited at process
    startup instead of destroying an operator-supplied deployment setting.
    """

    global _APPLIED_FINGERPRINT
    current = dict(runtime or _runtime_settings())
    enabled = bool(current.get("enabled"))
    proxy_url = str(current.get("url") or "").strip()
    no_proxy = str(current.get("no_proxy") or "").strip()
    if enabled and not proxy_url:
        # Validate before touching process state. Restoring/directing environment
        # variables here would turn a broken enabled proxy into a silent bypass.
        raise RuntimeError(
            "系统代理已启用，但代理地址不可用；未修改当前代理环境"
        )

    with _ENV_LOCK:
        if enabled:
            values = {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "ALL_PROXY": proxy_url,
                "NO_PROXY": no_proxy,
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "all_proxy": proxy_url,
                "no_proxy": no_proxy,
            }
            os.environ.update(values)
        else:
            for key, original in _INITIAL_PROXY_ENV.items():
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original
        _APPLIED_FINGERPRINT = _fingerprint(current)

    return {
        "enabled": enabled,
        "no_proxy": no_proxy if enabled else "",
        "fingerprint": _APPLIED_FINGERPRINT[:16],
    }


def applied_proxy_fingerprint() -> str:
    with _ENV_LOCK:
        return _APPLIED_FINGERPRINT


def restore_initial_proxy_environment() -> dict[str, Any]:
    """Restore proxy variables inherited before the application started."""

    return apply_proxy_environment(
        {"enabled": False, "url": None, "no_proxy": ""}
    )


def _get_reconcile_lock() -> asyncio.Lock:
    global _RECONCILE_LOCK
    if _RECONCILE_LOCK is None:
        _RECONCILE_LOCK = asyncio.Lock()
    return _RECONCILE_LOCK


async def reconcile_proxy_runtime() -> dict[str, Any]:
    """Rotate long-lived clients after the proxy environment has changed."""

    async with _get_reconcile_lock():
        status = apply_proxy_environment()
        restarted: list[str] = []
        failures: list[str] = []

        # Stop Telegram-backed workers before restarting either one. They can
        # share a cached account client, so this ordering lets its refcount reach
        # zero and guarantees the replacement is created with the new proxy.
        manga_runtime = None
        keyword_service = None
        try:
            from backend.services.keyword_monitor import get_keyword_monitor_service

            keyword_service = get_keyword_monitor_service()
            await keyword_service.stop()
        except Exception:
            failures.append("keyword_monitor_stop")
            logger.exception("代理切换时停止关键词监听失败")

        try:
            from backend.services.manga.runtime import get_manga_runtime

            manga_runtime = get_manga_runtime()
            await manga_runtime.stop_ehentai()
            await manga_runtime.stop()
        except Exception:
            failures.append("manga_stop")
            logger.exception("代理切换时停止漫画监听失败")

        if keyword_service is not None:
            try:
                await keyword_service.restart_from_tasks()
                restarted.append("keyword_monitor")
            except Exception:
                failures.append("keyword_monitor_start")
                logger.exception("代理切换后重启关键词监听失败")

        if manga_runtime is not None:
            try:
                await manga_runtime.start_if_enabled()
                restarted.append("manga")
            except Exception:
                failures.append("manga_start")
                logger.exception("代理切换后重启漫画监听失败")

        try:
            from backend.services.telegram_bot import get_telegram_bot_runtime

            get_telegram_bot_runtime().request_reconcile()
            restarted.append("telegram_bot")
        except Exception:
            failures.append("telegram_bot")
            logger.exception("代理切换后唤醒 Telegram Bot 失败")

        result = {
            **status,
            "restarted": restarted,
            "failures": failures,
        }
        logger.info(
            "系统代理运行时已更新 enabled=%s restarted=%s failures=%s",
            status["enabled"],
            ",".join(restarted) or "none",
            ",".join(failures) or "none",
        )
        return result


def schedule_proxy_runtime_reconcile() -> asyncio.Task[Any] | None:
    """Schedule a coalesced, non-blocking rotation from an async request."""

    global _RECONCILE_PENDING, _RECONCILE_TASK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = _RECONCILE_TASK
    if task is not None and not task.done() and task.get_loop() is loop:
        _RECONCILE_PENDING = True
        return task

    _RECONCILE_PENDING = True

    async def _runner() -> None:
        global _RECONCILE_PENDING, _RECONCILE_TASK
        try:
            while _RECONCILE_PENDING:
                _RECONCILE_PENDING = False
                await reconcile_proxy_runtime()
        finally:
            _RECONCILE_TASK = None

    _RECONCILE_TASK = loop.create_task(
        _runner(), name="system-proxy-runtime-reconcile"
    )
    return _RECONCILE_TASK


async def stop_proxy_runtime_reconcile() -> None:
    """Cancel an outstanding rotation before the application shuts down."""

    global _RECONCILE_PENDING, _RECONCILE_TASK
    _RECONCILE_PENDING = False
    task = _RECONCILE_TASK
    _RECONCILE_TASK = None
    if task is None or task.done() or task is asyncio.current_task():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
