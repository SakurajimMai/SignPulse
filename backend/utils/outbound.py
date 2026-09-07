"""Central outbound proxy adapters for HTTP, OpenAI, and botocore clients.

The settings import is intentionally lazy: configuration persistence imports utility
modules during startup, while outbound clients may be constructed from almost every
service domain. Keeping that edge lazy avoids a ConfigService import cycle.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx


class OutboundProxyConfigurationError(RuntimeError):
    """The configured proxy cannot safely serve an outbound client."""


def get_proxy_runtime_settings() -> dict[str, Any]:
    """Read the current runtime proxy contract without caching credentials."""

    from backend.services.config import get_config_service

    raw = get_config_service().get_proxy_runtime_settings()
    if not isinstance(raw, Mapping):
        raise OutboundProxyConfigurationError("invalid proxy runtime settings")

    enabled = bool(raw.get("enabled"))
    proxy_url = str(raw.get("url") or "").strip() or None
    no_proxy = str(raw.get("no_proxy") or "").strip()
    if enabled and proxy_url is None:
        # Never silently fall back to a direct connection when proxying was enabled.
        raise OutboundProxyConfigurationError("proxy is enabled without a usable URL")
    return {"enabled": enabled, "url": proxy_url, "no_proxy": no_proxy}


def _no_proxy_mount(entry: str) -> str | None:
    value = entry.strip()
    if not value:
        return None
    if value == "*":
        return "all://"
    if "://" in value:
        return value

    host = value
    port = ""
    if value.startswith("[") and "]" in value:
        end = value.index("]")
        host = value[1:end]
        port = value[end + 1 :]
    elif value.count(":") == 1:
        possible_host, possible_port = value.rsplit(":", 1)
        if possible_port.isdigit():
            host, port = possible_host, f":{possible_port}"

    bare_host = host.lstrip("*.")
    try:
        parsed_ip = ipaddress.ip_address(bare_host)
    except ValueError:
        parsed_ip = None
    if parsed_ip is not None:
        rendered = f"[{parsed_ip}]" if parsed_ip.version == 6 else str(parsed_ip)
        return f"all://{rendered}{port}"
    if "/" in host:
        # HTTPX accepts CIDR-shaped NO_PROXY mounts for compatibility with its
        # environment parser, even though matching support varies by release.
        return f"all://{host}{port}"
    if host.lower() == "localhost":
        return f"all://localhost{port}"
    if host.startswith("*."):
        return f"all://{host}{port}"
    if host.startswith("."):
        return f"all://*{host}{port}"
    return f"all://*{host}{port}"


def no_proxy_mounts(no_proxy: str) -> dict[str, None]:
    """Translate the persisted NO_PROXY list into HTTPX direct mounts."""

    mounts: dict[str, None] = {}
    for raw in str(no_proxy or "").split(","):
        pattern = _no_proxy_mount(raw)
        if pattern:
            mounts[pattern] = None
    return mounts


def should_bypass_proxy(url: str | None, no_proxy: str) -> bool:
    """Return whether a known endpoint is covered by the configured bypass list."""

    if not url:
        return False
    try:
        parsed = urlsplit(str(url))
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return False
    if not host:
        return False

    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        host_ip = None

    for raw in str(no_proxy or "").split(","):
        entry = raw.strip()
        if not entry:
            continue
        if entry == "*":
            return True
        entry_host = entry
        entry_port: int | None = None
        if entry.startswith("[") and "]" in entry:
            end = entry.index("]")
            entry_host = entry[1:end]
            suffix = entry[end + 1 :]
            if suffix.startswith(":") and suffix[1:].isdigit():
                entry_port = int(suffix[1:])
        elif entry.count(":") == 1:
            possible_host, possible_port = entry.rsplit(":", 1)
            if possible_port.isdigit():
                entry_host = possible_host
                entry_port = int(possible_port)
        if entry_port is not None and entry_port != port:
            continue
        if "/" in entry_host and host_ip is not None:
            try:
                if host_ip in ipaddress.ip_network(entry_host, strict=False):
                    return True
            except ValueError:
                continue
            continue
        candidate = entry_host.casefold()
        subdomains_only = candidate.startswith(".") or candidate.startswith("*.")
        candidate = candidate.lstrip("*.")
        if host == candidate:
            if not subdomains_only:
                return True
        elif host.endswith(f".{candidate}"):
            return True
    return False


def _httpx_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(kwargs)
    runtime = get_proxy_runtime_settings()
    if not runtime["enabled"]:
        return result

    transport = result.get("transport")
    if transport is not None:
        if not getattr(transport, "_tg_signpulse_proxy_managed", False):
            raise OutboundProxyConfigurationError(
                "custom HTTPX transports must be created by the outbound proxy adapter"
            )
    else:
        result["proxy"] = runtime["url"]
    result["trust_env"] = False
    mounts = dict(result.get("mounts") or {})
    bypass_mounts = no_proxy_mounts(runtime["no_proxy"])
    if transport is None:
        mounts.update(bypass_mounts)
    elif isinstance(transport, httpx.AsyncBaseTransport):
        mounts.update(
            {
                pattern: httpx.AsyncHTTPTransport(trust_env=False)
                for pattern in bypass_mounts
            }
        )
    else:
        mounts.update(
            {
                pattern: httpx.HTTPTransport(trust_env=False)
                for pattern in bypass_mounts
            }
        )
    if mounts:
        result["mounts"] = mounts
    return result


def httpx_client_kwargs(**kwargs: Any) -> dict[str, Any]:
    """Return proxy-aware kwargs for a synchronous HTTPX client."""

    return _httpx_kwargs(kwargs)


def httpx_async_client_kwargs(**kwargs: Any) -> dict[str, Any]:
    """Return proxy-aware kwargs for an asynchronous HTTPX client."""

    return _httpx_kwargs(kwargs)


def create_httpx_client(**kwargs: Any) -> httpx.Client:
    return httpx.Client(**httpx_client_kwargs(**kwargs))


def create_async_httpx_client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(**httpx_async_client_kwargs(**kwargs))


def create_async_httpx_transport(**kwargs: Any) -> httpx.AsyncHTTPTransport:
    """Create a proxy-aware low-level transport for retry-tuned clients."""

    result = dict(kwargs)
    runtime = get_proxy_runtime_settings()
    if runtime["enabled"]:
        result["proxy"] = runtime["url"]
        result["trust_env"] = False
    transport = httpx.AsyncHTTPTransport(**result)
    transport._tg_signpulse_proxy_managed = True
    return transport


def openai_async_client_kwargs(**kwargs: Any) -> dict[str, Any]:
    """Inject OpenAI's matching HTTP client only while app proxying is enabled."""

    result = dict(kwargs)
    runtime = get_proxy_runtime_settings()
    if not runtime["enabled"]:
        return result
    if result.get("http_client") is not None:
        raise OutboundProxyConfigurationError(
            "a custom OpenAI HTTP client would bypass the application proxy"
        )

    from openai import DefaultAsyncHttpxClient

    mounts = no_proxy_mounts(runtime["no_proxy"])
    client_kwargs: dict[str, Any] = {
        "proxy": runtime["url"],
        "trust_env": False,
    }
    if mounts:
        client_kwargs["mounts"] = mounts
    result["http_client"] = DefaultAsyncHttpxClient(**client_kwargs)
    return result


