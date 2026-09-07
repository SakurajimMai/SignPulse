from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StrictBool, StrictStr, conint, validator

from backend.core.auth import get_current_user
from backend.models.user import User
from backend.services.alerts.config import public_alerts, save_alert_rules
from backend.services.alerts.rules import ACCEPTED_RULE_IDS
from backend.services.alerts.smtp import send_smtp_mail, smtp_ready
from backend.services.config import get_config_service
from backend.services.push_notifications import is_in_quiet_hours

logger = logging.getLogger("backend.alerts.api")
router = APIRouter()


CooldownMinutes = conint(strict=True, ge=1, le=24 * 60)


class AlertRuleUpdate(BaseModel):
    id: StrictStr
    enabled: StrictBool
    email_enabled: StrictBool | None = None
    telegram_enabled: StrictBool | None = None
    cooldown_minutes: CooldownMinutes

    @validator("id")
    def validate_rule_id(cls, value: str) -> str:
        if value not in ACCEPTED_RULE_IDS:
            raise ValueError(f"unknown alert rule: {value}")
        return value

    class Config:
        extra = "forbid"


class AlertsSaveRequest(BaseModel):
    rules: list[AlertRuleUpdate]

    @validator("rules")
    def validate_unique_rules(
        cls, rules: list[AlertRuleUpdate]
    ) -> list[AlertRuleUpdate]:
        if not rules:
            raise ValueError("at least one alert rule is required")
        ids = [item.id for item in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate alert rule id")
        return rules

    class Config:
        extra = "forbid"


class AlertTestResponse(BaseModel):
    success: bool
    message: str


@router.get("/alerts")
def get_alerts(current_user: User = Depends(get_current_user)):
    payload = public_alerts()
    settings = get_config_service().get_global_settings()
    from backend.services.push_notifications import telegram_bot_ready

    payload["smtp_ready"] = smtp_ready(settings)
    payload["smtp_enabled"] = bool(settings.get("smtp_enabled"))
    payload["bot_ready"] = telegram_bot_ready(settings)
    payload["quiet_hours"] = is_in_quiet_hours(settings)
    return payload


@router.put("/alerts")
def put_alerts(
    request: AlertsSaveRequest, current_user: User = Depends(get_current_user)
):
    payload = save_alert_rules(
        {"rules": [item.dict(exclude_none=True) for item in request.rules]}
    )
    settings = get_config_service().get_global_settings()
    from backend.services.push_notifications import telegram_bot_ready

    payload["smtp_ready"] = smtp_ready(settings)
    payload["smtp_enabled"] = bool(settings.get("smtp_enabled"))
    payload["bot_ready"] = telegram_bot_ready(settings)
    payload["quiet_hours"] = is_in_quiet_hours(settings)
    return payload


@router.post("/alerts/test", response_model=AlertTestResponse)
def test_alert_mail(current_user: User = Depends(get_current_user)):
    settings = get_config_service().get_global_settings()
    if not smtp_ready(settings):
        return AlertTestResponse(
            success=False,
            message=(
                "SMTP 未配置完整：请先填写主机、发件人和通知邮箱；"
                "填写用户名时还需要密码"
            ),
        )
    try:
        send_smtp_mail(
            subject="[SignPulse] 告警测试",
            body="这是一封测试邮件。SMTP 配置正常，失败告警将发送到此邮箱。",
            settings=settings,
        )
        return AlertTestResponse(success=True, message="测试邮件已发送")
    except Exception as exc:
        logger.warning("SMTP 测试失败: %s", exc)
        return AlertTestResponse(success=False, message=f"发送失败: {exc}")
