from __future__ import annotations

import importlib
from typing import Any

import pytest

from backend.services.alerts import smtp as smtp_module
from backend.utils import socket_proxy


def test_parse_socket_proxy_supports_credentials_and_rejects_https():
    parsed = socket_proxy.parse_socket_proxy(
        "socks5://user%40name:p%2Fass@proxy.example:1080"
    )
    assert parsed.host == "proxy.example"
    assert parsed.port == 1080
    assert parsed.username == "user@name"
    assert parsed.password == "p/ass"

    with pytest.raises(ValueError, match="HTTP 或 SOCKS5"):
        socket_proxy.parse_socket_proxy("https://proxy.example:8443")


def test_create_proxy_connection_configures_pysocks(monkeypatch):
    calls: list[tuple[str, Any]] = []

    class FakeSocket:
        def set_proxy(self, **kwargs):
            calls.append(("proxy", kwargs))

        def settimeout(self, value):
            calls.append(("timeout", value))

        def bind(self, value):
            calls.append(("bind", value))

        def connect(self, value):
            calls.append(("connect", value))

        def close(self):
            calls.append(("close", None))

    fake = FakeSocket()
    monkeypatch.setattr(socket_proxy.socks, "socksocket", lambda: fake)
    result = socket_proxy.create_proxy_connection(
        "http://proxy.local:3128",
        "smtp.example.com",
        587,
        timeout=12,
        source_address=("127.0.0.1", 0),
    )

    assert result is fake
    assert ("connect", ("smtp.example.com", 587)) in calls
    proxy_call = next(value for name, value in calls if name == "proxy")
    assert proxy_call["addr"] == "proxy.local"
    assert proxy_call["port"] == 3128
    assert proxy_call["rdns"] is True


def test_smtp_uses_system_proxy_without_exposing_credentials(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeServer:
        def ehlo(self):
            return None

        def login(self, username, password):
            captured["login"] = (username, password)

        def send_message(self, message):
            captured["subject"] = str(message["Subject"])

        def quit(self):
            return None

    def make_server(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeServer()

    monkeypatch.setattr(smtp_module, "_ProxySMTP_SSL", make_server)
    monkeypatch.setattr(
        smtp_module,
        "get_proxy_runtime_settings",
        lambda: {
            "enabled": True,
            "url": "socks5://proxy-user:proxy-secret@proxy.local:1080",
            "no_proxy": "localhost,127.0.0.1,::1",
        },
    )
    smtp_module.send_smtp_mail(
        subject="proxy test",
        body="body",
        settings={
            "smtp_enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_encryption": "ssl",
            "smtp_username": "from@example.com",
            "smtp_password": "mail-secret",
            "smtp_from": "from@example.com",
            "smtp_notify_email": "ops@example.com",
        },
    )

    assert captured["args"] == ("smtp.example.com", 465)
    assert captured["kwargs"]["proxy_url"].startswith("socks5://")
    assert captured["subject"] == "proxy test"


def test_smtp_fails_closed_when_enabled_proxy_has_no_url(monkeypatch):
    monkeypatch.setattr(
        smtp_module,
        "get_proxy_runtime_settings",
        lambda: {"enabled": True, "url": None, "no_proxy": "localhost"},
    )

    with pytest.raises(RuntimeError, match="禁止 SMTP 直连"):
        smtp_module.send_smtp_mail(
            subject="proxy test",
            body="body",
            settings={
                "smtp_enabled": True,
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_encryption": "ssl",
                "smtp_username": "from@example.com",
                "smtp_password": "mail-secret",
                "smtp_from": "from@example.com",
                "smtp_notify_email": "ops@example.com",
            },
        )


def test_telegram_account_proxy_config_error_is_not_swallowed(monkeypatch):
    accounts_module = importlib.import_module("backend.services.telegram.accounts")
    config_module = importlib.import_module("backend.services.config")

    class BrokenConfig:
        def get_global_proxy(self):
            raise RuntimeError("系统代理已启用，但代理地址不可用")

    monkeypatch.setattr(accounts_module, "get_account_profile", lambda _name: {})
    monkeypatch.setattr(config_module, "get_config_service", BrokenConfig)

    with pytest.raises(RuntimeError, match="代理地址不可用"):
        accounts_module._account_proxy_dict("proxy-account")


def test_get_client_replaces_idle_cached_client_when_proxy_changes(monkeypatch):
    client_module = importlib.import_module("tg_signer.core.client")

    class FakeClient:
        def __init__(self, _name, *, proxy=None, key="", **_kwargs):
            self.proxy = proxy
            self.key = key
            self.is_connected = False

    monkeypatch.setattr(client_module, "Client", FakeClient)
    monkeypatch.setattr(client_module, "_CLIENT_INSTANCES", {})
    monkeypatch.setattr(client_module, "_CLIENT_REFS", {})

    first = client_module.get_client(
        "proxy-account",
        workdir="/tmp/proxy-client-test",
        api_id=1,
        api_hash="hash",
        proxy={"scheme": "http", "hostname": "one", "port": 8080},
    )
    second = client_module.get_client(
        "proxy-account",
        workdir="/tmp/proxy-client-test",
        api_id=1,
        api_hash="hash",
        proxy={"scheme": "socks5", "hostname": "two", "port": 1080},
    )

    assert second is not first
    assert second.proxy["hostname"] == "two"
