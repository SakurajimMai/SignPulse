from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from backend import main as main_module
from backend import scheduler as scheduler_module
from backend.services import alerts as alerts_module
from backend.services import backup_archive, sign_task_notify
from backend.services import config as config_module
from backend.services import device_keepalive as keepalive_module
from backend.services.alerts import dispatcher as alert_dispatcher
from backend.services.sign_task_runner import _runner_send_notifications


@pytest.mark.asyncio()
async def test_sign_task_failure_schedules_email_alert(monkeypatch):
    scheduled: list[dict] = []

    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    await _runner_send_notifications(
        {
            "success": False,
            "account_invalid_detected": False,
            "task_notify_on_failure": True,
            "failure_category": "network_proxy",
            "account_name": "account-a",
            "task_name": "daily",
            "error_msg": "network down",
            "final_logs": [],
        }
    )

    assert scheduled == [
        {
            "rule_id": "sign_task_network_fail",
            "title": "签到任务失败：account-a/daily",
            "detail": "network down\nfailure_category=network_proxy",
            "fingerprint": '["account-a","daily"]',
            "channel_overrides": {"telegram": True},
        }
    ]


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    ("category", "expected_rule"),
    [
        ("flood_wait", "sign_task_rate_limited"),
        ("ai_timeout", "sign_task_ai_fail"),
        ("ai_error", "sign_task_ai_fail"),
        ("network_proxy", "sign_task_network_fail"),
        ("timeout", "sign_task_timeout"),
        ("button_not_found", "sign_task_flow_fail"),
        ("unknown", "sign_task_flow_fail"),
    ],
)
async def test_sign_failure_category_selects_canonical_rule(
    monkeypatch, category, expected_rule
):
    scheduled: list[str] = []
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **_kwargs: scheduled.append(rule_id),
    )

    await _runner_send_notifications(
        {
            "success": False,
            "account_invalid_detected": False,
            "failure_category": category,
            "account_name": "account-a",
            "task_name": "daily",
            "error_msg": "failed",
            "final_logs": [],
        }
    )

    assert scheduled == [expected_rule]


@pytest.mark.asyncio()
async def test_sign_task_alert_fingerprint_has_no_delimiter_collision(
    monkeypatch,
):
    scheduled: list[dict] = []
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append(
            {"rule_id": rule_id, **kwargs}
        ),
    )

    for account_name, task_name in (("a:b", "c"), ("a", "b:c")):
        await _runner_send_notifications(
            {
                "success": False,
                "account_invalid_detected": False,
                "task_notify_on_failure": False,
                "account_name": account_name,
                "task_name": task_name,
                "error_msg": "failed",
                "final_logs": [],
            }
        )

    assert scheduled[0]["fingerprint"] != scheduled[1]["fingerprint"]


