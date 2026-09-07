from __future__ import annotations

import asyncio

import pytest

from backend.services.telegram import sessions as sessions_mod
from backend.services.telegram.login_qr import TelegramQrLoginMixin


@pytest.fixture(autouse=True)
def clean_qr_sessions():
    sessions_mod._qr_login_sessions.clear()
    yield
    sessions_mod._qr_login_sessions.clear()


@pytest.mark.asyncio
async def test_cancel_after_success_keeps_persisted_session(tmp_path, monkeypatch):
    """官方 cancel 会走 cleanup；成功后的 cancel 仍删除未授权残留 session 文件。"""
    service = TelegramQrLoginMixin()
    service.session_dir = tmp_path
    session_file = tmp_path / "account.session"
    session_file.write_bytes(b"persisted")
    lock = asyncio.Lock()
    await lock.acquire()
    sessions_mod._qr_login_sessions["login-1"] = {
        "account_name": "account",
        "status": "success",
        "client": None,
        "lock": lock,
    }
    monkeypatch.setattr(
        "backend.services.telegram.login_qr.get_session_mode",
        lambda: "file",
    )

    assert await service.cancel_qr_login("login-1") is True
    assert not lock.locked()
    assert "login-1" not in sessions_mod._qr_login_sessions


@pytest.mark.asyncio
async def test_cancel_before_success_removes_temporary_session(tmp_path, monkeypatch):
    service = TelegramQrLoginMixin()
    service.session_dir = tmp_path
    session_file = tmp_path / "account.session"
    session_file.write_bytes(b"temporary")
    sessions_mod._qr_login_sessions["login-2"] = {
        "account_name": "account",
        "status": "waiting_scan",
        "client": None,
        "lock": None,
    }
    monkeypatch.setattr(
        "backend.services.telegram.login_qr.get_session_mode",
        lambda: "file",
    )

    assert await service.cancel_qr_login("login-2") is True
    assert not session_file.exists()


@pytest.mark.asyncio
async def test_qr_status_missing_session_is_expired():
    service = TelegramQrLoginMixin()
    result = await service.get_qr_login_status("missing")
    assert result["status"] == "expired"


@pytest.mark.asyncio
async def test_scanned_status_does_not_expire_old_token():
    import time

    service = TelegramQrLoginMixin()
    sessions_mod._qr_login_sessions["login-3"] = {
        "status": "scanned_wait_confirm",
        "scan_seen": True,
        "expires_ts": 1,
        "expires_at": "old",
        "last_export_ts": time.time(),
        "client": None,
    }

    result = await service.get_qr_login_status("login-3")

    assert result["status"] == "scanned_wait_confirm"
    assert "login-3" in sessions_mod._qr_login_sessions
    assert int(sessions_mod._qr_login_sessions["login-3"]["expires_ts"]) > 1
