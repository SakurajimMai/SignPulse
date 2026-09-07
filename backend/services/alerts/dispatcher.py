from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.utils.time import utc_now, utc_now_iso_z

from .config import mutate_alerts_state
from .rules import RULE_IDS, rule_severity, rule_title
from .smtp import send_smtp_mail, smtp_ready

logger = logging.getLogger("backend.alerts")

FAILED_RETRY_BACKOFF_SECONDS = 60
IN_FLIGHT_TTL_SECONDS = 5 * 60
DELIVERY_CHANNELS = ("email", "telegram")
SIGN_TASK_FAILURE_RULE_IDS = frozenset(
    {
        "sign_task_rate_limited",
        "sign_task_ai_fail",
        "sign_task_network_fail",
        "sign_task_timeout",
        "sign_task_flow_fail",
    }
)
_scheduled_tasks: set[asyncio.Task[None]] = set()


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _within_window(
    last_iso: Any, seconds: int | float, *, now: datetime | None = None
) -> bool:
    last = _parse_ts(last_iso)
    if last is None:
        return False
    current = now or utc_now()
    return last <= current < last + timedelta(seconds=max(1, seconds))


def _within_cooldown(
    last_iso: Any, minutes: int, *, now: datetime | None = None
) -> bool:
    return _within_window(last_iso, max(1, minutes) * 60, now=now)


def _fingerprint(rule_id: str, fingerprint: str | None) -> str:
    text = str(fingerprint or rule_id).strip() or rule_id
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return f"{rule_id}:{digest}"


def _delivery_key(event_key: str, channel: str) -> str:
    return f"{event_key}:{channel}"


def _normalize_channel_overrides(
    value: Mapping[str, Any] | None,
) -> dict[str, bool]:
    if not value:
        return {}
    return {
        channel: bool(value[channel])
        for channel in DELIVERY_CHANNELS
        if channel in value
    }


def _clean_title(value: Any, fallback: str) -> str:
    text = str(value or fallback).replace("\r", " ").replace("\n", " ")
    return " ".join(text.split()).strip()[:160] or fallback


def _append_recent(
    state: dict[str, Any],
    *,
    rule_id: str,
    title: str,
    detail: str,
    status: str,
    severity: str = "warning",
    deliveries: list[dict[str, str]] | None = None,
    reason: str = "",
    error: str = "",
) -> None:
    recent = list(state.get("recent") or [])
    item: dict[str, Any] = {
        "at": utc_now_iso_z(),
        "rule_id": rule_id,
        "title": title,
        "detail": str(detail or "")[:500],
        "severity": severity,
        "status": status,
        "error": str(error or "")[:300],
        "deliveries": [dict(delivery) for delivery in (deliveries or [])],
    }
    if reason:
        item["reason"] = reason
    recent.append(item)
    state["recent"] = recent


def _active_reservation(value: Any, now: datetime) -> bool:
    if not isinstance(value, dict) or not value.get("token"):
        return False
    return _within_window(value.get("at"), IN_FLIGHT_TTL_SECONDS, now=now)


def _delivery_status(deliveries: list[dict[str, str]]) -> tuple[str, str, str]:
    sent = [item for item in deliveries if item.get("status") == "sent"]
    failed = [item for item in deliveries if item.get("status") == "failed"]
    if sent:
        status = "partial" if failed else "sent"
    elif failed:
        status = "failed"
    else:
        status = "skipped"

    errors = [item.get("error", "") for item in failed if item.get("error")]
    reasons = [item.get("reason", "") for item in deliveries if item.get("reason")]
    reason = ""
    if status == "skipped":
        reason = reasons[0] if reasons and len(set(reasons)) == 1 else ""
        if not reason:
            reason = "no_deliverable_channel"
    return status, reason, "; ".join(errors)[:300]


def _telegram_text(
    *, rule_label: str, subject_title: str, severity: str, detail: str
) -> str:
    lines = [
        f"[SignPulse] {rule_label}",
        f"级别：{severity}",
        f"标题：{subject_title}",
        f"时间：{utc_now_iso_z()}",
    ]
    if detail:
        lines.extend(["", str(detail).strip()[:3000]])
    return "\n".join(lines)[:3900]


