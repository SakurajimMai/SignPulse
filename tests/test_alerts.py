from __future__ import annotations

import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core import config as config_module
from backend.core.auth import create_access_token
from backend.services.alerts import config as alerts_config
from backend.services.alerts.config import (
    SCHEMA_VERSION,
    STATE_KEY_LIMIT,
    load_alerts_state,
    public_alerts,
    save_alert_rules,
    save_alerts_state,
)
from backend.services.alerts.dispatcher import _fingerprint, fire_alert
from backend.services.alerts.rules import RULE_IDS
from backend.services.alerts.smtp import send_smtp_mail, smtp_ready, smtp_settings
from backend.utils.atomic_io import read_json_safe, write_json_atomic
from tg_signer.security import is_encrypted_secret


def _write_alert_state_keys(data_dir: str, prefix: str, count: int) -> None:
    os.environ["APP_DATA_DIR"] = data_dir
    config_module.get_settings.cache_clear()

    for index in range(count):
        key = f"{prefix}-{index}"

        def _add_key(state: dict, *, key: str = key) -> None:
            # Widen the read/modify/write race so this fails reliably without
            # the inter-process file lock.
            time.sleep(0.005)
            state["last_sent"][key] = "2026-01-01T00:00:00Z"

        alerts_config.mutate_alerts_state(_add_key)


def _auth_headers() -> dict:
    token = create_access_token(
        {"sub": "admin"},
        expires_delta=timedelta(hours=1),
    )
    return {"Authorization": f"Bearer {token}"}


def _smtp_settings(**over) -> dict:
    data = {
        "smtp_enabled": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_encryption": "ssl",
        "smtp_username": "from@example.com",
        "smtp_password": "secret",
        "smtp_from": "from@example.com",
        "smtp_notify_email": "ops@example.com",
        "telegram_bot_quiet_hours_enabled": False,
    }
    data.update(over)
    return data


class _FakeConfig:
    def __init__(self, settings: dict):
        self._settings = settings

    def get_global_settings(self) -> dict:
        return self._settings

    def get_proxy_runtime_settings(self) -> dict:
        return {"enabled": False, "url": None, "no_proxy": "localhost"}


def test_smtp_settings_parse_recipients_and_ready():
    cfg = smtp_settings(_smtp_settings(smtp_notify_email="a@x.com，b@y.com; c@z.com"))
    assert cfg["recipients"] == ["a@x.com", "b@y.com", "c@z.com"]
    assert smtp_ready(_smtp_settings()) is True
    assert smtp_ready(_smtp_settings(smtp_host="")) is False
    assert smtp_ready(_smtp_settings(smtp_enabled=False)) is False
    assert smtp_ready(_smtp_settings(smtp_notify_email="")) is False
    assert smtp_ready(_smtp_settings(smtp_password="")) is False
    assert (
        smtp_ready(
            _smtp_settings(
                smtp_username="", smtp_password="", smtp_from="from@example.com"
            )
        )
        is True
    )


def _refresh_settings(isolated_env: Path) -> Path:
    config_module.get_settings.cache_clear()
    return isolated_env


def test_alert_state_path_uses_resolved_writable_base(tmp_path: Path):
    resolved = tmp_path / "fallback-data"
    fake = SimpleNamespace(
        data_dir=Path("/data"),
        resolve_base_dir=lambda: resolved,
    )
    with patch.object(alerts_config, "get_settings", return_value=fake):
        assert alerts_config._alerts_file() == resolved / ".alerts_config.json"
        assert alerts_config._alerts_lock().lock_file == str(
            resolved / ".alerts_config.json.lock"
        )


def test_public_alerts_defaults(isolated_env: Path):
    _refresh_settings(isolated_env)
    payload = public_alerts()
    ids = {item["id"] for item in payload["rules"]}
    assert ids == RULE_IDS
    assert all(item["enabled"] for item in payload["rules"])
    assert all(item["email_enabled"] for item in payload["rules"])
    assert all(item["telegram_enabled"] for item in payload["rules"])
    assert {item["severity"] for item in payload["rules"]} <= {
        "critical",
        "warning",
        "info",
    }
    assert payload["recent"] == []


