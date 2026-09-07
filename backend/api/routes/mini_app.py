"""Restricted API surface for the Telegram Mini App."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, constr

from backend.core.rate_limit import compose_rate_limit_key, get_rate_limiter
from backend.scheduler.instance_lock import has_scheduler_lock
from backend.services.config import get_config_service
from backend.services.control_plane import (
    ControlTaskAccountError,
    ControlTaskDisabledError,
    ControlTaskNotFoundError,
    IdempotencyConflictError,
    get_control_plane,
)
from backend.services.telegram_bot.api import bot_token_from_settings
from backend.services.telegram_bot.auth import (
    INIT_DATA_MAX_LENGTH,
    MINI_APP_SESSION_SECONDS,
    InvalidInitDataError,
    TelegramOperator,
    TelegramOperatorNotAllowedError,
    authenticate_init_data,
    create_mini_app_access_token,
    get_current_mini_app_operator,
)

router = APIRouter()
rate_limiter = get_rate_limiter()

InitDataString = constr(strict=True, min_length=1, max_length=INIT_DATA_MAX_LENGTH)
StorageName = constr(strict=True, min_length=1, max_length=128)
RequestId = constr(
    strict=True,
    min_length=8,
    max_length=128,
    regex=r"^[A-Za-z0-9_-]+$",
)
MINI_APP_PRIMARY_RETRY_AFTER_SECONDS = 3


class MiniAppAuthRequest(BaseModel):
    init_data: InitDataString

    class Config:
        extra = "forbid"


class MiniAppRunRequest(BaseModel):
    account_name: StorageName
    request_id: RequestId

    class Config:
        extra = "forbid"


def _require_control_enabled(settings: dict) -> None:
    if not settings.get("telegram_bot_control_enabled"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MINI_APP_DISABLED",
        )


def require_scheduler_primary() -> None:
    """Reject process-local task operations on a scheduler replica."""
    if not has_scheduler_lock():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MINI_APP_PRIMARY_UNAVAILABLE",
            headers={
                "Retry-After": str(MINI_APP_PRIMARY_RETRY_AFTER_SECONDS),
            },
        )


def _map_control_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ControlTaskNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="TASK_NOT_FOUND"
        )
    if isinstance(exc, IdempotencyConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="REQUEST_ID_TARGET_CONFLICT",
        )
    if isinstance(exc, ControlTaskDisabledError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="TASK_DISABLED",
        )
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=str(exc) or "TASK_ACCOUNT_INVALID",
    )


@router.post("/auth")
def authenticate_mini_app(body: MiniAppAuthRequest, request: Request):
    auth_key = compose_rate_limit_key(request, "telegram-mini-app")
    rate_limiter.hit(
        scope="mini_app.auth",
        key=auth_key,
        max_attempts=20,
        window_seconds=300,
        block_seconds=300,
        detail="MINI_APP_AUTH_RATE_LIMITED",
    )
    settings = get_config_service().get_global_settings()
    _require_control_enabled(settings)
    try:
        operator = authenticate_init_data(body.init_data, settings)
    except TelegramOperatorNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MINI_APP_OPERATOR_NOT_ALLOWED",
        ) from exc
    except InvalidInitDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MINI_APP_INIT_DATA_INVALID",
        ) from exc
    token = bot_token_from_settings(settings)
    access_token = create_mini_app_access_token(operator, bot_token=token)
    rate_limiter.reset("mini_app.auth", auth_key)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": MINI_APP_SESSION_SECONDS,
        "user": operator.public_dict(),
    }


@router.get("/bootstrap")
def mini_app_bootstrap(
    operator: TelegramOperator = Depends(get_current_mini_app_operator),
):
    del operator
    settings = get_config_service().get_global_settings()
    _require_control_enabled(settings)
    return get_control_plane().bootstrap()


@router.get("/alerts")
def mini_app_alerts(
    limit: int = Query(default=20, ge=1, le=40),
    operator: TelegramOperator = Depends(get_current_mini_app_operator),
):
    del operator
    settings = get_config_service().get_global_settings()
    _require_control_enabled(settings)
    return get_control_plane().recent_alerts(limit=limit)


@router.post("/tasks/{task_name}/run")
async def mini_app_run_task(
    body: MiniAppRunRequest,
    request: Request,
    task_name: str = Path(..., min_length=1, max_length=128),
    operator: TelegramOperator = Depends(get_current_mini_app_operator),
):
    settings = get_config_service().get_global_settings()
    _require_control_enabled(settings)
    require_scheduler_primary()
    run_key = compose_rate_limit_key(request, str(operator.user_id))
    rate_limiter.hit(
        scope="mini_app.run",
        key=run_key,
        max_attempts=8,
        window_seconds=60,
        block_seconds=60,
        detail="MINI_APP_RUN_RATE_LIMITED",
    )
    try:
        result = await get_control_plane().start_task_run(
            body.account_name,
            task_name,
            body.request_id,
            actor_id=f"mini-app:{operator.user_id}",
        )
    except (
        ControlTaskNotFoundError,
        ControlTaskAccountError,
        ControlTaskDisabledError,
        IdempotencyConflictError,
    ) as exc:
        raise _map_control_error(exc) from exc
    return {"request_id": body.request_id, **result}


@router.get("/tasks/{task_name}/run/status")
def mini_app_run_status(
    task_name: str = Path(..., min_length=1, max_length=128),
    account_name: str = Query(..., min_length=1, max_length=128),
    run_id: Optional[str] = Query(default=None, max_length=128),
    operator: TelegramOperator = Depends(get_current_mini_app_operator),
):
    del operator
    settings = get_config_service().get_global_settings()
    _require_control_enabled(settings)
    require_scheduler_primary()
    try:
        return get_control_plane().task_run_status(
            account_name, task_name, run_id=run_id
        )
    except (
        ControlTaskNotFoundError,
        ControlTaskAccountError,
        ControlTaskDisabledError,
    ) as exc:
        raise _map_control_error(exc) from exc
