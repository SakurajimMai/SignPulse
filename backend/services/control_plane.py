"""Restricted control-plane facade shared by Telegram Bot and Mini App."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


class ControlPlaneError(ValueError):
    pass


class ControlTaskNotFoundError(ControlPlaneError):
    pass


class ControlTaskAccountError(ControlPlaneError):
    pass


class ControlTaskDisabledError(ControlPlaneError):
    pass


class IdempotencyConflictError(ControlPlaneError):
    pass


@dataclass
class _IdempotentRun:
    target: tuple[str, str]
    result: dict[str, Any]
    expires_at: float


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _mini_active_run(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "run_id": _text(value.get("run_id"), 128),
        "state": _text(value.get("state"), 32) or "idle",
        "phase": _text(value.get("phase"), 64) or None,
        "phase_detail": _text(value.get("phase_detail"), 500),
        "account_name": _text(value.get("account_name"), 128),
        "task_name": _text(value.get("task_name"), 128),
        "started_at": _text(value.get("started_at"), 64) or None,
        "wait_seconds": value.get("wait_seconds"),
    }


def _mini_run_status(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "run_id": _text(raw.get("run_id"), 128),
        "state": _text(raw.get("state"), 32) or "idle",
        "success": raw.get("success") if isinstance(raw.get("success"), bool) else None,
        "error": _text(raw.get("error"), 1000),
        "output": _text(raw.get("output"), 2000),
        "started_at": _text(raw.get("started_at"), 64) or None,
        "finished_at": _text(raw.get("finished_at"), 64) or None,
        "phase": _text(raw.get("phase"), 64) or None,
        "phase_detail": _text(raw.get("phase_detail"), 500),
        "wait_seconds": raw.get("wait_seconds"),
        "account_name": _text(raw.get("account_name"), 128),
        "task_name": _text(raw.get("task_name"), 128),
        "failure_category": _text(raw.get("failure_category"), 64) or None,
        "timeout_seconds": raw.get("timeout_seconds"),
        "retry_count_effective": raw.get("retry_count_effective"),
    }


def _mini_alert(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    deliveries: list[dict[str, str]] = []
    for delivery in raw.get("deliveries") or []:
        if not isinstance(delivery, dict):
            continue
        deliveries.append(
            {
                "channel": _text(delivery.get("channel"), 32),
                "status": _text(delivery.get("status"), 32),
                "reason": _text(delivery.get("reason"), 96),
                "error": _text(delivery.get("error"), 300),
            }
        )
    return {
        "at": _text(raw.get("at"), 64),
        "rule_id": _text(raw.get("rule_id"), 96),
        "title": _text(raw.get("title"), 240),
        "detail": _text(raw.get("detail"), 1000),
        "severity": _text(raw.get("severity"), 32) or "warning",
        "status": _text(raw.get("status"), 32),
        "reason": _text(raw.get("reason"), 96),
        "error": _text(raw.get("error"), 500),
        "deliveries": deliveries,
    }


class ControlPlane:
    _IDEMPOTENCY_TTL_SECONDS = 15 * 60
    _IDEMPOTENCY_LIMIT = 512

    def __init__(self) -> None:
        self._run_requests: dict[tuple[str, str], _IdempotentRun] = {}
        self._run_lock = asyncio.Lock()

    @staticmethod
    def _sign_service():
        from backend.services.sign_tasks import get_sign_task_service

        return get_sign_task_service()

    def list_tasks(self) -> list[dict[str, Any]]:
        tasks = self._sign_service().list_tasks(aggregate=True)
        result: list[dict[str, Any]] = []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            accounts = [
                _text(name, 128)
                for name in (item.get("account_names") or [])
                if str(name or "").strip() and str(name) != "*"
            ]
            primary = _text(item.get("account_name"), 128)
            if primary and primary != "*" and primary not in accounts:
                accounts.insert(0, primary)
            last_run = item.get("last_run")
            mini_last_run = None
            if isinstance(last_run, dict):
                mini_last_run = {
                    "time": _text(last_run.get("time"), 64),
                    "success": bool(last_run.get("success")),
                    "message": _text(last_run.get("message"), 500),
                }
            result.append(
                {
                    "name": _text(item.get("name"), 128),
                    "account_name": primary,
                    "account_names": accounts,
                    "enabled": bool(item.get("enabled", True)),
                    "sign_at": _text(item.get("sign_at"), 128),
                    "execution_mode": _text(item.get("execution_mode"), 32)
                    or "fixed",
                    "last_run": mini_last_run,
                    "active_run": _mini_active_run(item.get("active_run")),
                }
            )
        return result

    def active_runs(self) -> list[dict[str, Any]]:
        values = self._sign_service().list_active_runs()
        return [item for value in values if (item := _mini_active_run(value))]

    def recent_alerts(self, limit: int = 20) -> dict[str, Any]:
        from backend.services.alerts.config import public_alerts

        payload = public_alerts()
        all_recent = [
            _mini_alert(item) for item in (payload.get("recent") or []) if isinstance(item, dict)
        ]
        count = max(1, min(int(limit or 20), 40))
        rules = []
        for rule in payload.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            rules.append(
                {
                    "id": _text(rule.get("id"), 96),
                    "group": _text(rule.get("group"), 64),
                    "title": _text(rule.get("title"), 160),
                    "severity": _text(rule.get("severity"), 32) or "warning",
                    "enabled": bool(rule.get("enabled")),
                }
            )
        return {"items": all_recent[-count:], "total": len(all_recent), "rules": rules}

    def bootstrap(self) -> dict[str, Any]:
        tasks = self.list_tasks()
        active = self.active_runs()
        accounts_total = 0
        accounts_connected = 0
        try:
            from backend.services.telegram import get_telegram_service

            accounts = get_telegram_service().list_accounts()
            accounts_total = len(accounts)
            accounts_connected = sum(
                1
                for item in accounts
                if isinstance(item, dict)
                and not item.get("needs_relogin")
                and str(item.get("status") or "connected") not in {"invalid", "error"}
            )
            system_status = "ready"
        except Exception:
            system_status = "degraded"
        alerts = self.recent_alerts(limit=5)
        return {
            "system": {
                "status": system_status,
                "accounts_total": accounts_total,
                "accounts_connected": accounts_connected,
                "tasks_total": len(tasks),
                "tasks_enabled": sum(1 for item in tasks if item["enabled"]),
                "active_runs_total": len(active),
            },
            "tasks": tasks,
            "active_runs": active,
            "recent_alerts": alerts["items"],
            "capabilities": ["tasks:read", "tasks:run", "alerts:read"],
        }

    def _resolve_target(
        self,
        account_name: str,
        task_name: str,
        *,
        require_enabled: bool = False,
    ) -> tuple[str, str]:
        account = str(account_name or "").strip()
        task = str(task_name or "").strip()
        if not account or account == "*":
            raise ControlTaskAccountError("TASK_ACCOUNT_REQUIRED")
        if not task:
            raise ControlTaskNotFoundError("TASK_NOT_FOUND")
        try:
            found = self._sign_service().get_task(task, account_name=account)
        except ValueError as exc:
            raise ControlTaskAccountError(str(exc)) from exc
        if not found:
            raise ControlTaskNotFoundError("TASK_NOT_FOUND")
        if require_enabled and not bool(found.get("enabled", True)):
            raise ControlTaskDisabledError("TASK_DISABLED")
        return account, task

    def _prune_requests(self, now: float) -> None:
        for key, entry in list(self._run_requests.items()):
            if entry.expires_at <= now:
                self._run_requests.pop(key, None)
        if len(self._run_requests) <= self._IDEMPOTENCY_LIMIT:
            return
        oldest = sorted(
            self._run_requests.items(), key=lambda item: item[1].expires_at
        )
        for key, _entry in oldest[: len(oldest) - self._IDEMPOTENCY_LIMIT]:
            self._run_requests.pop(key, None)

    async def start_task_run(
        self,
        account_name: str,
        task_name: str,
        request_id: str | None = None,
        *,
        actor_id: str = "telegram",
    ) -> dict[str, Any]:
        target = self._resolve_target(
            account_name, task_name, require_enabled=True
        )
        key = (str(actor_id), str(request_id or "").strip())
        use_idempotency = bool(key[1])
        async with self._run_lock:
            now = time.monotonic()
            self._prune_requests(now)
            if use_idempotency and key in self._run_requests:
                entry = self._run_requests[key]
                if entry.target != target:
                    raise IdempotencyConflictError("REQUEST_ID_TARGET_CONFLICT")
                return dict(entry.result)
            result = await self._sign_service().start_task_run(*target)
            sanitized = _mini_run_status(result)
            if use_idempotency:
                self._run_requests[key] = _IdempotentRun(
                    target=target,
                    result=dict(sanitized),
                    expires_at=now + self._IDEMPOTENCY_TTL_SECONDS,
                )
            return sanitized

    def task_run_status(
        self, account_name: str, task_name: str, run_id: str | None = None
    ) -> dict[str, Any]:
        account, task = self._resolve_target(account_name, task_name)
        return _mini_run_status(
            self._sign_service().get_task_run_status(account, task, run_id=run_id)
        )


_control_plane: ControlPlane | None = None


def get_control_plane() -> ControlPlane:
    global _control_plane
    if _control_plane is None:
        _control_plane = ControlPlane()
    return _control_plane
