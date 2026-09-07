from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.api.routes.alerts import AlertsSaveRequest


def _rule(**overrides):
    payload = {
        "id": "games_site_publish_fail",
        "enabled": True,
        "email_enabled": True,
        "telegram_enabled": False,
        "cooldown_minutes": 30,
    }
    payload.update(overrides)
    return payload


def test_alert_rule_update_accepts_strict_valid_payload():
    request = AlertsSaveRequest(rules=[_rule()])
    assert request.rules[0].id == "games_site_publish_fail"
    assert request.rules[0].enabled is True
    assert request.rules[0].email_enabled is True
    assert request.rules[0].telegram_enabled is False
    assert request.rules[0].cooldown_minutes == 30


@pytest.mark.parametrize(
    "rule",
    [
        _rule(id="unknown_rule"),
        _rule(enabled="false"),
        _rule(email_enabled="true"),
        _rule(telegram_enabled=1),
        _rule(cooldown_minutes="30"),
        _rule(cooldown_minutes=0),
        _rule(cooldown_minutes=1441),
        _rule(unexpected=True),
    ],
)
def test_alert_rule_update_rejects_invalid_values(rule):
    with pytest.raises(ValidationError):
        AlertsSaveRequest(rules=[rule])


def test_alert_rule_update_rejects_duplicate_ids_and_extra_request_fields():
    with pytest.raises(ValidationError):
        AlertsSaveRequest(rules=[_rule(), _rule(enabled=False)])
    with pytest.raises(ValidationError):
        AlertsSaveRequest(rules=[_rule()], unexpected=True)
    with pytest.raises(ValidationError):
        AlertsSaveRequest(rules=[])
