"""Telegram Bot command handling for the restricted control plane."""

from __future__ import annotations

import logging
import secrets
import shlex
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from backend.services.config import get_config_service
from backend.services.control_plane import ControlPlaneError, get_control_plane

from .api import TelegramBotApi
from .auth import allowed_user_ids_from_settings

logger = logging.getLogger("backend.telegram_bot.commands")


@dataclass(frozen=True)
class _PendingRun:
    user_id: int
    account_name: str
    task_name: str
    stage: str
    expires_at: float


def mini_app_url_from_settings(settings: Mapping[str, Any]) -> str:
    raw = str(settings.get("telegram_bot_mini_app_url") or "").strip()
    if not raw or len(raw) > 2048:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return ""
    return raw


def _private_operator(update_part: Mapping[str, Any]) -> tuple[int, int] | None:
    sender = update_part.get("from")
    message = update_part.get("message") if "message" in update_part else update_part
    chat = message.get("chat") if isinstance(message, Mapping) else None
    if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
        return None
    try:
        user_id = int(sender.get("id"))
        chat_id = int(chat.get("id"))
    except (TypeError, ValueError):
        return None
    if chat.get("type") != "private" or user_id <= 0 or chat_id != user_id:
        return None
    return user_id, chat_id


def _concrete_task_accounts(task: Mapping[str, Any]) -> list[str]:
    """Return stable, deduplicated real accounts for a task definition."""
    values = list(task.get("account_names") or [])
    primary = str(task.get("account_name") or "").strip()
    if primary and primary != "*":
        values.insert(0, primary)
    accounts: list[str] = []
    for value in values:
        account = str(value or "").strip()
        if account and account != "*" and account not in accounts:
            accounts.append(account)
    return accounts


