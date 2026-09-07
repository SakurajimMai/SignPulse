from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from backend.utils import outbound


def _set_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
    url: str | None = None,
    no_proxy: str = "localhost,127.0.0.1,::1",
) -> None:
    monkeypatch.setattr(
        outbound,
        "get_proxy_runtime_settings",
        lambda: {"enabled": enabled, "url": url, "no_proxy": no_proxy},
    )


def test_disabled_proxy_preserves_httpx_defaults(monkeypatch: pytest.MonkeyPatch):
    _set_runtime(monkeypatch, enabled=False)
    transport = httpx.MockTransport(lambda _request: httpx.Response(204))

    kwargs = outbound.httpx_client_kwargs(timeout=3, transport=transport)

    assert kwargs == {"timeout": 3, "transport": transport}
    assert "proxy" not in kwargs
    assert "trust_env" not in kwargs


def test_enabled_proxy_configures_httpx_and_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_runtime(
        monkeypatch,
        enabled=True,
        url="http://user:secret@proxy.example:3128",
        no_proxy="localhost,127.0.0.1,::1,.internal.example",
    )

    kwargs = outbound.httpx_async_client_kwargs(timeout=5)

    assert kwargs["proxy"] == "http://user:secret@proxy.example:3128"
    assert kwargs["trust_env"] is False
    assert set(kwargs["mounts"]) == {
        "all://localhost",
        "all://127.0.0.1",
        "all://[::1]",
        "all://*.internal.example",
    }
    assert set(kwargs["mounts"].values()) == {None}


