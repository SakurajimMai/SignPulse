from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend.services.telegram_bot import auth
from backend.services.telegram_bot.ids import notification_targets

BOT_TOKEN = "123456:test-bot-token"
SECRET_KEY = "mini-app-test-secret-key"


def signed_init_data(
    *,
    user_id: int = 42,
    auth_date: int = 1_800_000_000,
    bot_token: str = BOT_TOKEN,
) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAE-test-query",
        "user": json.dumps(
            {
                "id": user_id,
                "first_name": "Ada",
                "username": "ada_ops",
                "language_code": "en",
            },
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _settings(**overrides) -> dict:
    current = {
        "telegram_bot_token": BOT_TOKEN,
        "telegram_bot_allowed_user_ids": [42],
    }
    current.update(overrides)
    return current


def test_verify_init_data_accepts_official_signature_and_rejects_tampering():
    data = signed_init_data()
    operator = auth.verify_init_data(data, BOT_TOKEN, now=1_800_000_001)
    assert operator.user_id == 42
    assert operator.username == "ada_ops"

    with pytest.raises(auth.InvalidInitDataError):
        auth.verify_init_data(data.replace("ada_ops", "root_ops"), BOT_TOKEN, now=1_800_000_001)


def test_verify_init_data_rejects_expired_future_and_duplicate_fields():
    assert (
        auth.verify_init_data(
            signed_init_data(),
            BOT_TOKEN,
            now=1_800_000_000 + auth.INIT_DATA_MAX_AGE_SECONDS,
        ).user_id
        == 42
    )
    with pytest.raises(auth.InvalidInitDataError, match="expired"):
        auth.verify_init_data(
            signed_init_data(),
            BOT_TOKEN,
            now=1_800_000_001 + auth.INIT_DATA_MAX_AGE_SECONDS,
        )
    with pytest.raises(auth.InvalidInitDataError, match="future"):
        auth.verify_init_data(
            signed_init_data(auth_date=1_800_000_100), BOT_TOKEN, now=1_800_000_000
        )
    with pytest.raises(auth.InvalidInitDataError, match="duplicate"):
        auth.verify_init_data(
            signed_init_data() + "&auth_date=1800000000",
            BOT_TOKEN,
            now=1_800_000_001,
        )


def test_authenticate_init_data_enforces_allowlist():
    data = signed_init_data()
    assert auth.authenticate_init_data(data, _settings(), now=1_800_000_001).user_id == 42
    with pytest.raises(auth.TelegramOperatorNotAllowedError):
        auth.authenticate_init_data(
            data,
            _settings(telegram_bot_allowed_user_ids=[99]),
            now=1_800_000_001,
        )


def test_mini_app_jwt_has_independent_contract_and_live_revocation(monkeypatch):
    current = _settings()
    fake_config = SimpleNamespace(get_global_settings=lambda: current)
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(secret_key=SECRET_KEY))
    monkeypatch.setattr(auth, "get_config_service", lambda: fake_config)

    token = auth.create_mini_app_access_token(
        auth.TelegramOperator(user_id=42, first_name="Ada"), bot_token=BOT_TOKEN
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    assert auth.get_current_mini_app_operator(credentials).user_id == 42

    current["telegram_bot_allowed_user_ids"] = [99]
    with pytest.raises(HTTPException) as denied:
        auth.get_current_mini_app_operator(credentials)
    assert denied.value.status_code == 403

    current["telegram_bot_allowed_user_ids"] = [42]
    current["telegram_bot_token"] = "999:rotated"
    with pytest.raises(HTTPException) as revoked:
        auth.get_current_mini_app_operator(credentials)
    assert revoked.value.status_code == 401
    assert revoked.value.detail == "MINI_APP_SESSION_REVOKED"

    admin_like = jwt.encode(
        {
            "iss": "tg-signpulse",
            "aud": "telegram-mini-app",
            "sub": "admin",
            "scope": "admin",
            "iat": 1_800_000_000,
            "exp": 4_000_000_000,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(auth.InvalidInitDataError):
        auth.decode_mini_app_access_token(admin_like)


def test_notification_targets_prefer_user_ids():
    assert notification_targets(
        {"telegram_bot_allowed_user_ids": [42, 7], "telegram_bot_chat_id": "-1001"}
    ) == [("7", None), ("42", None), ("-1001", None)]
    assert notification_targets({"telegram_bot_allowed_user_ids": [42]}) == [("42", None)]
    assert notification_targets(
        {
            "telegram_bot_chat_id": "-1001",
            "telegram_bot_message_thread_id": "9",
        }
    ) == [("-1001", 9)]
