"""签到调度补跑：避免容器重启把时间窗内的随机等待冲掉。"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.utils.time import utc_now


def parse_clock(value: str) -> time | None:
    raw = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def parse_last_run_at(task: dict[str, Any]) -> datetime | None:
    last = task.get("last_run")
    raw = ""
    if isinstance(last, dict):
        raw = str(last.get("time") or "")
    elif last:
        raw = str(last)
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=utc_now().tzinfo)
    return parsed


def localize_now(timezone_name: str) -> datetime:
    try:
        tz = ZoneInfo(str(timezone_name or "").strip() or "Asia/Hong_Kong")
    except Exception:
        tz = ZoneInfo("Asia/Hong_Kong")
    now = utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return now.astimezone(tz)


def in_daily_window(now: datetime, task: dict[str, Any]) -> bool:
    mode = str(task.get("execution_mode") or "daily").strip().lower()
    if mode == "range":
        start = parse_clock(str(task.get("range_start") or task.get("sign_at") or "08:00"))
        end = parse_clock(str(task.get("range_end") or "23:59"))
        if start is None or end is None:
            return True
        current = now.time().replace(microsecond=0)
        start_t = start
        end_t = end
        if end_t <= start_t:
            return current >= start_t or current <= end_t
        return start_t <= current <= end_t
    sign_at = parse_clock(str(task.get("sign_at") or "00:00"))
    if sign_at is None:
        return True
    return now.time().replace(microsecond=0) >= sign_at


def last_run_is_today(task: dict[str, Any], now: datetime) -> bool:
    last = parse_last_run_at(task)
    if last is None:
        return False
    local_last = last.astimezone(now.tzinfo) if now.tzinfo else last
    return local_last.date() == now.date()


def should_catch_up_sign_task(task: dict[str, Any], now: datetime) -> bool:
    if not task.get("enabled", True):
        return False
    if str(task.get("execution_mode") or "") == "listen":
        return False
    if last_run_is_today(task, now):
        return False
    return in_daily_window(now, task)


def oneshot_job_id(account_name: str, task_name: str, day: str) -> str:
    return f"oneshot-sign-{account_name}-{task_name}-{day}"


def compute_range_run_at(now: datetime, task: dict[str, Any], delay_seconds: float) -> datetime:
    start = parse_clock(str(task.get("range_start") or task.get("sign_at") or "08:00"))
    end = parse_clock(str(task.get("range_end") or "23:59"))
    run_at = now + timedelta(seconds=max(0.0, float(delay_seconds)))
    if end is not None:
        end_dt = now.replace(
            hour=end.hour, minute=end.minute, second=end.second, microsecond=0
        )
        if start and end_dt <= now.replace(
            hour=start.hour, minute=start.minute, second=start.second, microsecond=0
        ):
            end_dt += timedelta(days=1)
        if run_at > end_dt:
            return end_dt
    return run_at