def test_enabled_proxy_rejects_unmanaged_custom_httpx_transport(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_runtime(monkeypatch, enabled=True, url="http://proxy.example:3128")
    transport = httpx.MockTransport(lambda _request: httpx.Response(204))

    with pytest.raises(
        outbound.OutboundProxyConfigurationError,
        match="custom HTTPX transports",
    ):
        outbound.httpx_async_client_kwargs(transport=transport)


def test_runtime_settings_fail_closed_when_enabled_without_url(
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.services import config as config_module

    service = SimpleNamespace(
        get_proxy_runtime_settings=lambda: {
            "enabled": True,
            "url": None,
            "no_proxy": "localhost",
        }
    )
    monkeypatch.setattr(config_module, "get_config_service", lambda: service)

    with pytest.raises(
        outbound.OutboundProxyConfigurationError,
        match="enabled without a usable URL",
    ):
        outbound.get_proxy_runtime_settings()


@pytest.mark.parametrize(
    ("url", "no_proxy", "expected"),
    [
        ("https://localhost:9000/a", "localhost", True),
        ("https://api.internal.example/a", ".internal.example", True),
        ("https://internal.example/a", ".internal.example", False),
        ("https://10.4.3.2/a", "10.0.0.0/8", True),
        ("https://10.4.3.2:9443/a", "10.4.3.2:9000", False),
        ("https://public.example/a", "localhost,127.0.0.1", False),
    ],
)
def test_should_bypass_proxy(url: str, no_proxy: str, expected: bool):
    assert outbound.should_bypass_proxy(url, no_proxy) is expected


def test_openai_client_injects_proxy_http_client(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    class FakeOpenAIHttpClient:
        def __init__(self, **kwargs: Any):
            captured.update(kwargs)

    _set_runtime(
        monkeypatch,
        enabled=True,
        url="http://proxy.example:3128",
        no_proxy="localhost",
    )
    fake_openai = ModuleType("openai")
    fake_openai.DefaultAsyncHttpxClient = FakeOpenAIHttpClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    kwargs = outbound.openai_async_client_kwargs(timeout=11)

    assert kwargs["timeout"] == 11
    assert isinstance(kwargs["http_client"], FakeOpenAIHttpClient)
    assert captured["proxy"] == "http://proxy.example:3128"
    assert captured["trust_env"] is False
    assert captured["mounts"] == {"all://localhost": None}


def test_openai_client_rejects_unmanaged_http_client(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_runtime(monkeypatch, enabled=True, url="http://proxy.example:3128")

    with pytest.raises(
        outbound.OutboundProxyConfigurationError,
        match="custom OpenAI HTTP client",
    ):
        outbound.openai_async_client_kwargs(http_client=object())


def test_botocore_proxy_merge_preserves_existing_options(
    monkeypatch: pytest.MonkeyPatch,
):
    from botocore.config import Config

    _set_runtime(monkeypatch, enabled=True, url="http://proxy.example:3128")
    original = Config(
        retries={"max_attempts": 4, "mode": "standard"},
        s3={"addressing_style": "virtual"},
    )

    proxied = outbound.botocore_config_with_proxy(
        original,
        endpoint_url="https://s3.example.com",
    )

    assert proxied is not original
    assert proxied.retries == original.retries
    assert proxied.s3 == original.s3
    assert proxied.proxies == {
        "http": "http://proxy.example:3128",
        "https": "http://proxy.example:3128",
    }


def test_botocore_proxy_honors_bypass_and_rejects_socks(
    monkeypatch: pytest.MonkeyPatch,
):
    from botocore.config import Config

    original = Config(retries={"max_attempts": 2})
    _set_runtime(
        monkeypatch,
        enabled=True,
        url="http://proxy.example:3128",
        no_proxy=".internal.example",
    )
    assert (
        outbound.botocore_config_with_proxy(
            original,
            endpoint_url="https://files.internal.example",
        )
        is original
    )

    _set_runtime(monkeypatch, enabled=True, url="socks5://proxy.example:1080")
    with pytest.raises(
        outbound.OutboundProxyConfigurationError,
        match="S3 requests require an HTTP",
    ):
        outbound.botocore_config_with_proxy(
            original,
            endpoint_url="https://s3.example.com",
        )


@pytest.mark.asyncio
async def test_udp_forward_fails_closed_while_proxy_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    from tg_signer.core import monitor

    monkeypatch.setattr(
        monitor,
        "get_proxy_runtime_settings",
        lambda: {
            "enabled": True,
            "url": "http://proxy.example:3128",
            "no_proxy": "localhost",
        },
    )

    with pytest.raises(RuntimeError, match="UDP forwarding is disabled"):
        await monitor.UserMonitor.udp_forward(
            SimpleNamespace(host="collector.example", port=9999),
            object(),
        )


def _http_imports(tree: ast.AST) -> tuple[set[str], dict[str, str], set[str]]:
    module_aliases: set[str] = set()
    direct_names: dict[str, str] = {}
    openai_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "httpx":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            for alias in node.names:
                direct_names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == "openai":
            for alias in node.names:
                if alias.name == "AsyncOpenAI":
                    openai_names.add(alias.asname or alias.name)
    return module_aliases, direct_names, openai_names


def test_outbound_client_creation_has_proxy_coverage():
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    constructors = {"Client", "AsyncClient"}
    convenience_calls = {
        "delete",
        "get",
        "head",
        "options",
        "patch",
        "post",
        "put",
        "request",
        "stream",
    }

    for source_root in (root / "backend", root / "tg_signer"):
        for path in source_root.rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            httpx_aliases, direct_names, openai_names = _http_imports(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                segment = ast.get_source_segment(source, node) or ""
                call_name: str | None = None
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in httpx_aliases
                ):
                    call_name = node.func.attr
                    if call_name in convenience_calls:
                        failures.append(f"{relative}:{node.lineno} bare httpx.{call_name}")
                        continue
                elif isinstance(node.func, ast.Name):
                    call_name = direct_names.get(node.func.id)

                if call_name in {"AsyncHTTPTransport", "HTTPTransport"}:
                    if relative != "backend/utils/outbound.py":
                        failures.append(
                            f"{relative}:{node.lineno} unmanaged {call_name}"
                        )
                    continue
                if call_name in constructors:
                    if relative == "backend/utils/outbound.py":
                        continue
                    if relative == "backend/services/proxy_settings.py":
                        if "proxy=proxy_url" in segment and "trust_env=False" in segment:
                            continue
                    helper = (
                        "httpx_async_client_kwargs"
                        if call_name == "AsyncClient"
                        else "httpx_client_kwargs"
                    )
                    if helper not in segment:
                        failures.append(
                            f"{relative}:{node.lineno} {call_name} misses {helper}"
                        )
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in openai_names
                    and "openai_async_client_kwargs" not in segment
                ):
                    failures.append(
                        f"{relative}:{node.lineno} AsyncOpenAI misses proxy adapter"
                    )

            if "boto3.client(" in source and "botocore_config_with_proxy(" not in source:
                failures.append(f"{relative}: boto3 client misses proxy adapter")

    assert not failures, "Uncovered outbound clients:\n" + "\n".join(failures)