def test_save_alert_rules_clamp_and_list(isolated_env: Path):
    _refresh_settings(isolated_env)
    payload = save_alert_rules(
        {
            "rules": [
                {
                    "id": "games_site_publish_fail",
                    "enabled": False,
                    "email_enabled": False,
                    "telegram_enabled": True,
                    "cooldown_minutes": 99999,
                }
            ]
        }
    )
    item = next(
        r for r in payload["rules"] if r["id"] == "games_site_publish_fail"
    )
    assert item["enabled"] is False
    assert item["email_enabled"] is False
    assert item["telegram_enabled"] is True
    assert item["cooldown_minutes"] == 1440


def test_schema_v2_migrates_legacy_rules_and_delivery_maps_idempotently(
    isolated_env: Path,
):
    _refresh_settings(isolated_env)
    digest = "a" * 64
    legacy = {
        "schema_version": 1,
        "rules": {
            "sign_task_fail": {"enabled": False, "cooldown_minutes": 77},
            "sign_task_ai_fail": {"enabled": True, "cooldown_minutes": 12},
        },
        "last_sent": {f"sign_task_fail:{digest}": "2026-08-31T00:00:00Z"},
        "last_attempt": {f"sign_task_fail:{digest}": "2026-08-31T00:00:01Z"},
        "in_flight": {
            f"sign_task_fail:{digest}": {
                "token": "legacy-token",
                "at": "2026-08-31T00:00:02Z",
            }
        },
        "recent": [],
    }
    write_json_atomic(alerts_config._alerts_file(), legacy)

    first = load_alerts_state()
    second = load_alerts_state()

    assert first == second
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["rules"]["sign_task_ai_fail"]["enabled"] is True
    assert first["rules"]["sign_task_ai_fail"]["cooldown_minutes"] == 12
    for rule_id in (
        "sign_task_rate_limited",
        "sign_task_network_fail",
        "sign_task_timeout",
        "sign_task_flow_fail",
    ):
        assert first["rules"][rule_id]["enabled"] is False
        assert first["rules"][rule_id]["cooldown_minutes"] == 77
    for rule_id in (
        "sign_task_rate_limited",
        "sign_task_ai_fail",
        "sign_task_network_fail",
        "sign_task_timeout",
        "sign_task_flow_fail",
    ):
        email_key = f"{rule_id}:{digest}:email"
        telegram_key = f"{rule_id}:{digest}:telegram"
        assert email_key in first["last_sent"]
        assert email_key in first["last_attempt"]
        assert email_key in first["in_flight"]
        assert telegram_key not in first["last_sent"]
        assert telegram_key not in first["last_attempt"]
        assert telegram_key not in first["in_flight"]
    assert read_json_safe(alerts_config._alerts_file(), {})["schema_version"] == 2


@pytest.mark.parametrize(
    ("rule_id", "global_enabled", "task_enabled"),
    [
        ("sign_task_rate_limited", False, True),
        ("sign_task_ai_fail", True, False),
        ("sign_task_network_fail", False, False),
        ("sign_task_timeout", False, True),
        ("sign_task_flow_fail", True, False),
    ],
)
def test_sign_task_legacy_switches_only_suppress_telegram(
    isolated_env: Path,
    rule_id: str,
    global_enabled: bool,
    task_enabled: bool,
):
    _refresh_settings(isolated_env)
    settings = _smtp_settings(
        telegram_bot_notify_enabled=True,
        telegram_bot_task_failure_enabled=global_enabled,
        telegram_bot_token="123:token",
        telegram_bot_chat_id="-100123",
    )
    emails: list[dict] = []
    telegram: list[dict] = []
    with (
        patch(
            "backend.services.config.get_config_service",
            lambda: _FakeConfig(settings),
        ),
        patch(
            "backend.services.push_notifications.telegram_bot_ready",
            return_value=True,
        ),
        patch(
            "backend.services.alerts.dispatcher.send_smtp_mail",
            lambda **kwargs: emails.append(kwargs),
        ),
        patch(
            "backend.services.push_notifications.send_telegram_bot_message_sync",
            lambda **kwargs: telegram.append(kwargs),
        ),
    ):
        result = fire_alert(
            rule_id,
            title="sign failed",
            fingerprint=rule_id,
            channel_overrides={"telegram": task_enabled},
        )

    assert result["status"] == "sent"
    assert len(emails) == 1
    assert telegram == []
    assert next(
        item for item in result["deliveries"] if item["channel"] == "telegram"
    ) == {
        "channel": "telegram",
        "status": "skipped",
        "reason": "context_disabled",
    }


