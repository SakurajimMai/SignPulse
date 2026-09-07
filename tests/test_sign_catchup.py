from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from backend.scheduler.sign_catchup import (
    in_daily_window,
    last_run_is_today,
    oneshot_job_id,
    should_catch_up_sign_task,
)

TZ = ZoneInfo("Asia/Hong_Kong")


def test_range_window_and_today_skip():
    now = datetime(2026, 8, 26, 10, 0, tzinfo=TZ)
    task = {
        "enabled": True,
        "execution_mode": "range",
        "range_start": "08:00",
        "range_end": "19:00",
        "last_run": {"time": "2026-08-21T09:02:59.190124+00:00"},
    }
    assert in_daily_window(now, task) is True
    assert last_run_is_today(task, now) is False
    assert should_catch_up_sign_task(task, now) is True

    later = datetime(2026, 8, 26, 20, 0, tzinfo=TZ)
    assert should_catch_up_sign_task(task, later) is False

    task_today = {
        **task,
        "last_run": {"time": "2026-08-26T02:00:00+00:00"},
    }
    assert last_run_is_today(task_today, now) is True
    assert should_catch_up_sign_task(task_today, now) is False


def test_oneshot_job_id_does_not_use_sign_prefix_only():
    job_id = oneshot_job_id("sakuramaix", "Hyvps签到", "2026-08-26")
    assert job_id.startswith("oneshot-sign-")
    assert job_id != "sign-sakuramaix-Hyvps签到"


def test_real_startup_sync_runs_catch_up_once(monkeypatch):
    from backend import main

    calls: list[str] = []

    async def fake_sync_jobs() -> None:
        calls.append("sync")

    async def fake_catch_up() -> None:
        calls.append("catch-up")

    monkeypatch.setattr(main, "sync_jobs", fake_sync_jobs)
    monkeypatch.setattr(main, "catch_up_missed_sign_tasks", fake_catch_up)

    asyncio.run(main._sync_scheduler_on_startup())

    assert calls == ["sync", "catch-up"]


def test_catch_up_skips_replica(monkeypatch):
    import backend.scheduler as scheduler_module
    from backend.scheduler import instance_lock

    added_jobs: list[object] = []
    fake_scheduler = SimpleNamespace(
        add_job=lambda *args, **kwargs: added_jobs.append((args, kwargs))
    )
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)
    monkeypatch.setattr(instance_lock, "has_scheduler_lock", lambda: False)

    asyncio.run(scheduler_module.catch_up_missed_sign_tasks())

    assert added_jobs == []
