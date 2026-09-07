from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import mini_app
from backend.services import control_plane as control_module
from backend.services.control_plane import (
    ControlPlane,
    ControlTaskDisabledError,
    IdempotencyConflictError,
)
from backend.services.telegram_bot import auth

BOT_TOKEN = "123456:mini-app-test-token"
SECRET_KEY = "mini-app-route-test-secret"


def _signed_init_data(user_id: int, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "route-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "Operator", "username": "ops"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeSignService:
    def __init__(self) -> None:
        self.start_calls = 0
        self.enabled = True

    def list_tasks(self, aggregate=True):
        assert aggregate is True
        return [
            {
                "name": "daily",
                "account_name": "*",
                "account_names": ["acct-a", "acct-b"],
                "enabled": self.enabled,
                "sign_at": "08:00",
                "execution_mode": "fixed",
            }
        ]

    def list_active_runs(self):
        return []

    def get_task(self, task_name, account_name=None):
        if task_name != "daily" or account_name not in {"acct-a", "acct-b"}:
            return None
        return {"name": task_name, "account_name": account_name, "enabled": self.enabled}

    async def start_task_run(self, account_name, task_name):
        self.start_calls += 1
        return {
            "run_id": f"run-{self.start_calls}",
            "state": "running",
            "account_name": account_name,
            "task_name": task_name,
        }

    def get_task_run_status(self, account_name, task_name, run_id=None):
        return {
            "run_id": run_id or "run-1",
            "state": "running",
            "account_name": account_name,
            "task_name": task_name,
        }


@pytest.mark.asyncio
async def test_control_plane_requires_enabled_task_and_deduplicates_request_id():
    service = FakeSignService()
    plane = ControlPlane()
    plane._sign_service = lambda: service

    first = await plane.start_task_run(
        "acct-a", "daily", "request-123", actor_id="operator:42"
    )
    second = await plane.start_task_run(
        "acct-a", "daily", "request-123", actor_id="operator:42"
    )
    assert first == second
    assert service.start_calls == 1

    with pytest.raises(IdempotencyConflictError):
        await plane.start_task_run(
            "acct-b", "daily", "request-123", actor_id="operator:42"
        )

    service.enabled = False
    with pytest.raises(ControlTaskDisabledError):
        await plane.start_task_run(
            "acct-a", "daily", "request-disabled", actor_id="operator:42"
        )


def test_control_plane_alerts_keep_severity_and_sanitized_deliveries(monkeypatch):
    monkeypatch.setattr(
        "backend.services.alerts.config.public_alerts",
        lambda: {
            "rules": [
                {
                    "id": "task_fail",
                    "group": "tasks",
                    "title": "Task failed",
                    "severity": "critical",
                    "enabled": True,
                    "private": "must-not-leak",
                }
            ],
            "recent": [
                {
                    "at": "2026-08-31T00:00:00Z",
                    "rule_id": "task_fail",
                    "title": "Task failed",
                    "detail": "details",
                    "severity": "critical",
                    "status": "failed",
                    "deliveries": [
                        {
                            "channel": "telegram",
                            "status": "failed",
                            "reason": "network",
                            "error": "timeout",
                            "token": "must-not-leak",
                        }
                    ],
                }
            ],
        },
    )
    result = ControlPlane().recent_alerts(limit=5)
    assert result["items"][0]["severity"] == "critical"
    assert result["items"][0]["deliveries"] == [
        {
            "channel": "telegram",
            "status": "failed",
            "reason": "network",
            "error": "timeout",
        }
    ]
    assert result["rules"][0]["severity"] == "critical"
    assert "private" not in result["rules"][0]


def test_mini_app_auth_bootstrap_run_and_live_allowlist(monkeypatch):
    now = int(time.time())
    settings = {
        "telegram_bot_control_enabled": True,
        "telegram_bot_token": BOT_TOKEN,
        "telegram_bot_allowed_user_ids": [42],
    }
    fake_config = SimpleNamespace(get_global_settings=lambda: settings)
    service = FakeSignService()
    plane = ControlPlane()
    plane._sign_service = lambda: service
    monkeypatch.setattr(mini_app, "get_config_service", lambda: fake_config)
    monkeypatch.setattr(mini_app, "get_control_plane", lambda: plane)
    monkeypatch.setattr(mini_app, "has_scheduler_lock", lambda: True)
    monkeypatch.setattr(auth, "get_config_service", lambda: fake_config)
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(secret_key=SECRET_KEY))
    monkeypatch.setattr(
        control_module.ControlPlane,
        "bootstrap",
        lambda self: {
            "system": {
                "status": "ready",
                "accounts_total": 2,
                "accounts_connected": 2,
                "tasks_total": 1,
                "tasks_enabled": 1,
                "active_runs_total": 0,
            },
            "tasks": self.list_tasks(),
            "active_runs": [],
            "recent_alerts": [],
            "capabilities": ["tasks:read", "tasks:run", "alerts:read"],
        },
    )
    mini_app.rate_limiter.reset_all()

    app = FastAPI()
    app.include_router(mini_app.router, prefix="/api/mini-app")
    client = TestClient(app)
    response = client.post(
        "/api/mini-app/auth",
        json={"init_data": _signed_init_data(42, now)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["id"] == 42
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    bootstrap = client.get("/api/mini-app/bootstrap", headers=headers)
    assert bootstrap.status_code == 200
    assert bootstrap.json()["tasks"][0]["account_names"] == ["acct-a", "acct-b"]

    run = client.post(
        "/api/mini-app/tasks/daily/run",
        headers=headers,
        json={"account_name": "acct-a", "request_id": "route-request-123"},
    )
    assert run.status_code == 200
    assert run.json()["run_id"] == "run-1"

    repeated = client.post(
        "/api/mini-app/tasks/daily/run",
        headers=headers,
        json={"account_name": "acct-a", "request_id": "route-request-123"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["run_id"] == "run-1"
    assert service.start_calls == 1

    primary_status = client.get(
        "/api/mini-app/tasks/daily/run/status",
        headers=headers,
        params={"account_name": "acct-a", "run_id": "run-1"},
    )
    assert primary_status.status_code == 200
    assert primary_status.json()["run_id"] == "run-1"

    monkeypatch.setattr(mini_app, "has_scheduler_lock", lambda: False)
    replica_run = client.post(
        "/api/mini-app/tasks/daily/run",
        headers=headers,
        json={"account_name": "acct-a", "request_id": "replica-request-123"},
    )
    assert replica_run.status_code == 503
    assert replica_run.json()["detail"] == "MINI_APP_PRIMARY_UNAVAILABLE"
    assert replica_run.headers["retry-after"] == "3"
    assert service.start_calls == 1

    replica_status = client.get(
        "/api/mini-app/tasks/daily/run/status",
        headers=headers,
        params={"account_name": "acct-a", "run_id": "run-1"},
    )
    assert replica_status.status_code == 503
    assert replica_status.json()["detail"] == "MINI_APP_PRIMARY_UNAVAILABLE"
    assert replica_status.headers["retry-after"] == "3"

    settings["telegram_bot_allowed_user_ids"] = [99]
    denied = client.get("/api/mini-app/bootstrap", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "MINI_APP_OPERATOR_NOT_ALLOWED"