@pytest.mark.parametrize(
    ("global_enabled", "task_enabled"),
    [(False, True), (True, False)],
)
def test_account_login_legacy_switches_only_suppress_telegram(
    isolated_env: Path,
    global_enabled: bool,
    task_enabled: bool,
):
    _refresh_settings(isolated_env)
    settings = _smtp_settings(
        telegram_bot_notify_enabled=True,
        telegram_bot_login_notify_enabled=global_enabled,
        telegram_bot_token="123:token",
        telegram_bot_chat_id="-100123",
    )
    emails: list[dict] = []
    telegram: list[dict] = []
    with (
        patch(
            "backend.services.config.get_config_service",
            lambda: _FakeConfig(settings),
        ),
        patch(
            "backend.services.push_notifications.telegram_bot_ready",
            return_value=True,
        ),
        patch(
            "backend.services.alerts.dispatcher.send_smtp_mail",
            lambda **kwargs: emails.append(kwargs),
        ),
        patch(
            "backend.services.push_notifications.send_telegram_bot_message_sync",
            lambda **kwargs: telegram.append(kwargs),
        ),
    ):
        result = fire_alert(
            "account_login_invalid",
            title="account expired",
            fingerprint="account-a",
            channel_overrides={"telegram": task_enabled},
        )

    assert result["status"] == "sent"
    assert len(emails) == 1
    assert telegram == []
    assert next(
        item for item in result["deliveries"] if item["channel"] == "telegram"
    ) == {
        "channel": "telegram",
        "status": "skipped",
        "reason": "context_disabled",
    }


def test_fire_alert_sends_and_respects_cooldown(isolated_env: Path):
    _refresh_settings(isolated_env)
    sent: list[dict] = []

    def _send(**kwargs):
        sent.append(kwargs)

    with (
        patch(
            "backend.services.config.get_config_service",
            lambda: _FakeConfig(_smtp_settings()),
        ),
        patch("backend.services.alerts.dispatcher.send_smtp_mail", _send),
    ):
        first = fire_alert(
            "games_site_publish_fail",
            title="游戏发布失败：demo",
            detail="wp down",
            fingerprint="job-1",
        )
        second = fire_alert(
            "games_site_publish_fail",
            title="游戏发布失败：demo",
            detail="wp down again",
            fingerprint="job-1",
        )
    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert next(
        item for item in second["deliveries"] if item["channel"] == "email"
    )["reason"] == "cooldown"
    assert len(sent) == 1
    assert "demo" in sent[0]["subject"]
    recent = public_alerts()["recent"]
    assert any(item["status"] == "sent" for item in recent)
    assert recent[-1]["status"] == "skipped"
    assert recent[-1]["reason"] == "no_deliverable_channel"
    assert recent[-1]["rule_id"] == "games_site_publish_fail"