@pytest.mark.asyncio()
async def test_sign_task_email_alert_is_independent_from_bot_flag(monkeypatch):
    scheduled: list[dict] = []
    sent: list[dict] = []

    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    async def _notify(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(sign_task_notify, "send_failure_notification", _notify)
    await _runner_send_notifications(
        {
            "success": False,
            "account_invalid_detected": False,
            "task_notify_on_failure": False,
            "failure_category": "timeout",
            "account_name": "account-a",
            "task_name": "daily",
            "error_msg": "timed out",
            "final_logs": [],
        }
    )

    assert [item["rule_id"] for item in scheduled] == ["sign_task_timeout"]
    assert scheduled[0]["channel_overrides"] == {"telegram": False}
    assert sent == []


@pytest.mark.asyncio()
async def test_invalid_account_always_uses_unified_rule_without_direct_bot(monkeypatch):
    scheduled: list[dict] = []
    sent: list[dict] = []
    statuses = [{}, {"invalid_notified_at": "2026-08-31T00:00:00Z"}]

    monkeypatch.setattr(
        sign_task_notify, "get_account_status", lambda _name: statuses.pop(0)
    )
    monkeypatch.setattr(
        sign_task_notify, "set_account_status", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    async def _notify(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(sign_task_notify, "send_account_invalid_notification", _notify)

    first = await sign_task_notify.mark_account_invalid(
        account_name="account-a", task_name="daily", message="session expired"
    )
    second = await sign_task_notify.mark_account_invalid(
        account_name="account-a", task_name="daily", message="session expired"
    )

    assert first is True and second is False
    assert len(scheduled) == 2
    assert scheduled[0]["rule_id"] == "account_login_invalid"
    assert scheduled[0]["fingerprint"] == "account-a"
    assert scheduled[0]["channel_overrides"] == {"telegram": True}
    assert sent == []


@pytest.mark.asyncio()
async def test_account_email_alert_is_independent_from_bot_flag(monkeypatch):
    scheduled: list[dict] = []
    sent: list[dict] = []

    monkeypatch.setattr(sign_task_notify, "get_account_status", lambda _name: {})
    monkeypatch.setattr(
        sign_task_notify, "set_account_status", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    async def _notify(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(sign_task_notify, "send_account_invalid_notification", _notify)
    await sign_task_notify.mark_account_invalid(
        account_name="account-a",
        task_name="daily",
        message="session expired",
        notify_on_failure=False,
    )

    assert [item["rule_id"] for item in scheduled] == ["account_login_invalid"]
    assert scheduled[0]["channel_overrides"] == {"telegram": False}
    assert sent == []


@pytest.mark.asyncio()
async def test_device_keepalive_failures_are_aggregated(monkeypatch):
    scheduled: list[dict] = []

    class _Keepalive:
        async def run_due(self):
            return {
                "checked": 2,
                "kept_alive": 1,
                "skipped": 0,
                "failed": 1,
                "results": [
                    {
                        "account_name": "account-b",
                        "status": "failed",
                        "message": "AUTH_KEY_UNREGISTERED",
                    }
                ],
            }

    monkeypatch.setattr(
        keepalive_module, "get_device_keepalive_service", lambda: _Keepalive()
    )
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    await scheduler_module._job_device_keepalive()

    assert len(scheduled) == 1
    assert scheduled[0]["rule_id"] == "system_device_keepalive_fail"
    assert "account-b" in scheduled[0]["detail"]


@pytest.mark.asyncio()
async def test_device_keepalive_does_not_duplicate_account_invalid_alert(monkeypatch):
    scheduled: list[dict] = []

    class _Keepalive:
        async def run_due(self):
            return {
                "checked": 1,
                "kept_alive": 0,
                "skipped": 0,
                "failed": 1,
                "results": [
                    {
                        "account_name": "account-b",
                        "status": "failed",
                        "message": "AUTH_KEY_UNREGISTERED",
                        "needs_relogin": True,
                    }
                ],
            }

    monkeypatch.setattr(
        keepalive_module, "get_device_keepalive_service", lambda: _Keepalive()
    )
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    await scheduler_module._job_device_keepalive()

    assert scheduled == []


@pytest.mark.asyncio()
async def test_auto_backup_failure_schedules_alert(monkeypatch, tmp_path):
    scheduled: list[dict] = []

    monkeypatch.setattr(
        config_module,
        "get_config_service",
        lambda: SimpleNamespace(
            get_global_settings=lambda: {"auto_backup_enabled": True}
        ),
    )
    monkeypatch.setattr(backup_archive, "should_run_auto_backup", lambda _cfg: True)
    monkeypatch.setattr(backup_archive, "auto_backup_keep", lambda _cfg: 3)
    monkeypatch.setattr(
        backup_archive,
        "run_auto_backup",
        lambda *_args, **_kwargs: {
            "success": False,
            "error": "archive failed",
            "path": tmp_path / "broken.zip",
            "webdav": {},
        },
    )
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    await scheduler_module._job_auto_backup()

    assert len(scheduled) == 1
    assert scheduled[0]["rule_id"] == "system_backup_archive_fail"
    assert "archive failed" in scheduled[0]["detail"]


@pytest.mark.asyncio()
async def test_backup_webdav_and_retention_use_independent_rules(
    monkeypatch, tmp_path
):
    scheduled: list[dict] = []
    monkeypatch.setattr(
        config_module,
        "get_config_service",
        lambda: SimpleNamespace(
            get_global_settings=lambda: {
                "auto_backup_enabled": True,
                "webdav_url": "https://dav.example",
            }
        ),
    )
    monkeypatch.setattr(backup_archive, "should_run_auto_backup", lambda _cfg: True)
    monkeypatch.setattr(backup_archive, "auto_backup_keep", lambda _cfg: 3)
    monkeypatch.setattr(
        backup_archive,
        "run_auto_backup",
        lambda *_args, **_kwargs: {
            "success": True,
            "path": tmp_path / "backup.zip",
            "webdav": {"success": False, "error": "upload failed"},
            "remote_prune": {"success": False, "error": "prune failed"},
        },
    )
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    await scheduler_module._job_auto_backup()

    assert [item["rule_id"] for item in scheduled] == [
        "system_backup_webdav_fail",
        "system_backup_retention_fail",
    ]


@pytest.mark.asyncio()
async def test_memory_monitor_connects_alert_callback(monkeypatch):
    scheduled: list[dict] = []

    class _Monitor:
        def __init__(self, **kwargs):
            self.threshold_mb = kwargs["threshold_mb"]
            self.check_interval = kwargs["check_interval"]
            self.alert_repeat_interval = kwargs["alert_repeat_interval"]
            self._callback = kwargs["alert_callback"]

        def check(self):
            self._callback(
                SimpleNamespace(
                    rss_mb=640.0,
                    threshold_mb=self.threshold_mb,
                    message="内存使用 640.0 MB 超过阈值 512.0 MB",
                )
            )

        def get_stats(self):
            return {"current_rss_mb": 640.0, "alert_count": 1}

    async def _cancel(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setenv("INSTANCE_ID", "worker-a")
    monkeypatch.setenv("MEMORY_CHECK_INTERVAL_S", "60")
    monkeypatch.setenv("MEMORY_ALERT_REPEAT_INTERVAL_S", "nan")
    monkeypatch.setattr("backend.utils.memory_monitor.MemoryMonitor", _Monitor)
    monkeypatch.setattr(main_module.asyncio, "sleep", _cancel)
    monkeypatch.setattr(
        alerts_module,
        "schedule_alert",
        lambda rule_id, **kwargs: scheduled.append({"rule_id": rule_id, **kwargs}),
    )

    with pytest.raises(asyncio.CancelledError):
        await main_module._memory_monitor_loop()

    assert len(scheduled) == 1
    assert scheduled[0]["rule_id"] == "system_memory_high"
    assert scheduled[0]["fingerprint"] == (
        f"backend-process:worker-a:{main_module.os.getpid()}"
    )
    assert "实例：worker-a" in scheduled[0]["detail"]
    assert f"PID：{main_module.os.getpid()}" in scheduled[0]["detail"]
    assert main_module.app.state.memory_monitor.alert_repeat_interval == 300.0


@pytest.mark.asyncio
async def test_memory_alert_fingerprint_isolated_by_instance(monkeypatch):
    scheduled: list[dict] = []

    class _Record:
        rss_mb = 700.0
        message = "rss high"

    class _Monitor:
        def __init__(self, **kwargs):
            self.threshold_mb = kwargs["threshold_mb"]
            self.check_interval = kwargs["check_interval"]
            self._callback = kwargs["alert_callback"]

        def check(self):
            self._callback(_Record())

        def get_stats(self):
            return {"current_rss_mb": 700.0, "alert_count": 1}

    async def _cancel(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("backend.utils.memory_monitor.MemoryMonitor", _Monitor)
    monkeypatch.setattr(main_module.asyncio, "sleep", _cancel)
    monkeypatch.setattr(
        "backend.services.alerts.schedule_alert",
        lambda rule_id, **kwargs: scheduled.append(
            {"rule_id": rule_id, **kwargs}
        ),
    )

    cases = (("worker-a", 1001), ("worker-a", 1002), ("worker-b", 1001))
    for instance_id, pid in cases:
        monkeypatch.setenv("INSTANCE_ID", instance_id)
        monkeypatch.setattr(main_module.os, "getpid", lambda pid=pid: pid)
        with pytest.raises(asyncio.CancelledError):
            await main_module._memory_monitor_loop()

    assert [item["fingerprint"] for item in scheduled] == [
        "backend-process:worker-a:1001",
        "backend-process:worker-a:1002",
        "backend-process:worker-b:1001",
    ]


@pytest.mark.asyncio()
async def test_scheduled_alert_is_retained_and_drained(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    drain_task = None

    def _fire(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(alert_dispatcher, "fire_alert", _fire)
    try:
        alert_dispatcher.schedule_alert("system_memory_high", title="high memory")
        assert await asyncio.to_thread(started.wait, 2)
        assert any(
            not task.done() for task in alert_dispatcher._scheduled_tasks
        )
        drain_task = asyncio.create_task(
            alert_dispatcher.drain_scheduled_alerts(timeout=2)
        )
        await asyncio.sleep(0.05)
        assert not drain_task.done()
    finally:
        release.set()
        if drain_task is not None:
            await drain_task
        else:
            await alert_dispatcher.drain_scheduled_alerts(timeout=2)
    assert all(task.done() for task in alert_dispatcher._scheduled_tasks)