class BotCommandHandler:
    _PENDING_SECONDS = 5 * 60
    _PENDING_LIMIT = 256

    def __init__(self, api: TelegramBotApi) -> None:
        self.api = api
        self._pending: dict[str, _PendingRun] = {}

    @staticmethod
    def _settings() -> dict[str, Any]:
        return get_config_service().get_global_settings()

    def _authorized(self, user_id: int) -> bool:
        settings = self._settings()
        return bool(settings.get("telegram_bot_control_enabled")) and (
            user_id in allowed_user_ids_from_settings(settings)
        )

    def _prune_pending(self) -> None:
        now = time.monotonic()
        for token, entry in list(self._pending.items()):
            if entry.expires_at <= now:
                self._pending.pop(token, None)
        if len(self._pending) <= self._PENDING_LIMIT:
            return
        ordered = sorted(self._pending.items(), key=lambda item: item[1].expires_at)
        for token, _entry in ordered[: len(ordered) - self._PENDING_LIMIT]:
            self._pending.pop(token, None)

    def _new_pending(
        self, *, user_id: int, account_name: str, task_name: str, stage: str
    ) -> str:
        self._prune_pending()
        token = secrets.token_urlsafe(9)
        self._pending[token] = _PendingRun(
            user_id=user_id,
            account_name=account_name,
            task_name=task_name,
            stage=stage,
            expires_at=time.monotonic() + self._PENDING_SECONDS,
        )
        return token

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> None:
        await self.api.send_message(
            chat_id=chat_id,
            text=str(text)[:3900],
            reply_markup=reply_markup,
        )

    async def handle_update(self, update: Mapping[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            await self._handle_callback(update, callback)
            return
        message = update.get("message")
        if isinstance(message, Mapping):
            await self._handle_message(update, message)

    async def _handle_message(
        self, update: Mapping[str, Any], message: Mapping[str, Any]
    ) -> None:
        del update
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
            return
        try:
            user_id = int(sender.get("id"))
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if chat.get("type") != "private" or chat_id != user_id:
            await self._send(chat_id, "请在与 Bot 的私聊中使用控制命令。")
            return

        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        command = (parts[0].split("@", 1)[0] if parts else "").lower()
        args = parts[1:]
        if command == "/id":
            await self._send(chat_id, f"你的 Telegram User ID：{user_id}")
            return
        try:
            settings = self._settings()
        except Exception:
            logger.exception("读取 Telegram Bot 控制配置失败")
            await self._send(chat_id, "控制配置暂时不可用，请稍后重试。")
            return
        if not settings.get("telegram_bot_control_enabled"):
            await self._send(chat_id, "Telegram Bot 控制功能尚未启用。")
            return
        if user_id not in allowed_user_ids_from_settings(settings):
            await self._send(
                chat_id,
                f"当前账号未获授权。你的 Telegram User ID：{user_id}",
            )
            return

        try:
            if command in {"/start", "/help"}:
                await self._send_help(chat_id)
            elif command == "/status":
                await self._send_status(chat_id)
            elif command == "/tasks":
                await self._send_tasks(chat_id, user_id)
            elif command == "/alerts":
                await self._send_alerts(chat_id)
            elif command == "/run":
                await self._prepare_run_command(chat_id, user_id, args)
            else:
                await self._send_help(chat_id)
        except ControlPlaneError as exc:
            await self._send(chat_id, (str(exc) or "操作失败，请稍后重试。")[:500])
        except Exception:
            logger.exception("处理 Telegram Bot 命令失败 command=%s", command)
            await self._send(chat_id, "操作失败，请稍后重试。")

    async def _send_help(self, chat_id: int) -> None:
        settings = self._settings()
        buttons: list[list[dict[str, Any]]] = []
        mini_url = mini_app_url_from_settings(settings)
        if mini_url:
            buttons.append(
                [{"text": "打开 Mini App", "web_app": {"url": mini_url}}]
            )
        buttons.append(
            [
                {"text": "系统状态", "callback_data": "show:status"},
                {"text": "任务列表", "callback_data": "show:tasks"},
            ]
        )
        await self._send(
            chat_id,
            "TG-SignPulse 控制中心\n"
            "/status 系统状态\n/tasks 签到任务\n"
            "/run <任务名> [账号] 运行任务\n/alerts 最近告警",
            reply_markup={"inline_keyboard": buttons},
        )

    async def _send_status(self, chat_id: int) -> None:
        data = get_control_plane().bootstrap()
        system = data["system"]
        await self._send(
            chat_id,
            "系统状态：{status}\n账号：{connected}/{accounts}\n"
            "任务：{enabled}/{tasks}\n运行中：{active}".format(
                status=system["status"],
                connected=system["accounts_connected"],
                accounts=system["accounts_total"],
                enabled=system["tasks_enabled"],
                tasks=system["tasks_total"],
                active=system["active_runs_total"],
            ),
        )

    async def _send_tasks(self, chat_id: int, user_id: int) -> None:
        tasks = get_control_plane().list_tasks()
        if not tasks:
            await self._send(chat_id, "当前没有签到任务。")
            return
        lines = ["签到任务："]
        buttons: list[list[dict[str, str]]] = []
        for item in tasks[:10]:
            accounts = _concrete_task_accounts(item)
            task = str(item.get("name") or "")
            marker = "启用" if item.get("enabled") else "停用"
            account_label = ", ".join(accounts) if accounts else "-"
            lines.append(f"- {task} · {account_label} · {marker}")
            if task and item.get("enabled"):
                for account in accounts:
                    token = self._new_pending(
                        user_id=user_id,
                        account_name=account,
                        task_name=task,
                        stage="prepare",
                    )
                    buttons.append(
                        [
                            {
                                "text": f"运行 {task} / {account}"[:60],
                                "callback_data": f"run:prepare:{token}",
                            }
                        ]
                    )
        if len(tasks) > 10:
            lines.append(f"另有 {len(tasks) - 10} 个任务，请在 Mini App 中查看。")
        await self._send(
            chat_id,
            "\n".join(lines),
            reply_markup={"inline_keyboard": buttons} if buttons else None,
        )

    async def _send_alerts(self, chat_id: int) -> None:
        alerts = get_control_plane().recent_alerts(limit=5)["items"]
        if not alerts:
            await self._send(chat_id, "暂无告警记录。")
            return
        lines = ["最近告警："]
        for item in reversed(alerts):
            lines.append(
                f"- [{item['status'] or '-'}] {item['title'] or item['rule_id']}"
            )
        await self._send(chat_id, "\n".join(lines))

    async def _prepare_run_command(
        self, chat_id: int, user_id: int, args: list[str]
    ) -> None:
        if not args:
            await self._send_tasks(chat_id, user_id)
            return
        task_name = args[0]
        account_name = args[1] if len(args) > 1 else ""
        tasks = [item for item in get_control_plane().list_tasks() if item["name"] == task_name]
        if not tasks:
            await self._send(chat_id, "找不到这个任务。")
            return
        if account_name:
            task = next(
                (
                    item
                    for item in tasks
                    if account_name in _concrete_task_accounts(item)
                ),
                None,
            )
        else:
            enabled_options: list[tuple[Mapping[str, Any], str]] = []
            for candidate in tasks:
                if not candidate.get("enabled"):
                    continue
                enabled_options.extend(
                    (candidate, account)
                    for account in _concrete_task_accounts(candidate)
                )
            if len(enabled_options) > 1:
                buttons: list[list[dict[str, str]]] = []
                seen_accounts: set[str] = set()
                for _candidate, candidate_account in enabled_options:
                    if candidate_account in seen_accounts:
                        continue
                    seen_accounts.add(candidate_account)
                    token = self._new_pending(
                        user_id=user_id,
                        account_name=candidate_account,
                        task_name=task_name,
                        stage="prepare",
                    )
                    buttons.append(
                        [
                            {
                                "text": candidate_account[:60],
                                "callback_data": f"run:prepare:{token}",
                            }
                        ]
                    )
                await self._send(
                    chat_id,
                    f"任务 {task_name} 包含多个账号，请选择：",
                    reply_markup={"inline_keyboard": buttons},
                )
                return
            if enabled_options:
                task, account_name = enabled_options[0]
            else:
                task = tasks[0]
                accounts = _concrete_task_accounts(task)
                account_name = accounts[0] if len(accounts) == 1 else ""
        if not task or not account_name or account_name == "*":
            await self._send(chat_id, "无法确定任务账号。")
            return
        if not task.get("enabled"):
            await self._send(chat_id, "该任务已停用，不能手动运行。")
            return
        token = self._new_pending(
            user_id=user_id,
            account_name=account_name,
            task_name=task_name,
            stage="confirm",
        )
        await self._send(
            chat_id,
            f"确认运行 {account_name}/{task_name}？",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "确认运行",
                            "callback_data": f"run:confirm:{token}",
                        }
                    ]
                ]
            },
        )

    async def _handle_callback(
        self, update: Mapping[str, Any], callback: Mapping[str, Any]
    ) -> None:
        callback_id = str(callback.get("id") or "")
        private = _private_operator(callback)
        data = str(callback.get("data") or "")
        answered = False
        try:
            if private is None:
                await self.api.answer_callback_query(
                    callback_id, text="请在私聊中操作", show_alert=True
                )
                answered = True
                return
            user_id, chat_id = private
            if not self._authorized(user_id):
                await self.api.answer_callback_query(
                    callback_id, text="当前账号未获授权", show_alert=True
                )
                answered = True
                return
            if data == "show:status":
                await self.api.answer_callback_query(callback_id)
                answered = True
                await self._send_status(chat_id)
                return
            if data == "show:tasks":
                await self.api.answer_callback_query(callback_id)
                answered = True
                await self._send_tasks(chat_id, user_id)
                return

            pieces = data.split(":", 2)
            self._prune_pending()
            if len(pieces) != 3 or pieces[0] != "run":
                await self.api.answer_callback_query(
                    callback_id, text="未知操作", show_alert=True
                )
                answered = True
                return
            stage, token = pieces[1], pieces[2]
            pending = self._pending.get(token)
            if (
                pending is None
                or pending.user_id != user_id
                or pending.stage != stage
                or pending.expires_at <= time.monotonic()
            ):
                self._pending.pop(token, None)
                await self.api.answer_callback_query(
                    callback_id, text="操作已过期", show_alert=True
                )
                answered = True
                return
            self._pending.pop(token, None)
            await self.api.answer_callback_query(callback_id, text="处理中")
            answered = True
            if stage == "prepare":
                confirm = self._new_pending(
                    user_id=user_id,
                    account_name=pending.account_name,
                    task_name=pending.task_name,
                    stage="confirm",
                )
                await self._send(
                    chat_id,
                    f"确认运行 {pending.account_name}/{pending.task_name}？",
                    reply_markup={
                        "inline_keyboard": [
                            [
                                {
                                    "text": "确认运行",
                                    "callback_data": f"run:confirm:{confirm}",
                                }
                            ]
                        ]
                    },
                )
                return
            result = await get_control_plane().start_task_run(
                pending.account_name,
                pending.task_name,
                request_id=f"bot-{update.get('update_id', '')}-{token}",
                actor_id=f"telegram-bot:{user_id}",
            )
            await self._send(
                chat_id,
                f"任务已受理：{pending.account_name}/{pending.task_name}\n"
                f"状态：{result['state']}\nRun ID：{result['run_id']}",
            )
        except ControlPlaneError as exc:
            message = str(exc) or "任务操作失败"
            if private is not None:
                await self._send(private[1], message[:500])
        except Exception:
            logger.exception("处理 Telegram Bot callback 失败")
            if private is not None:
                try:
                    await self._send(private[1], "操作失败，请稍后重试。")
                except Exception:
                    logger.debug("发送 callback 失败提示失败", exc_info=True)
        finally:
            if callback_id and not answered:
                try:
                    await self.api.answer_callback_query(
                        callback_id, text="操作失败", show_alert=True
                    )
                except Exception:
                    logger.debug("answerCallbackQuery 失败", exc_info=True)