def fire_alert(
    rule_id: str,
    *,
    title: str,
    detail: str = "",
    fingerprint: str | None = None,
    channel_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reserve and deliver one event independently through each channel."""
    subject_title = _clean_title(title, rule_title(rule_id))
    severity = rule_severity(rule_id)
    if rule_id not in RULE_IDS:
        result = {
            "status": "ignored",
            "reason": "unknown_rule",
            "deliveries": [],
        }

        def _record_unknown(state: dict[str, Any]) -> None:
            _append_recent(
                state,
                rule_id=rule_id,
                title=subject_title,
                detail=detail,
                status="ignored",
                reason="unknown_rule",
                deliveries=[],
            )

        mutate_alerts_state(_record_unknown)
        return result

    from backend.services import push_notifications
    from backend.services.config import get_config_service

    global_settings = get_config_service().get_global_settings()
    email_ready = smtp_ready(global_settings)
    bot_ready = push_notifications.telegram_bot_ready(global_settings)
    quiet_hours = push_notifications.is_in_quiet_hours(global_settings)
    effective_channel_overrides = _normalize_channel_overrides(channel_overrides)
    if rule_id in SIGN_TASK_FAILURE_RULE_IDS and not global_settings.get(
        "telegram_bot_task_failure_enabled", True
    ):
        effective_channel_overrides["telegram"] = False
    if rule_id == "account_login_invalid" and not global_settings.get(
        "telegram_bot_login_notify_enabled", False
    ):
        effective_channel_overrides["telegram"] = False
    event_key = _fingerprint(rule_id, fingerprint)
    reservation_token = uuid.uuid4().hex
    reserved_at = utc_now()
    reserved_iso = reserved_at.isoformat().replace("+00:00", "Z")

    def _reserve(state: dict[str, Any]) -> dict[str, Any]:
        rule = state["rules"].get(rule_id) or {}
        last_sent = state.get("last_sent") or {}
        last_attempt = state.get("last_attempt") or {}
        in_flight = state.get("in_flight") or {}
        for stale_key, value in list(in_flight.items()):
            if not _active_reservation(value, reserved_at):
                in_flight.pop(stale_key, None)

        deliveries: list[dict[str, str]] = []
        reserved_channels: list[str] = []
        cooldown = int(rule.get("cooldown_minutes") or 30)
        for channel in DELIVERY_CHANNELS:
            delivery: dict[str, str] = {"channel": channel}
            channel_enabled = bool(rule.get(f"{channel}_enabled"))
            reason = ""
            if not rule.get("enabled"):
                reason = "disabled"
            elif not channel_enabled:
                reason = "channel_disabled"
            elif effective_channel_overrides.get(channel) is False:
                reason = "context_disabled"
            elif channel == "email" and not email_ready:
                reason = "email_not_ready"
            elif channel == "telegram" and not bot_ready:
                reason = "telegram_not_ready"
            elif channel == "telegram" and quiet_hours:
                reason = "quiet_hours"

            key = _delivery_key(event_key, channel)
            if not reason and _within_cooldown(
                last_sent.get(key), cooldown, now=reserved_at
            ):
                reason = "cooldown"
            if not reason and _active_reservation(in_flight.get(key), reserved_at):
                reason = "in_flight"
            if not reason and _within_window(
                last_attempt.get(key), FAILED_RETRY_BACKOFF_SECONDS, now=reserved_at
            ):
                reason = "retry_backoff"

            if reason:
                delivery.update(status="skipped", reason=reason)
            else:
                in_flight[key] = {
                    "token": reservation_token,
                    "at": reserved_iso,
                }
                last_attempt[key] = reserved_iso
                reserved_channels.append(channel)
                delivery["status"] = "reserved"
            deliveries.append(delivery)

        state["in_flight"] = in_flight
        state["last_attempt"] = last_attempt
        if not reserved_channels:
            status, reason, error = _delivery_status(deliveries)
            _append_recent(
                state,
                rule_id=rule_id,
                title=subject_title,
                detail=detail,
                severity=severity,
                status=status,
                reason=reason,
                error=error,
                deliveries=deliveries,
            )
        return {
            "channels": reserved_channels,
            "deliveries": deliveries,
        }

    reservation = mutate_alerts_state(_reserve)
    if not reservation["channels"]:
        status, reason, error = _delivery_status(reservation["deliveries"])
        return {
            "status": status,
            "reason": reason,
            "error": error,
            "deliveries": reservation["deliveries"],
        }

    rule_label = _clean_title(rule_title(rule_id), rule_id)
    subject = f"[SignPulse] {rule_label}：{subject_title}"
    if subject_title == rule_label:
        subject = f"[SignPulse] {subject_title}"
    body_lines = [
        f"规则：{rule_label}",
        f"级别：{severity}",
        f"标题：{subject_title}",
        f"时间：{utc_now_iso_z()}",
    ]
    if detail:
        body_lines.extend(["", str(detail).strip()[:2000]])
    body = "\n".join(body_lines)

    deliveries = reservation["deliveries"]
    by_channel = {item["channel"]: item for item in deliveries}
    for channel in reservation["channels"]:
        delivery = by_channel[channel]
        try:
            if channel == "email":
                send_smtp_mail(
                    subject=subject,
                    body=body,
                    settings=global_settings,
                )
            else:
                push_notifications.send_telegram_notifications_sync(
                    global_settings,
                    text=_telegram_text(
                        rule_label=rule_label,
                        subject_title=subject_title,
                        severity=severity,
                        detail=detail,
                    ),
                )
            delivery["status"] = "sent"
        except Exception as exc:
            delivery["status"] = "failed"
            delivery["error"] = str(exc)[:300]
            logger.warning(
                "告警%s投递失败 rule=%s: %s",
                channel,
                rule_id,
                exc,
            )

    completed_iso = utc_now_iso_z()
    status, reason, error = _delivery_status(deliveries)

    def _finish(state: dict[str, Any]) -> None:
        in_flight = state.get("in_flight") or {}
        last_sent = state.get("last_sent") or {}
        last_attempt = state.get("last_attempt") or {}
        for channel in reservation["channels"]:
            key = _delivery_key(event_key, channel)
            current = in_flight.get(key)
            if isinstance(current, dict) and current.get("token") == reservation_token:
                in_flight.pop(key, None)
            delivery = by_channel[channel]
            if delivery.get("status") == "sent":
                last_sent[key] = completed_iso
            else:
                last_attempt[key] = completed_iso
        state["in_flight"] = in_flight
        state["last_sent"] = last_sent
        state["last_attempt"] = last_attempt
        _append_recent(
            state,
            rule_id=rule_id,
            title=subject_title,
            detail=detail,
            severity=severity,
            status=status,
            reason=reason,
            error=error,
            deliveries=deliveries,
        )

    mutate_alerts_state(_finish)
    return {
        "status": status,
        "reason": reason,
        "error": error,
        "deliveries": deliveries,
    }


def schedule_alert(
    rule_id: str,
    *,
    title: str,
    detail: str = "",
    fingerprint: str | None = None,
    channel_overrides: Mapping[str, Any] | None = None,
) -> None:
    """Schedule a non-blocking alert without leaking failures to its caller."""
    normalized_overrides = _normalize_channel_overrides(channel_overrides)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            fire_alert(
                rule_id,
                title=title,
                detail=detail,
                fingerprint=fingerprint,
                channel_overrides=normalized_overrides,
            )
        except Exception:
            logger.debug("告警同步投递失败 rule=%s", rule_id, exc_info=True)
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(
                fire_alert,
                rule_id,
                title=title,
                detail=detail,
                fingerprint=fingerprint,
                channel_overrides=normalized_overrides,
            )
        except Exception:
            logger.warning("告警后台投递失败 rule=%s", rule_id, exc_info=True)

    try:
        task = loop.create_task(_run(), name=f"alert-{rule_id}")
        _scheduled_tasks.add(task)
        task.add_done_callback(_scheduled_tasks.discard)
    except Exception:
        logger.debug("无法调度告警任务 rule=%s", rule_id, exc_info=True)


async def drain_scheduled_alerts(timeout: float = 5.0) -> None:
    """Give already scheduled alert deliveries a bounded shutdown grace period."""
    pending = {task for task in _scheduled_tasks if not task.done()}
    if not pending:
        return
    _done, still_pending = await asyncio.wait(
        pending,
        timeout=max(0.0, float(timeout)),
    )
    if still_pending:
        logger.warning("关闭时仍有 %s 个告警投递任务未完成", len(still_pending))