def test_channels_record_partial_and_retry_independently(isolated_env: Path):
    _refresh_settings(isolated_env)
    settings = _smtp_settings(
        telegram_bot_notify_enabled=True,
        telegram_bot_login_notify_enabled=True,
        telegram_bot_token="123:token",
        telegram_bot_chat_id="-100123",
    )
    emails: list[dict] = []
    telegram_attempts = 0

    def _telegram_fail(**_kwargs):
        nonlocal telegram_attempts
        telegram_attempts += 1
        raise RuntimeError("telegram down")

    with (
        patch(
            "backend.services.config.get_config_service",
            lambda: _FakeConfig(settings),
        ),
        patch(
            "backend.services.alerts.dispatcher.send_smtp_mail",
            lambda **kwargs: emails.append(kwargs),
        ),
        patch(
            "backend.services.push_notifications.send_telegram_bot_message_sync",
            _telegram_fail,
        ),
    ):
        first = fire_alert(
            "account_login_invalid",
            title="account expired",
            fingerprint="account-a",
        )
        second = fire_alert(
            "account_login_invalid",
            title="account expired again",
            fingerprint="account-a",
        )

    assert first["status"] == "partial"
    assert {item["channel"]: item["status"] for item in first["deliveries"]} == {
        "email": "sent",
        "telegram": "failed",
    }
    assert second["status"] == "skipped"
    assert {item["channel"]: item["reason"] for item in second["deliveries"]} == {
        "email": "cooldown",
        "telegram": "retry_backoff",
    }
    assert len(emails) == 1
    assert telegram_attempts == 1
    state = load_alerts_state()
    assert any(key.endswith(":email") for key in state["last_sent"])
    assert not any(key.endswith(":telegram") for key in state["last_sent"])
    assert state["recent"][-2]["status"] == "partial"


def test_fire_alert_skips_when_disabled_or_smtp_missing(isolated_env: Path):
    _refresh_settings(isolated_env)
    save_alert_rules({"manga_ingest_worker_fail": {"enabled": False}})
    with patch(
        "backend.services.config.get_config_service",
        lambda: _FakeConfig(_smtp_settings()),
    ):
        skipped = fire_alert("manga_ingest_worker_fail", title="x")
    assert skipped["status"] == "skipped"
    assert skipped["reason"] == "disabled"

    with patch(
        "backend.services.config.get_config_service",
        lambda: _FakeConfig(_smtp_settings(smtp_enabled=False)),
    ):
        missing = fire_alert("games_cloud_fail", title="x")
    assert missing["reason"] == "no_deliverable_channel"
    assert {item["reason"] for item in missing["deliveries"]} == {
        "email_not_ready",
        "telegram_not_ready",
    }
    recent = public_alerts()["recent"]
    assert [item.get("reason") for item in recent[-2:]] == [
        "disabled",
        "no_deliverable_channel",
    ]


def test_fire_alert_concurrent_same_fingerprint_only_sends_once(isolated_env: Path):
    _refresh_settings(isolated_env)
    send_started = threading.Event()
    release_send = threading.Event()
    sent: list[str] = []

    def _send(**kwargs):
        sent.append(kwargs["subject"])
        send_started.set()
        assert release_send.wait(timeout=5)

    with (
        patch(
            "backend.services.config.get_config_service",
            lambda: _FakeConfig(_smtp_settings()),
        ),
        patch("backend.services.alerts.dispatcher.send_smtp_mail", _send),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(
            fire_alert,
            "games_site_publish_fail",
            title="first",
            fingerprint="same-job",
        )
        assert send_started.wait(timeout=5)
        second = pool.submit(
            fire_alert,
            "games_site_publish_fail",
            title="second",
            fingerprint="same-job",
        )
        try:
            second_result = second.result(timeout=5)
        finally:
            release_send.set()
        first_result = first.result(timeout=5)

    assert first_result["status"] == "sent"
    assert second_result["status"] == "skipped"
    email_delivery = next(
        item for item in second_result["deliveries"] if item["channel"] == "email"
    )
    assert email_delivery["reason"] == "in_flight"
    assert len(sent) == 1
    recent = public_alerts()["recent"]
    assert {item["status"] for item in recent} == {"sent", "skipped"}


def test_concurrent_events_merge_state_and_preserve_rule_save(isolated_env: Path):
    _refresh_settings(isolated_env)
    both_started = threading.Event()
    release_send = threading.Event()
    counter_lock = threading.Lock()
    started = 0

    def _send(**_kwargs):
        nonlocal started
        with counter_lock:
            started += 1
            if started == 2:
                both_started.set()
        assert release_send.wait(timeout=5)

    with (
        patch(
            "backend.services.config.get_config_service",
            lambda: _FakeConfig(_smtp_settings()),
        ),
        patch("backend.services.alerts.dispatcher.send_smtp_mail", _send),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        one = pool.submit(
            fire_alert,
            "games_site_publish_fail",
            title="one",
            fingerprint="job-one",
        )
        two = pool.submit(
            fire_alert,
            "games_cloud_fail",
            title="two",
            fingerprint="job-two",
        )
        try:
            assert both_started.wait(timeout=5)
            save_alert_rules({"manga_hmw_fail": {"enabled": False}})
        finally:
            release_send.set()
        assert one.result(timeout=5)["status"] == "sent"
        assert two.result(timeout=5)["status"] == "sent"

    state = load_alerts_state()
    assert state["rules"]["manga_hmw_source_fail"]["enabled"] is False
    assert state["rules"]["manga_hmw_convert_fail"]["enabled"] is False
    assert state["rules"]["manga_hmw_storage_fail"]["enabled"] is False
    assert len(state["last_sent"]) == 2
    sent_rules = {
        item["rule_id"] for item in state["recent"] if item["status"] == "sent"
    }
    assert sent_rules == {"games_site_publish_fail", "games_cloud_fail"}


def test_alert_state_transaction_is_safe_across_processes(isolated_env: Path):
    _refresh_settings(isolated_env)
    process_count = 3
    keys_per_process = 8
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_write_alert_state_keys,
            args=(str(isolated_env), f"worker-{index}", keys_per_process),
        )
        for index in range(process_count)
    ]

    started_processes = []
    try:
        for process in processes:
            process.start()
            started_processes.append(process)
        for process in started_processes:
            process.join(timeout=20)
            assert not process.is_alive()
            assert process.exitcode == 0
    finally:
        for process in started_processes:
            if process.is_alive():
                process.terminate()
        for process in started_processes:
            process.join(timeout=5)

    keys = set(load_alerts_state()["last_sent"])
    expected = {
        f"worker-{worker}-{index}"
        for worker in range(process_count)
        for index in range(keys_per_process)
    }
    assert expected <= keys