def botocore_config_with_proxy(
    config: Any = None,
    *,
    endpoint_url: str | None = None,
) -> Any:
    """Merge the application HTTP proxy into botocore without losing options."""

    runtime = get_proxy_runtime_settings()
    if not runtime["enabled"] or should_bypass_proxy(
        endpoint_url, runtime["no_proxy"]
    ):
        return config

    proxy_url = str(runtime["url"])
    if urlsplit(proxy_url).scheme.casefold() != "http":
        # botocore's urllib3 ProxyManager only implements HTTP CONNECT proxies.
        # Raising here is fail-closed and prevents a surprising direct S3 upload.
        raise OutboundProxyConfigurationError(
            "S3 requests require an HTTP application proxy"
        )

    from botocore.config import Config

    proxy_config = Config(proxies={"http": proxy_url, "https": proxy_url})
    return config.merge(proxy_config) if config is not None else proxy_config


__all__ = [
    "OutboundProxyConfigurationError",
    "botocore_config_with_proxy",
    "create_async_httpx_client",
    "create_async_httpx_transport",
    "create_httpx_client",
    "get_proxy_runtime_settings",
    "httpx_async_client_kwargs",
    "httpx_client_kwargs",
    "no_proxy_mounts",
    "openai_async_client_kwargs",
    "should_bypass_proxy",
]
