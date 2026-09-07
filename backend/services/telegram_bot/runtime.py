"""Primary-instance Telegram Bot polling runtime.

Mini App and slash commands use long polling. A leftover webhook blocks
getUpdates and setMyCommands, so the scheduler-primary owner deletes it and
takes over. Secondary replicas stay in standby and never call Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from backend.core.config import get_settings
from backend.scheduler.instance_lock import has_scheduler_lock
from backend.services.config import get_config_service
from backend.utils.atomic_io import read_json_safe, write_json_atomic

from .api import TelegramBotApi, TelegramBotApiError, bot_token_from_settings
from .commands import BotCommandHandler, mini_app_url_from_settings
from .ids import allowed_user_ids_from_settings

logger = logging.getLogger("backend.telegram_bot.runtime")

_SUPERVISOR_INTERVAL_SECONDS = 5.0
_CONFLICT_RECHECK_SECONDS = 60.0
_STATE_FILE = ".telegram_bot_state.json"
_STATE_VERSION = 1
_BOT_COMMANDS = (
    {"command": "start", "description": "打开控制中心"},
    {"command": "help", "description": "查看可用命令"},
    {"command": "status", "description": "查看系统状态"},
    {"command": "tasks", "description": "查看签到任务"},
    {"command": "run", "description": "运行签到任务"},
    {"command": "alerts", "description": "查看最近告警"},
    {"command": "id", "description": "查看 Telegram User ID"},
)
_COMMAND_LANGUAGE_CODES = ("zh",)


def _command_scopes(settings: dict[str, Any]) -> list[dict[str, Any] | None]:
    scopes: list[dict[str, Any] | None] = [None, {"type": "all_private_chats"}]
    for user_id in sorted(allowed_user_ids_from_settings(settings)):
        scopes.append({"type": "chat", "chat_id": user_id})
    return scopes


def _safe_bot_identity(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    try:
        bot_id = int(raw.get("id"))
    except (TypeError, ValueError):
        bot_id = 0
    return {
        "id": bot_id if bot_id > 0 else None,
        "username": str(raw.get("username") or "")[:64],
        "first_name": str(raw.get("first_name") or "")[:64],
    }


def _runtime_fingerprint(settings: dict[str, Any], token: str) -> str:
    proxy_url = str(settings.get("global_proxy") or "")
    material = {
        # Outbound notifications do not need getUpdates and must not make this
        # process take over a bot that is otherwise managed by a webhook.
        "enabled": bool(settings.get("telegram_bot_control_enabled")),
        "token": hashlib.sha256(token.encode("utf-8")).hexdigest() if token else "",
        "mini_app_url": mini_app_url_from_settings(settings),
        "allowed_user_ids": settings.get("telegram_bot_allowed_user_ids") or [],
        "proxy_enabled": bool(settings.get("proxy_enabled")),
        "proxy_no_proxy": str(settings.get("proxy_no_proxy") or ""),
        # Hash-only: credentials affect rotation but never enter state/log output.
        "proxy": hashlib.sha256(proxy_url.encode("utf-8")).hexdigest()
        if proxy_url
        else "",
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bot_token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


class TelegramBotRuntime:
    """Own exactly one long-polling worker on the scheduler-primary process."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._supervisor_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._api: TelegramBotApi | None = None
        self._handler: BotCommandHandler | None = None
        self._fingerprint = ""
        self._stopping = False
        self._last_probe_at = 0.0
        self._last_update_id: int | None = None
        self._offset_bot_id: int | None = None
        self._offset_token_fingerprint = ""
        self._managed_menu_user_ids: set[int] = set()
        self._status: dict[str, Any] = {
            "state": "stopped",
            "configured": False,
            "control_enabled": False,
            "primary": False,
            "running": False,
            "webhook_conflict": False,
            "bot": {"id": None, "username": "", "first_name": ""},
            "last_error": "",
            "last_update_id": None,
        }

    def status(self) -> dict[str, Any]:
        """Return a token-free snapshot suitable for diagnostics and APIs."""
        result = dict(self._status)
        result["bot"] = dict(self._status.get("bot") or {})
        return result

    async def start(self) -> dict[str, Any]:
        """Start the non-blocking supervisor; safe to call repeatedly."""
        task = self._supervisor_task
        if task is not None and not task.done():
            self._wake.set()
            return self.status()
        self._stopping = False
        self._wake = asyncio.Event()
        self._supervisor_task = asyncio.create_task(
            self._supervise(), name="telegram-bot-supervisor"
        )
        return self.status()

    async def reconcile(self) -> dict[str, Any]:
        """Immediately reconcile polling ownership with settings and primary lock."""
        async with self._lock:
            await self._reconcile_locked()
        return self.status()

    def request_reconcile(self) -> None:
        """Wake a running supervisor after settings are saved."""
        self._wake.set()

    async def stop(self) -> dict[str, Any]:
        """Stop supervisor and polling before the scheduler lock is released."""
        self._stopping = True
        self._wake.set()
        supervisor = self._supervisor_task
        if (
            supervisor is not None
            and supervisor is not asyncio.current_task()
            and not supervisor.done()
        ):
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        async with self._lock:
            await self._stop_worker_locked()
            self._status.update(
                state="stopped",
                running=False,
                webhook_conflict=False,
                last_error="",
            )
        self._supervisor_task = None
        return self.status()

    async def _supervise(self) -> None:
        try:
            while not self._stopping:
                # Clear the event before reconciliation so a settings change that
                # arrives during network setup remains set for the next pass.
                self._wake.clear()
                try:
                    await self.reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._status.update(
                        state="error", running=False, last_error="runtime_error"
                    )
                    logger.exception("Telegram Bot runtime reconcile failed")
                if self._stopping:
                    break
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=_SUPERVISOR_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._supervisor_task is asyncio.current_task():
                self._supervisor_task = None

    async def _reconcile_locked(self) -> None:
        try:
            settings = get_config_service().get_global_settings()
        except Exception:
            await self._stop_worker_locked()
            self._status.update(
                state="error",
                configured=False,
                running=False,
                last_error="settings_unavailable",
            )
            return
        token = bot_token_from_settings(settings)
        control_enabled = bool(settings.get("telegram_bot_control_enabled"))
        primary = has_scheduler_lock()
        configured = bool(token)
        fingerprint = _runtime_fingerprint(settings, token)
        self._status.update(
            configured=configured,
            control_enabled=control_enabled,
            primary=primary,
        )

        if not control_enabled or not configured or not primary or self._stopping:
            if (
                not control_enabled
                and configured
                and primary
                and not self._stopping
            ):
                cleanup_api = self._api
                temporary_cleanup_api = False
                if cleanup_api is None and self._load_managed_menu_state(token):
                    cleanup_api = TelegramBotApi(token)
                    temporary_cleanup_api = True
                if cleanup_api is not None:
                    try:
                        await self._reset_managed_menus(cleanup_api)
                    finally:
                        if temporary_cleanup_api:
                            with contextlib.suppress(Exception):
                                await cleanup_api.close()
            await self._stop_worker_locked()
            if self._stopping:
                state = "stopped"
            elif not control_enabled:
                state = "disabled"
            elif not configured:
                state = "unconfigured"
            else:
                state = "standby"
            self._fingerprint = fingerprint
            self._status.update(
                state=state,
                running=False,
                webhook_conflict=False,
                last_error="",
                bot={"id": None, "username": "", "first_name": ""},
            )
            return

        poll = self._poll_task
        if (
            fingerprint == self._fingerprint
            and poll is not None
            and not poll.done()
        ):
            return
        now = time.monotonic()
        if (
            fingerprint == self._fingerprint
            and self._status.get("state")
            in {"error", "webhook_conflict", "polling_conflict"}
            and now - self._last_probe_at < _CONFLICT_RECHECK_SECONDS
        ):
            return

        await self._stop_worker_locked()
        api: TelegramBotApi | None = None
        try:
            api = TelegramBotApi(token)
            webhook = await api.get_webhook_info()
            self._last_probe_at = now
            if str(webhook.get("url") or "").strip():
                try:
                    await api.delete_webhook(drop_pending_updates=True)
                except TelegramBotApiError as exc:
                    await api.close()
                    self._fingerprint = fingerprint
                    self._status.update(
                        state="webhook_conflict",
                        running=False,
                        webhook_conflict=True,
                        last_error=(
                            f"webhook_delete_failed_{exc.status_code or 'error'}"[:128]
                        ),
                        bot={"id": None, "username": "", "first_name": ""},
                    )
                    return
                leftover = await api.get_webhook_info()
                if str(leftover.get("url") or "").strip():
                    await api.close()
                    self._fingerprint = fingerprint
                    self._status.update(
                        state="webhook_conflict",
                        running=False,
                        webhook_conflict=True,
                        last_error="webhook_configured",
                        bot={"id": None, "username": "", "first_name": ""},
                    )
                    return
                logger.info("Telegram Bot 已清除旧 webhook，改由本机轮询接管")
            identity = await api.get_me()
            if identity.get("is_bot") is not True:
                raise TelegramBotApiError("getMe", "configured identity is not a bot")
            safe_identity = _safe_bot_identity(identity)
            bot_id = safe_identity["id"]
            if not isinstance(bot_id, int):
                raise TelegramBotApiError("getMe", "configured bot has no valid id")
            self._load_offset(bot_id=bot_id, token=token)
            await self._register_slash_menu(api, settings)
            self._api = api
            self._handler = BotCommandHandler(api)
            self._fingerprint = fingerprint
            self._status.update(
                state="running",
                running=True,
                webhook_conflict=False,
                last_error="",
                bot=safe_identity,
                last_update_id=self._last_update_id,
            )
            self._poll_task = asyncio.create_task(
                self._poll(api, self._handler), name="telegram-bot-polling"
            )
            logger.info(
                "Telegram Bot polling started bot_id=%s username=%s operators=%d mini_app=%s",
                bot_id,
                safe_identity["username"] or "-",
                len(allowed_user_ids_from_settings(settings)),
                "configured" if mini_app_url_from_settings(settings) else "disabled",
            )
        except asyncio.CancelledError:
            if api is not None:
                await api.close()
            raise
        except TelegramBotApiError as exc:
            if api is not None:
                await api.close()
            self._fingerprint = fingerprint
            self._last_probe_at = now
            self._status.update(
                state="error",
                running=False,
                webhook_conflict=False,
                last_error=f"bot_api_{exc.method}_{exc.status_code or 'error'}"[:128],
                bot={"id": None, "username": "", "first_name": ""},
            )
        except Exception:
            if api is not None:
                await api.close()
            self._fingerprint = fingerprint
            self._last_probe_at = now
            self._status.update(
                state="error",
                running=False,
                webhook_conflict=False,
                last_error="bot_initialization_failed",
                bot={"id": None, "username": "", "first_name": ""},
            )
            logger.exception("Telegram Bot initialization failed")

    async def _register_slash_menu(
        self, api: TelegramBotApi, settings: dict[str, Any]
    ) -> None:
        """Publish commands and configure operator-specific menu buttons.

        Unknown users retain the command menu so they can discover ``/id``.
        Authorized operators get a direct Mini App button when configured;
        slash commands remain registered and continue to work.
        """
        for language_code in _COMMAND_LANGUAGE_CODES:
            for scope in (None, {"type": "all_private_chats"}):
                try:
                    await api.delete_my_commands(
                        scope=scope, language_code=language_code
                    )
                except TelegramBotApiError:
                    logger.warning("Telegram Bot language command cleanup failed")
        for scope in _command_scopes(settings):
            try:
                await api.set_my_commands(_BOT_COMMANDS, scope=scope)
            except TelegramBotApiError:
                logger.warning("Telegram Bot commands registration failed")
        try:
            await api.set_commands_menu_button()
        except TelegramBotApiError:
            logger.warning("Telegram Bot default command menu registration failed")
        mini_app_url = mini_app_url_from_settings(settings)
        current_user_ids = set(allowed_user_ids_from_settings(settings))
        managed_user_ids = set(self._managed_menu_user_ids)
        for user_id in sorted(managed_user_ids - current_user_ids):
            try:
                await api.set_default_chat_menu_button(chat_id=user_id)
                managed_user_ids.discard(user_id)
            except TelegramBotApiError:
                logger.warning(
                    "Telegram Bot stale operator menu cleanup failed user_id=%s",
                    user_id,
                )
        for user_id in sorted(current_user_ids):
            try:
                if mini_app_url:
                    await api.set_chat_menu_button(
                        url=mini_app_url,
                        text="打开 Mini App",
                        chat_id=user_id,
                    )
                else:
                    await api.set_commands_menu_button(chat_id=user_id)
                managed_user_ids.add(user_id)
            except TelegramBotApiError:
                logger.warning(
                    "Telegram Bot operator menu registration failed user_id=%s",
                    user_id,
                )
        self._managed_menu_user_ids = managed_user_ids
        self._persist_offset(self._last_update_id)

    async def _reset_managed_menus(self, api: TelegramBotApi) -> None:
        """Remove per-chat buttons when command control is explicitly disabled."""
        remaining = set(self._managed_menu_user_ids)
        for user_id in sorted(self._managed_menu_user_ids):
            try:
                await api.set_default_chat_menu_button(chat_id=user_id)
                remaining.discard(user_id)
            except TelegramBotApiError:
                logger.warning(
                    "Telegram Bot operator menu reset failed user_id=%s", user_id
                )
        self._managed_menu_user_ids = remaining
        self._persist_offset(self._last_update_id)

    async def _stop_worker_locked(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        api = self._api
        self._api = None
        self._handler = None
        if api is not None:
            with contextlib.suppress(Exception):
                await api.close()

    def _state_path(self) -> Path:
        return Path(get_settings().resolve_base_dir()) / _STATE_FILE

    def _load_offset(self, *, bot_id: int, token: str) -> None:
        token_fingerprint = _bot_token_fingerprint(token)
        self._offset_bot_id = bot_id
        self._offset_token_fingerprint = token_fingerprint
        raw = read_json_safe(self._state_path(), default={})
        stored_bot_id = None
        stored_fingerprint = ""
        if isinstance(raw, dict):
            try:
                stored_bot_id = int(raw.get("bot_id"))
            except (TypeError, ValueError):
                stored_bot_id = None
            stored_fingerprint = str(raw.get("token_fingerprint") or "")
        if (
            stored_bot_id != bot_id
            or stored_fingerprint != token_fingerprint
        ):
            self._last_update_id = None
            self._managed_menu_user_ids = set()
            self._persist_offset(None)
            return

        value = raw.get("last_update_id")
        try:
            update_id = int(value)
        except (TypeError, ValueError):
            update_id = -1
        self._last_update_id = update_id if update_id >= 0 else None
        self._managed_menu_user_ids = self._parse_managed_menu_user_ids(
            raw.get("managed_menu_user_ids")
        )

    @staticmethod
    def _parse_managed_menu_user_ids(value: Any) -> set[int]:
        raw_managed_user_ids = value
        if not isinstance(raw_managed_user_ids, list):
            raw_managed_user_ids = []
        managed_user_ids: set[int] = set()
        for value in raw_managed_user_ids:
            try:
                user_id = int(value)
            except (TypeError, ValueError):
                continue
            if 0 < user_id < 2**63:
                managed_user_ids.add(user_id)
        return managed_user_ids

    def _load_managed_menu_state(self, token: str) -> bool:
        """Load only same-token menu state for cleanup without starting polling."""
        raw = read_json_safe(self._state_path(), default={})
        if not isinstance(raw, dict):
            return False
        token_fingerprint = _bot_token_fingerprint(token)
        if str(raw.get("token_fingerprint") or "") != token_fingerprint:
            return False
        try:
            bot_id = int(raw.get("bot_id"))
        except (TypeError, ValueError):
            return False
        if bot_id <= 0:
            return False
        managed_user_ids = self._parse_managed_menu_user_ids(
            raw.get("managed_menu_user_ids")
        )
        if not managed_user_ids:
            return False
        try:
            update_id = int(raw.get("last_update_id"))
        except (TypeError, ValueError):
            update_id = -1
        self._offset_bot_id = bot_id
        self._offset_token_fingerprint = token_fingerprint
        self._last_update_id = update_id if update_id >= 0 else None
        self._managed_menu_user_ids = managed_user_ids
        return True

    def _persist_offset(self, update_id: int | None) -> None:
        if self._offset_bot_id is None or not self._offset_token_fingerprint:
            return
        try:
            write_json_atomic(
                self._state_path(),
                {
                    "version": _STATE_VERSION,
                    "bot_id": self._offset_bot_id,
                    "token_fingerprint": self._offset_token_fingerprint,
                    "last_update_id": update_id,
                    "managed_menu_user_ids": sorted(self._managed_menu_user_ids),
                },
            )
        except (OSError, TypeError, ValueError):
            logger.warning("Telegram Bot update offset persistence failed")

    async def _poll(self, api: TelegramBotApi, handler: BotCommandHandler) -> None:
        backoff = 1.0
        try:
            while not self._stopping and self._api is api:
                try:
                    updates = await api.get_updates(
                        offset=self._last_update_id, timeout=25
                    )
                    backoff = 1.0
                    self._status["last_error"] = ""
                    for update in updates:
                        try:
                            update_id = int(update.get("update_id"))
                        except (TypeError, ValueError):
                            continue
                        if (
                            self._last_update_id is not None
                            and update_id < self._last_update_id
                        ):
                            continue
                        self._last_update_id = update_id + 1
                        self._status["last_update_id"] = self._last_update_id
                        self._persist_offset(self._last_update_id)
                        try:
                            await handler.handle_update(update)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.error("Telegram Bot update handling failed")
                except asyncio.CancelledError:
                    raise
                except TelegramBotApiError as exc:
                    if exc.status_code == 409:
                        self._last_probe_at = time.monotonic()
                        self._status.update(
                            state="polling_conflict",
                            running=False,
                            webhook_conflict=False,
                            last_error="another_get_updates_consumer",
                        )
                        logger.warning(
                            "Telegram Bot polling conflict: another getUpdates consumer is active"
                        )
                        return
                    self._status["last_error"] = (
                        f"poll_{exc.status_code or 'error'}"[:128]
                    )
                    delay = float(exc.retry_after or backoff)
                    backoff = min(backoff * 2.0, 30.0)
                    await asyncio.sleep(max(1.0, min(delay, 60.0)))
                except Exception:
                    self._status["last_error"] = "poll_failed"
                    logger.error("Telegram Bot polling failed")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
        finally:
            if self._api is api and self._status.get("state") == "running":
                self._status.update(state="stopped", running=False)


_runtime: TelegramBotRuntime | None = None


def get_telegram_bot_runtime() -> TelegramBotRuntime:
    global _runtime
    if _runtime is None:
        _runtime = TelegramBotRuntime()
    return _runtime


async def start_telegram_bot_runtime() -> dict[str, Any]:
    return await get_telegram_bot_runtime().start()


async def stop_telegram_bot_runtime() -> dict[str, Any]:
    return await get_telegram_bot_runtime().stop()