def test_failed_send_uses_short_retry_backoff_and_is_observable(isolated_env: Path):
    _refresh_settings(isolated_env)
    attempts = 0

    def _fail(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("smtp down")

    with (
        patch(
            "backend.services.config.get_config_service",
            lambda: _FakeConfig(_smtp_settings()),
        ),
        patch("backend.services.alerts.dispatcher.send_smtp_mail", _fail),
    ):
        first = fire_alert(
            "games_site_publish_fail", title="one", fingerprint="failed-job"
        )
        second = fire_alert(
            "games_site_publish_fail", title="two", fingerprint="failed-job"
        )

    assert first["status"] == "failed"
    assert first["error"] == "smtp down"
    assert second["status"] == "skipped"
    assert next(
        item for item in second["deliveries"] if item["channel"] == "email"
    )["reason"] == "retry_backoff"
    assert attempts == 1
    recent = public_alerts()["recent"]
    assert recent[-2]["status"] == "failed"
    assert next(
        item
        for item in recent[-1]["deliveries"]
        if item["channel"] == "email"
    )["reason"] == "retry_backoff"


def test_fingerprint_hashes_full_value_and_state_maps_are_bounded(isolated_env: Path):
    _refresh_settings(isolated_env)
    prefix = "x" * 120
    assert _fingerprint("games_site_publish_fail", prefix + "a") != _fingerprint(
        "games_site_publish_fail", prefix + "b"
    )

    oversized = {
        f"key-{index}": f"2026-01-01T00:00:00.{index:06d}Z"
        for index in range(STATE_KEY_LIMIT + 25)
    }
    save_alerts_state(
        {
            "rules": {},
            "last_sent": oversized,
            "last_attempt": oversized,
            "recent": [],
        }
    )
    state = load_alerts_state()
    assert len(state["last_sent"]) == STATE_KEY_LIMIT
    assert len(state["last_attempt"]) == STATE_KEY_LIMIT


def test_quiet_hours_skip_is_recorded(isolated_env: Path):
    _refresh_settings(isolated_env)
    sent_email: list[dict] = []
    sent_bot: list[dict] = []
    with (
        patch(
            "backend.services.config.get_config_service",
            lambda: _FakeConfig(_smtp_settings()),
        ),
        patch(
            "backend.services.push_notifications.is_in_quiet_hours", return_value=True
        ),
        patch(
            "backend.services.push_notifications.telegram_bot_ready",
            return_value=True,
        ),
        patch(
            "backend.services.alerts.dispatcher.send_smtp_mail",
            lambda **kwargs: sent_email.append(kwargs),
        ),
        patch(
            "backend.services.push_notifications.send_telegram_bot_message_sync",
            lambda **kwargs: sent_bot.append(kwargs),
        ),
    ):
        result = fire_alert("manga_site_fail", title="quiet failure")
    assert result["status"] == "sent"
    assert sent_email and not sent_bot
    telegram = next(
        item for item in result["deliveries"] if item["channel"] == "telegram"
    )
    assert telegram == {
        "channel": "telegram",
        "status": "skipped",
        "reason": "quiet_hours",
    }
    assert public_alerts()["recent"][-1]["deliveries"] == result["deliveries"]


def test_smtp_subject_strips_header_newlines():
    class _Server:
        message = None

        def ehlo(self):
            return None

        def login(self, _username, _password):
            return None

        def send_message(self, message):
            self.message = message

        def quit(self):
            return None

    server = _Server()
    with patch("backend.services.alerts.smtp.smtplib.SMTP_SSL", return_value=server):
        send_smtp_mail(
            subject="failure\r\nBcc: injected@example.com",
            body="body",
            settings=_smtp_settings(),
        )
    assert server.message is not None
    assert str(server.message["Subject"]) == "failure Bcc: injected@example.com"


def test_alerts_api_get_put_and_test(client, db_session, isolated_env: Path):
    headers = _auth_headers()
    assert client.get("/api/alerts").status_code in (401, 403)
    got = client.get("/api/alerts", headers=headers)
    assert got.status_code == 200
    body = got.json()
    assert "rules" in body
    assert body["smtp_ready"] is False
    assert body["bot_ready"] is False

    saved = client.put(
        "/api/alerts",
        json={
            "rules": [
                {"id": "cloud_login_invalid", "enabled": False, "cooldown_minutes": 90}
            ]
        },
        headers=headers,
    )
    assert saved.status_code == 200
    migrated = {
        item["id"]: item
        for item in saved.json()["rules"]
        if item["id"] in {"cloud_auth_invalid", "cloud_keepalive_fail"}
    }
    assert set(migrated) == {"cloud_auth_invalid", "cloud_keepalive_fail"}
    assert all(item["enabled"] is False for item in migrated.values())
    assert all(item["cooldown_minutes"] == 90 for item in migrated.values())

    test_resp = client.post("/api/alerts/test", headers=headers)
    assert test_resp.status_code == 200
    assert test_resp.json()["success"] is False


def test_smtp_password_encrypted_and_masked(client, db_session, isolated_env: Path):
    headers = _auth_headers()
    resp = client.post(
        "/api/config/settings",
        json={
            "smtp_enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_encryption": "starttls",
            "smtp_username": "from@example.com",
            "smtp_password": "plain-smtp-secret",
            "smtp_from": "from@example.com",
            "smtp_notify_email": "ops@example.com",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    got = client.get("/api/config/settings", headers=headers).json()
    assert got["smtp_password"] is None
    assert got["smtp_password_set"] is True
    assert got["smtp_host"] == "smtp.example.com"
    assert got["smtp_encryption"] == "starttls"
    from backend.services.config import get_config_service

    svc = get_config_service()
    stored = svc.get_global_settings()
    assert is_encrypted_secret(stored["smtp_password"])
    raw = svc._get_global_settings_file().read_text(encoding="utf-8")
    assert "plain-smtp-secret" not in raw

    keep = client.post(
        "/api/config/settings",
        json={"smtp_host": "smtp2.example.com", "smtp_password": ""},
        headers=headers,
    )
    assert keep.status_code == 200
    again = get_config_service().get_global_settings()
    assert again["smtp_host"] == "smtp2.example.com"
    assert is_encrypted_secret(again["smtp_password"])
