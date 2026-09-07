"""Structured global proxy settings API and migration tests."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.core.auth import create_access_token
from backend.services.config import ConfigService
from backend.services.proxy_settings import (
    DEFAULT_PROXY_TEST_URL,
    check_proxy_connection,
)


def _auth_headers() -> dict[str, str]:
    token = create_access_token(
        {"sub": "admin"},
        expires_delta=timedelta(hours=1),
    )
    return {"Authorization": f"Bearer {token}"}


def _valid_proxy_payload(**overrides):
    payload = {
        "proxy_enabled": True,
        "proxy_scheme": "socks5",
        "proxy_host": "proxy.example.com",
        "proxy_port": 1080,
        "proxy_username": "proxy-user",
        "proxy_password": "proxy-secret",
        "proxy_no_proxy": "example.internal,10.0.0.0/8",
    }
    payload.update(overrides)
    return payload


def test_proxy_settings_defaults_are_safe(client, db_session):
    response = client.get("/api/config/settings", headers=_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["proxy_enabled"] is False
    assert body["proxy_scheme"] == "http"
    assert body["proxy_password"] is None
    assert body["proxy_password_set"] is False
    assert body["global_proxy"] is None
    assert set(body["proxy_no_proxy"].split(",")) >= {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def test_proxy_password_is_encrypted_and_never_echoed(client, db_session):
    response = client.post(
        "/api/config/settings",
        json=_valid_proxy_payload(),
        headers=_auth_headers(),
    )

    assert response.status_code == 200, response.text
    service = ConfigService()
    raw_text = service._get_global_settings_file().read_text(encoding="utf-8")
    persisted = json.loads(raw_text)
    assert "proxy-secret" not in raw_text
    assert str(persisted["proxy_password"]).startswith("fernet:")
    assert persisted["global_proxy"] is None

    got_response = client.get("/api/config/settings", headers=_auth_headers())
    assert got_response.status_code == 200
    got = got_response.json()
    assert got["proxy_password"] is None
    assert got["proxy_password_set"] is True
    assert got["global_proxy"] is None
    assert got["proxy_host"] == "proxy.example.com"
    assert "proxy-secret" not in got_response.text

    internal = service.get_proxy_runtime_settings()
    assert internal == {
        "enabled": True,
        "url": "socks5://proxy-user:proxy-secret@proxy.example.com:1080",
        "no_proxy": (
            "example.internal,10.0.0.0/8,localhost,127.0.0.1,::1"
        ),
    }


def test_config_export_masks_proxy_secret_and_runtime_url(client, db_session):
    saved = client.post(
        "/api/config/settings",
        json=_valid_proxy_payload(proxy_password="export-only-secret"),
        headers=_auth_headers(),
    )
    assert saved.status_code == 200

    response = client.get("/api/config/export/all", headers=_auth_headers())

    assert response.status_code == 200
    assert "export-only-secret" not in response.text
    exported = response.json()
    global_settings = exported["settings"]["global"]
    assert global_settings["proxy_password"] == "***MASKED***"
    assert global_settings["global_proxy"] is None
    assert exported["_meta"]["proxy_password_masked"] is True


def test_empty_proxy_password_keeps_existing_secret(client, db_session):
    first = client.post(
        "/api/config/settings",
        json=_valid_proxy_payload(),
        headers=_auth_headers(),
    )
    assert first.status_code == 200
    service = ConfigService()
    before = json.loads(
        service._get_global_settings_file().read_text(encoding="utf-8")
    )["proxy_password"]

    second = client.post(
        "/api/config/settings",
        json={"proxy_host": "proxy-2.example.com", "proxy_password": ""},
        headers=_auth_headers(),
    )

    assert second.status_code == 200, second.text
    after = json.loads(
        service._get_global_settings_file().read_text(encoding="utf-8")
    )["proxy_password"]
    assert after == before
    assert service.get_global_proxy() == (
        "socks5://proxy-user:proxy-secret@proxy-2.example.com:1080"
    )


def test_broken_enabled_proxy_contract_stays_enabled_and_fails_closed(
    isolated_env,
    monkeypatch,
):
    service = ConfigService()
    monkeypatch.setattr(
        service,
        "get_global_settings",
        lambda: {
            "proxy_enabled": True,
            "global_proxy": None,
            "proxy_no_proxy": "localhost,127.0.0.1,::1",
        },
    )

    assert service.get_proxy_runtime_settings() == {
        "enabled": True,
        "url": None,
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    with pytest.raises(RuntimeError, match="代理地址不可用"):
        service.get_global_proxy()


def test_encrypted_max_length_password_does_not_break_later_saves(
    isolated_env,
):
    service = ConfigService()
    assert service.save_global_settings(
        _valid_proxy_payload(proxy_password="x" * 1024)
    )

    assert service.save_global_settings({"log_retention_days": 14})
    assert service.get_global_settings()["log_retention_days"] == 14


def test_proxy_credentials_can_be_explicitly_cleared(client, db_session):
    first = client.post(
        "/api/config/settings",
        json=_valid_proxy_payload(),
        headers=_auth_headers(),
    )
    assert first.status_code == 200

    cleared = client.post(
        "/api/config/settings",
        json={"proxy_clear_credentials": True},
        headers=_auth_headers(),
    )

    assert cleared.status_code == 200, cleared.text
    got = client.get("/api/config/settings", headers=_auth_headers()).json()
    assert got["proxy_username"] is None
    assert got["proxy_password"] is None
    assert got["proxy_password_set"] is False
    assert ConfigService().get_global_proxy() == (
        "socks5://proxy.example.com:1080"
    )


@pytest.mark.parametrize(
    "payload",
    [
        _valid_proxy_payload(proxy_scheme="https"),
        _valid_proxy_payload(proxy_scheme="ftp"),
        _valid_proxy_payload(proxy_host="http://proxy.example.com"),
        _valid_proxy_payload(proxy_port=0),
        _valid_proxy_payload(proxy_port=65536),
        _valid_proxy_payload(proxy_username=None),
        {"proxy_enabled": True},
        {"proxy_no_proxy": "https://internal.example.com"},
    ],
)
def test_invalid_proxy_settings_are_rejected(client, db_session, payload):
    response = client.post(
        "/api/config/settings",
        json=payload,
        headers=_auth_headers(),
    )

    assert response.status_code == 400, response.text


def test_legacy_global_proxy_is_migrated_and_encrypted(isolated_env):
    service = ConfigService()
    settings_file = service._get_global_settings_file()
    settings_file.write_text(
        json.dumps(
            {
                "global_proxy": (
                    "http://old%20user:p%40ss%2Fword@legacy.example.com:8080"
                )
            }
        ),
        encoding="utf-8",
    )

    settings = service.get_global_settings()

    assert settings["proxy_enabled"] is True
    assert settings["proxy_scheme"] == "http"
    assert settings["proxy_host"] == "legacy.example.com"
    assert settings["proxy_port"] == 8080
    assert settings["proxy_username"] == "old user"
    assert settings["global_proxy"] == (
        "http://old%20user:p%40ss%2Fword@legacy.example.com:8080"
    )
    persisted_text = settings_file.read_text(encoding="utf-8")
    persisted = json.loads(persisted_text)
    assert persisted["global_proxy"] is None
    assert str(persisted["proxy_password"]).startswith("fernet:")
    assert "p%40ss" not in persisted_text
    assert "p@ss/word" not in persisted_text


def test_invalid_legacy_proxy_url_is_removed_from_disk(isolated_env):
    service = ConfigService()
    settings_file = service._get_global_settings_file()
    settings_file.write_text(
        json.dumps(
            {"global_proxy": "http://legacy-user:legacy-secret@bad host:8080"}
        ),
        encoding="utf-8",
    )

    loaded = service.get_global_settings()
    persisted_text = settings_file.read_text(encoding="utf-8")

    assert loaded["global_proxy"] is None
    assert json.loads(persisted_text)["global_proxy"] is None
    assert "legacy-secret" not in persisted_text


def test_legacy_global_proxy_post_populates_structured_settings(
    client, db_session
):
    response = client.post(
        "/api/config/settings",
        json={"global_proxy": "socks5://127.0.0.1:1080"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200, response.text
    got = client.get("/api/config/settings", headers=_auth_headers()).json()
    assert got["proxy_enabled"] is True
    assert got["proxy_scheme"] == "socks5"
    assert got["proxy_host"] == "127.0.0.1"
    assert got["proxy_port"] == 1080
    assert got["global_proxy"] is None


def test_proxy_test_endpoint_requires_auth(client, db_session):
    response = client.post("/api/config/proxy/test")
    assert response.status_code == 401


def test_proxy_test_endpoint_reports_broken_enabled_proxy(client, db_session):
    service = SimpleNamespace(
        get_global_proxy=lambda: (_ for _ in ()).throw(
            RuntimeError("proxy credentials are unavailable")
        )
    )
    with patch("backend.api.routes.config.get_config_service", return_value=service):
        response = client.post(
            "/api/config/proxy/test",
            headers=_auth_headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "message": "系统代理已启用但配置不可用，请重新保存代理设置",
        "status_code": None,
        "latency_ms": None,
    }


def test_proxy_test_endpoint_never_returns_credentials(client, db_session):
    saved = client.post(
        "/api/config/settings",
        json=_valid_proxy_payload(),
        headers=_auth_headers(),
    )
    assert saved.status_code == 200

    result = {
        "success": True,
        "message": "已通过代理连接 Telegram API",
        "status_code": 200,
        "latency_ms": 12,
        "proxy_url": "must-not-be-serialized",
    }
    with patch(
        "backend.services.proxy_settings.check_proxy_connection",
        new_callable=AsyncMock,
        return_value=result,
    ) as tester:
        response = client.post(
            "/api/config/proxy/test",
            headers=_auth_headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "已通过代理连接 Telegram API",
        "status_code": 200,
        "latency_ms": 12,
    }
    assert "proxy-secret" not in response.text
    assert "must-not-be-serialized" not in response.text
    tested_url = tester.await_args.args[0]
    assert "proxy-secret" in tested_url


@pytest.mark.asyncio
async def test_proxy_connection_uses_only_fixed_telegram_endpoint(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url):
            captured["target_url"] = url
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr(
        "backend.services.proxy_settings.httpx.AsyncClient",
        FakeClient,
    )

    result = await check_proxy_connection("http://user:secret@proxy.test:3128")

    assert result["success"] is True
    assert captured["target_url"] == DEFAULT_PROXY_TEST_URL
    assert captured["client_kwargs"]["trust_env"] is False
    assert "proxy_url" not in result
    assert "user" not in str(result)
    assert "secret" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [407, 520])
async def test_proxy_connection_rejects_http_error_status(monkeypatch, status_code):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, _url):
            return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(
        "backend.services.proxy_settings.httpx.AsyncClient",
        FakeClient,
    )

    result = await check_proxy_connection("http://user:secret@proxy.test:3128")

    assert result["success"] is False
    assert result["status_code"] == status_code
    assert f"HTTP {status_code}" in result["message"]
    assert "user" not in str(result)
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_proxy_connection_accepts_redirect_status(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, _url):
            return SimpleNamespace(status_code=302)

    monkeypatch.setattr(
        "backend.services.proxy_settings.httpx.AsyncClient",
        FakeClient,
    )

    result = await check_proxy_connection("http://proxy.test:3128")

    assert result["success"] is True
    assert result["status_code"] == 302


@pytest.mark.asyncio
async def test_proxy_connection_rejects_html_intercept_with_success_status(
    monkeypatch,
):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, _url):
            return SimpleNamespace(
                status_code=200,
                text=(
                    "<!DOCTYPE html><html><title>Cloudflare proxy error</title>"
                    "</html>"
                ),
            )

    monkeypatch.setattr(
        "backend.services.proxy_settings.httpx.AsyncClient",
        FakeClient,
    )

    result = await check_proxy_connection("http://proxy.test:3128")

    assert result["success"] is False
    assert result["status_code"] == 200
    assert "网页而非 Telegram API" in result["message"]
    assert "Cloudflare" not in str(result)
