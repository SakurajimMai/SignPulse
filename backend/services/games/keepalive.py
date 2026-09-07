from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from backend.services.alerts import schedule_alert
from backend.utils.atomic_io import read_json_safe, write_json_atomic
from backend.utils.http_error_text import looks_like_html, safe_response_snippet
from backend.utils.outbound import httpx_async_client_kwargs
from backend.utils.time import utc_now_iso_z

from .clouds import (
    CLOUD_TARGETS,
    QUARK_API_BASES,
    QUARK_UA,
    TERABOX_BASE,
    CloudError,
    _cookie_dict,
    _json_data,
    _pikpak_login,
    _quark_cookie_header,
    _quark_json,
    _terabox_json,
    _terabox_refresh_js_token,
    cloud_status,
    openlist_configured,
    persist_client_cookies,
)
from .config import GamesSettings, load_games_settings
from .paths import games_dirs

logger = logging.getLogger("backend.games.keepalive")

KEEP_INTERVAL_HOURS = 6
KEEPALIVE_JOB_ID = "games-cloud-keepalive"
_refresh_lock = asyncio.Lock()

_AUTH_FAILURE_MARKERS = (
    "unauthorized",
    "forbidden",
    "invalid_grant",
    "invalid refresh",
    "token expired",
    "token invalid",
    "cookie 失效",
    "cookie 无效",
    "重新填写 bduss",
    "登录失败",
    "登录失效",
    "凭据失效",
    "验证码拦截",
)


def _is_cloud_auth_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    texts: list[str] = []
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts.append(str(current).casefold())
        if isinstance(current, httpx.HTTPStatusError) and current.response.status_code in {
            401,
            403,
        }:
            return True
        current = current.__cause__ or current.__context__
    blob = "\n".join(texts)
    return any(marker in blob for marker in _AUTH_FAILURE_MARKERS)


def keepalive_state_path(settings: GamesSettings):
    return games_dirs(settings).root / "cloud_keepalive.json"


def load_keepalive_state(settings: GamesSettings | None = None) -> dict[str, Any]:
    current = settings or load_games_settings()
    raw = read_json_safe(keepalive_state_path(current), {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "interval_hours": KEEP_INTERVAL_HOURS,
        "last_run_at": raw.get("last_run_at"),
        "results": raw.get("results") if isinstance(raw.get("results"), dict) else {},
    }


def save_keepalive_state(settings: GamesSettings, state: dict[str, Any]) -> None:
    write_json_atomic(keepalive_state_path(settings), state)


def _result(
    *,
    ok: bool,
    skipped: bool = False,
    refreshed: bool = False,
    message: str = "",
) -> dict[str, Any]:
    return {
        "ok": ok,
        "skipped": skipped,
        "refreshed": refreshed,
        "message": message,
        "checked_at": utc_now_iso_z(),
    }


def _skip(message: str) -> dict[str, Any]:
    return _result(ok=True, skipped=True, message=message)


async def _keep_baidu(settings: GamesSettings) -> dict[str, Any]:
    if openlist_configured(settings, "baidu"):
        return _skip("走 OpenList，无需续期 Cookie")
    cookie = str(settings.baidu_cookie or "").strip()
    if not cookie or not settings.baidu_enabled:
        return _skip("未配置")
    cookies = _cookie_dict(cookie, "BDUSS")
    async with httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            cookies=cookies,
            headers={
                "User-Agent": "netdisk;1.0",
                "Referer": "https://pan.baidu.com/disk/home",
            },
        )
    ) as client:
        try:
            response = await client.get(
                "https://pan.baidu.com/api/quota",
                params={"checkexpire": "1", "checkfree": "1"},
            )
            data = _json_data(response)
            if data.get("errno") not in {None, 0, "0"}:
                raise CloudError(str(data.get("show_msg") or data.get("errmsg") or data))
            if response.status_code >= 400 or looks_like_html(response.text):
                raise CloudError(
                    f"百度续期失败：{safe_response_snippet(response, limit=180)}"
                )
            return _result(
                ok=True,
                refreshed=persist_client_cookies(
                    "baidu_cookie", settings.baidu_cookie, "BDUSS", client
                ),
                message="登录有效",
            )
        finally:
            persist_client_cookies(
                "baidu_cookie", settings.baidu_cookie, "BDUSS", client
            )


async def _keep_pikpak(settings: GamesSettings) -> dict[str, Any]:
    if openlist_configured(settings, "pikpak"):
        return _skip("走 OpenList，无需续期 Token")
    if not (
        settings.pikpak_refresh_token
        or (settings.pikpak_username and settings.pikpak_password)
    ):
        return _skip("未配置")
    before = str(settings.pikpak_refresh_token or "")
    async with httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(30.0), follow_redirects=True
        )
    ) as client:
        await _pikpak_login(settings, client)
    after = load_games_settings().pikpak_refresh_token
    return _result(
        ok=True,
        refreshed=bool(after) and after != before,
        message="refresh_token 有效",
    )


