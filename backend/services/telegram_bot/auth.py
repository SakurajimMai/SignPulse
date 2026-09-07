"""Authentication primitives for the Telegram Mini App and Bot operator."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping
from urllib.parse import parse_qsl

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError

from backend.core.config import get_settings
from backend.services.config import get_config_service
from backend.utils.time import utc_now

from .api import bot_token_from_settings
from .ids import allowed_user_ids_from_settings

MINI_APP_ISSUER = "tg-signpulse"
MINI_APP_AUDIENCE = "telegram-mini-app"
MINI_APP_SCOPE = "miniapp:operator"
MINI_APP_SESSION_SECONDS = 3600
# Telegram may reuse initData when a minimized WebView is restored. Align the
# exchange window with the session it creates so normal resumes remain usable.
INIT_DATA_MAX_AGE_SECONDS = MINI_APP_SESSION_SECONDS
INIT_DATA_MAX_LENGTH = 8192
INIT_DATA_MAX_FIELDS = 64
AUTH_DATE_FUTURE_TOLERANCE_SECONDS = 30

_mini_bearer = HTTPBearer(auto_error=False)


class MiniAppAuthError(ValueError):
    """Base error for invalid or unauthorized Telegram Mini App data."""


class InvalidInitDataError(MiniAppAuthError):
    """The initData payload is missing, stale, malformed, or has a bad signature."""


class TelegramOperatorNotAllowedError(MiniAppAuthError):
    """The Telegram user is authenticated but not present in the allowlist."""


@dataclass(frozen=True)
class TelegramOperator:
    user_id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""
    photo_url: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.user_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "username": self.username,
            "language_code": self.language_code,
            "photo_url": self.photo_url,
        }


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _parse_init_data(init_data: str) -> tuple[dict[str, str], str]:
    if not isinstance(init_data, str):
        raise InvalidInitDataError("initData must be a string")
    if not init_data or len(init_data) > INIT_DATA_MAX_LENGTH or "\x00" in init_data:
        raise InvalidInitDataError("invalid initData length")
    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=INIT_DATA_MAX_FIELDS,
        )
    except (ValueError, TypeError) as exc:
        raise InvalidInitDataError("malformed initData") from exc
    if not pairs:
        raise InvalidInitDataError("empty initData")

    fields: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in fields:
            raise InvalidInitDataError("duplicate or empty initData field")
        fields[key] = value
    supplied_hash = fields.pop("hash", "").strip().lower()
    if len(supplied_hash) != 64:
        raise InvalidInitDataError("missing or invalid initData hash")
    try:
        bytes.fromhex(supplied_hash)
    except ValueError as exc:
        raise InvalidInitDataError("invalid initData hash") from exc
    return fields, supplied_hash


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: int | float | None = None,
    max_age_seconds: int = INIT_DATA_MAX_AGE_SECONDS,
) -> TelegramOperator:
    """Validate Telegram WebApp initData and return its signed user identity."""
    token = str(bot_token or "").strip()
    if not token:
        raise InvalidInitDataError("Telegram Bot is not configured")
    fields, supplied_hash = _parse_init_data(init_data)
    data_check_string = "\n".join(
        f"{key}={fields[key]}" for key in sorted(fields)
    )
    secret_key = hmac.new(
        b"WebAppData", token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, supplied_hash):
        raise InvalidInitDataError("invalid initData signature")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidInitDataError("missing or invalid auth_date") from exc
    current = int(time.time() if now is None else now)
    if auth_date > current + AUTH_DATE_FUTURE_TOLERANCE_SECONDS:
        raise InvalidInitDataError("initData auth_date is in the future")
    if current - auth_date > max(1, int(max_age_seconds)):
        raise InvalidInitDataError("initData has expired")

    raw_user = fields.get("user", "")
    try:
        user = json.loads(raw_user)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InvalidInitDataError("missing or invalid user") from exc
    if not isinstance(user, dict) or user.get("is_bot") is True:
        raise InvalidInitDataError("invalid Telegram user")
    try:
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidInitDataError("missing or invalid Telegram user id") from exc
    if not 0 < user_id < 2**63:
        raise InvalidInitDataError("invalid Telegram user id")

    return TelegramOperator(
        user_id=user_id,
        first_name=str(user.get("first_name") or "")[:128],
        last_name=str(user.get("last_name") or "")[:128],
        username=str(user.get("username") or "")[:64],
        language_code=str(user.get("language_code") or "")[:16],
        photo_url=str(user.get("photo_url") or "")[:1024],
    )


def authenticate_init_data(
    init_data: str,
    settings: Mapping[str, Any] | None = None,
    *,
    now: int | float | None = None,
) -> TelegramOperator:
    current = settings or get_config_service().get_global_settings()
    operator = verify_init_data(
        init_data,
        bot_token_from_settings(current),
        now=now,
    )
    if operator.user_id not in allowed_user_ids_from_settings(current):
        raise TelegramOperatorNotAllowedError("Telegram user is not allowed")
    return operator


def create_mini_app_access_token(
    operator: TelegramOperator,
    *,
    bot_token: str,
    expires_seconds: int = MINI_APP_SESSION_SECONDS,
) -> str:
    issued_at = utc_now()
    expires_at = issued_at + timedelta(seconds=max(60, int(expires_seconds)))
    claims = {
        "iss": MINI_APP_ISSUER,
        "aud": MINI_APP_AUDIENCE,
        "sub": f"telegram:{operator.user_id}",
        "scope": MINI_APP_SCOPE,
        "tg_user_id": operator.user_id,
        "tg_first_name": operator.first_name,
        "tg_last_name": operator.last_name,
        "tg_username": operator.username,
        "tg_language_code": operator.language_code,
        "tg_photo_url": operator.photo_url,
        "bot_fp": _token_fingerprint(bot_token),
        "iat": issued_at,
        "exp": expires_at,
        "jti": secrets.token_urlsafe(18),
    }
    return jwt.encode(claims, get_settings().secret_key, algorithm="HS256")


def decode_mini_app_access_token(token: str) -> TelegramOperator:
    try:
        claims = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=["HS256"],
            audience=MINI_APP_AUDIENCE,
            issuer=MINI_APP_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "scope"]},
        )
    except PyJWTError as exc:
        raise InvalidInitDataError("invalid Mini App session") from exc
    if claims.get("scope") != MINI_APP_SCOPE:
        raise InvalidInitDataError("invalid Mini App scope")
    try:
        user_id = int(claims["tg_user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidInitDataError("invalid Mini App operator") from exc
    if claims.get("sub") != f"telegram:{user_id}" or user_id <= 0:
        raise InvalidInitDataError("invalid Mini App subject")
    return TelegramOperator(
        user_id=user_id,
        first_name=str(claims.get("tg_first_name") or "")[:128],
        last_name=str(claims.get("tg_last_name") or "")[:128],
        username=str(claims.get("tg_username") or "")[:64],
        language_code=str(claims.get("tg_language_code") or "")[:16],
        photo_url=str(claims.get("tg_photo_url") or "")[:1024],
    )


def get_current_mini_app_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(_mini_bearer),
) -> TelegramOperator:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MINI_APP_NOT_AUTHENTICATED",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        operator = decode_mini_app_access_token(credentials.credentials)
    except MiniAppAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MINI_APP_SESSION_INVALID",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    current = get_config_service().get_global_settings()
    current_token = bot_token_from_settings(current)
    try:
        claims = jwt.decode(
            credentials.credentials,
            get_settings().secret_key,
            algorithms=["HS256"],
            audience=MINI_APP_AUDIENCE,
            issuer=MINI_APP_ISSUER,
        )
    except PyJWTError as exc:  # defensive: decode above already succeeded
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MINI_APP_SESSION_INVALID",
        ) from exc
    if not current_token or not hmac.compare_digest(
        str(claims.get("bot_fp") or ""), _token_fingerprint(current_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MINI_APP_SESSION_REVOKED",
        )
    if operator.user_id not in allowed_user_ids_from_settings(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MINI_APP_OPERATOR_NOT_ALLOWED",
        )
    return operator