async def _keep_terabox(settings: GamesSettings) -> dict[str, Any]:
    if openlist_configured(settings, "terabox"):
        return _skip("走 OpenList，无需续期 Cookie")
    cookies = _cookie_dict(settings.terabox_cookie, "ndus")
    if not any(key.casefold() == "ndus" for key in cookies):
        return _skip("未配置")
    cookies.setdefault("lang", "en")
    async with httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            cookies=cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": f"{TERABOX_BASE}/main",
                "Accept": "application/json, text/plain, */*",
            },
        )
    ) as client:
        try:
            js_token = await _terabox_refresh_js_token(client)
            data, _token = await _terabox_json(
                client,
                "POST",
                "/api/list",
                js_token,
                data={
                    "order": "name",
                    "desc": "0",
                    "dir": settings.terabox_remote_dir or "/",
                    "num": "1",
                    "page": "1",
                    "showempty": "0",
                },
            )
            if data.get("errno") not in {None, 0, "0"}:
                raise CloudError(str(data))
            return _result(
                ok=True,
                refreshed=persist_client_cookies(
                    "terabox_cookie", settings.terabox_cookie, "ndus", client
                ),
                message="登录有效",
            )
        finally:
            persist_client_cookies(
                "terabox_cookie", settings.terabox_cookie, "ndus", client
            )


async def _keep_quark(settings: GamesSettings) -> dict[str, Any]:
    if openlist_configured(settings, "quark"):
        return _skip("走 OpenList，无需续期 Cookie")
    cookie = _quark_cookie_header(settings.quark_cookie)
    if not cookie:
        return _skip("未配置")
    cookies = _cookie_dict(cookie, "__puus")
    last_error = ""
    for base in QUARK_API_BASES:
        async with httpx.AsyncClient(
            **httpx_async_client_kwargs(
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
                cookies=cookies,
                headers={
                    "User-Agent": QUARK_UA,
                    "Origin": "https://pan.quark.cn",
                    "Referer": "https://pan.quark.cn/",
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                },
            )
        ) as client:
            client._quark_base = base  # type: ignore[attr-defined]
            try:
                parent = str(settings.quark_folder_id or "").strip() or "0"
                await _quark_json(
                    client,
                    "GET",
                    "/file/sort",
                    params={
                        "pdir_fid": parent,
                        "_page": "1",
                        "_size": "1",
                        "_fetch_total": "1",
                    },
                )
                refreshed = persist_client_cookies(
                    "quark_cookie", settings.quark_cookie, "__puus", client
                )
                return _result(ok=True, refreshed=refreshed, message="登录有效")
            except Exception as exc:
                last_error = str(exc)
                logger.warning("夸克续期 %s 失败：%s", base, exc)
            finally:
                persist_client_cookies(
                    "quark_cookie", settings.quark_cookie, "__puus", client
                )
    raise CloudError(last_error or "夸克续期失败")


_KEEPERS = {
    "baidu": _keep_baidu,
    "pikpak": _keep_pikpak,
    "terabox": _keep_terabox,
    "quark": _keep_quark,
}


async def refresh_cloud_sessions(
    settings: GamesSettings | None = None,
) -> dict[str, Any]:
    """访问已配置网盘并写回刷新后的 Cookie / refresh_token。"""
    async with _refresh_lock:
        return await _refresh_cloud_sessions(settings)


async def _refresh_cloud_sessions(
    settings: GamesSettings | None = None,
) -> dict[str, Any]:
    current = settings or load_games_settings()
    statuses = cloud_status(current)
    results: dict[str, Any] = {}
    for target in CLOUD_TARGETS:
        status = statuses.get(target) or {}
        if not status.get("configured"):
            results[target] = _skip("未配置")
            continue
        keeper = _KEEPERS.get(target)
        if keeper is None:
            results[target] = _skip("无需续期")
            continue
        try:
            results[target] = await keeper(current)
            current = load_games_settings()
        except Exception as exc:
            logger.warning("网盘 %s 自动续期失败：%s", target, exc)
            message = (str(exc).strip() or type(exc).__name__)[:240]
            results[target] = _result(ok=False, message=message)
            results[target]["failure_kind"] = (
                "auth" if _is_cloud_auth_failure(exc) else "keepalive"
            )
        result = results.get(target) or {}
        if not result.get("ok") and not result.get("skipped"):
            auth_failure = result.get("failure_kind") == "auth"
            schedule_alert(
                "cloud_auth_invalid" if auth_failure else "cloud_keepalive_fail",
                title=(
                    f"网盘登录失效：{target}"
                    if auth_failure
                    else f"网盘会话续期失败：{target}"
                ),
                detail=str(result.get("message") or "登录无效"),
                fingerprint=target,
            )
    state = {
        "interval_hours": KEEP_INTERVAL_HOURS,
        "last_run_at": utc_now_iso_z(),
        "results": results,
    }
    save_keepalive_state(current, state)
    return state
