"""Structured global proxy settings and connectivity checks."""

from __future__ import annotations

import ipaddress
import re
from time import perf_counter
from typing import Any, Mapping, Optional
from urllib.parse import quote, unquote, urlsplit

import httpx

from backend.utils.http_error_text import looks_like_html
from backend.utils.proxy import normalize_proxy_url

SUPPORTED_PROXY_SCHEMES = frozenset({"http", "socks5"})
DEFAULT_PROXY_SCHEME = "http"
DEFAULT_PROXY_TEST_URL = "https://api.telegram.org"
REQUIRED_NO_PROXY_ENTRIES = ("localhost", "127.0.0.1", "::1")
DEFAULT_NO_PROXY = ",".join(REQUIRED_NO_PROXY_ENTRIES)

_PROXY_SETTING_KEYS = frozenset(
    {
        "proxy_enabled",
        "proxy_scheme",
        "proxy_host",
        "proxy_port",
        "proxy_username",
        "proxy_password",
        "proxy_no_proxy",
    }
)
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _normalize_host(value: object) -> Optional[str]:
    host = str(value or "").strip()
    if not host:
        return None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    if len(host) > 253:
        raise ValueError("代理主机名过长")
    if any(char in host for char in ("://", "/", "@", "?", "#")):
        raise ValueError("代理主机只能填写域名或 IP 地址")

    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    if not _HOSTNAME_RE.fullmatch(host):
        raise ValueError("代理主机格式无效")
    labels = host.rstrip(".").split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        raise ValueError("代理主机格式无效")
    return host.rstrip(".")


def _normalize_scheme(value: object) -> str:
    scheme = str(value or DEFAULT_PROXY_SCHEME).strip().lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("代理类型仅支持 http 或 socks5（HTTP 代理可转发 HTTPS）")
    return scheme


def _normalize_port(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("代理端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("代理端口必须在 1-65535 之间")
    return port


def _validate_no_proxy_entry(entry: str) -> None:
    if entry == "*":
        return
    if len(entry) > 255 or any(char.isspace() for char in entry):
        raise ValueError("代理绕过地址格式无效")
    if "://" in entry or "@" in entry or "?" in entry or "#" in entry:
        raise ValueError("代理绕过地址格式无效")

    if "/" in entry:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise ValueError("代理绕过网段格式无效") from exc
        return

    candidate = entry
    if candidate.startswith("*."):
        candidate = candidate[2:]
    elif candidate.startswith("."):
        candidate = candidate[1:]
    if not candidate:
        raise ValueError("代理绕过地址格式无效")

    try:
        ipaddress.ip_address(candidate.strip("[]"))
        return
    except ValueError:
        pass

    # Standard NO_PROXY values may include a port (for example example.com:443).
    if candidate.count(":") == 1:
        possible_host, possible_port = candidate.rsplit(":", 1)
        if possible_port.isdigit():
            _normalize_port(possible_port)
            candidate = possible_host
    _normalize_host(candidate)


def normalize_no_proxy(value: object) -> str:
    """Normalize bypass entries and always keep loopback traffic direct."""

    if isinstance(value, (list, tuple, set)):
        raw_entries = [str(item) for item in value]
    else:
        raw_entries = re.split(r"[,;\n\r]+", str(value or ""))

    entries: list[str] = []
    seen: set[str] = set()
    for raw_entry in [*raw_entries, *REQUIRED_NO_PROXY_ENTRIES]:
        entry = raw_entry.strip()
        if not entry:
            continue
        _validate_no_proxy_entry(entry)
        identity = entry.casefold()
        if identity not in seen:
            entries.append(entry)
            seen.add(identity)
    if len(entries) > 128:
        raise ValueError("代理绕过地址最多允许 128 个")
    result = ",".join(entries)
    if len(result) > 4096:
        raise ValueError("代理绕过地址过长")
    return result


def parse_legacy_proxy_url(raw: object) -> dict[str, Any]:
    """Parse the legacy ``global_proxy`` string into structured fields."""

    value = normalize_proxy_url(str(raw or ""))
    if not value:
        raise ValueError("代理地址不能为空")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理地址端口无效") from exc
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")
    host = _normalize_host(parsed.hostname)
    if not host or port is None:
        raise ValueError("代理地址必须包含主机和端口")

    result: dict[str, Any] = {
        "proxy_enabled": True,
        "proxy_scheme": _normalize_scheme(parsed.scheme),
        "proxy_host": host,
        "proxy_port": _normalize_port(port),
        "proxy_username": unquote(parsed.username) if parsed.username else None,
        "proxy_password": unquote(parsed.password) if parsed.password else None,
        "proxy_no_proxy": DEFAULT_NO_PROXY,
    }
    validate_proxy_settings(result)
    return result


def normalize_proxy_update(values: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize proxy fields from an API request or imported global settings."""

    normalized = dict(values)
    structured_fields_supplied = bool(_PROXY_SETTING_KEYS.intersection(normalized))
    if "global_proxy" in normalized and not structured_fields_supplied:
        legacy_value = normalized.get("global_proxy")
        if legacy_value is None or not str(legacy_value).strip():
            normalized.update(
                {
                    "proxy_enabled": False,
                    "proxy_host": None,
                    "proxy_port": None,
                    "proxy_username": None,
                    "proxy_password": None,
                }
            )
        else:
            normalized.update(parse_legacy_proxy_url(legacy_value))

    if "proxy_enabled" in normalized:
        enabled = normalized["proxy_enabled"]
        if isinstance(enabled, str):
            lowered = enabled.strip().lower()
            if lowered not in {"true", "false", "1", "0"}:
                raise ValueError("代理启用状态格式无效")
            enabled = lowered in {"true", "1"}
        normalized["proxy_enabled"] = bool(enabled)
    if "proxy_scheme" in normalized:
        normalized["proxy_scheme"] = _normalize_scheme(normalized["proxy_scheme"])
    if "proxy_host" in normalized:
        normalized["proxy_host"] = _normalize_host(normalized["proxy_host"])
    if "proxy_port" in normalized:
        normalized["proxy_port"] = _normalize_port(normalized["proxy_port"])
    if "proxy_username" in normalized:
        username = str(normalized.get("proxy_username") or "").strip()
        if len(username) > 256:
            raise ValueError("代理用户名过长")
        normalized["proxy_username"] = username or None
    if "proxy_password" in normalized:
        password = normalized.get("proxy_password")
        if password is None or not str(password).strip():
            # Empty secret means keep the existing value.
            normalized.pop("proxy_password")
        else:
            password = str(password)
            if len(password) > 1024:
                raise ValueError("代理密码过长")
            normalized["proxy_password"] = password
    if "proxy_no_proxy" in normalized:
        normalized["proxy_no_proxy"] = normalize_no_proxy(
            normalized.get("proxy_no_proxy")
        )

    if normalized.pop("proxy_clear_credentials", False):
        normalized["proxy_username"] = None
        normalized["proxy_password"] = None

    # ``global_proxy`` is runtime compatibility data and is never persisted.
    if "global_proxy" in normalized:
        normalized["global_proxy"] = None
    return normalized


def validate_proxy_settings(settings: Mapping[str, Any]) -> None:
    """Validate the merged structured proxy configuration."""

    enabled = bool(settings.get("proxy_enabled"))
    scheme = _normalize_scheme(settings.get("proxy_scheme"))
    host = _normalize_host(settings.get("proxy_host"))
    port = _normalize_port(settings.get("proxy_port"))
    username = str(settings.get("proxy_username") or "").strip() or None
    password = settings.get("proxy_password")

    if enabled and (not host or port is None):
        raise ValueError("启用代理时必须填写代理主机和端口")
    if username and len(username) > 256:
        raise ValueError("代理用户名过长")
    if password:
        from tg_signer.security import is_encrypted_secret

        if not is_encrypted_secret(password) and len(str(password)) > 1024:
            raise ValueError("代理密码过长")
    if password and not username:
        raise ValueError("设置代理密码时必须同时填写用户名")
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("代理类型不受支持")
    normalize_no_proxy(settings.get("proxy_no_proxy"))


def build_proxy_url(
    settings: Mapping[str, Any], *, password: Optional[str] = None
) -> Optional[str]:
    """Build an internal proxy URL; the caller supplies a decrypted password."""

    if not bool(settings.get("proxy_enabled")):
        return None
    validate_proxy_settings({**settings, "proxy_password": password})
    scheme = _normalize_scheme(settings.get("proxy_scheme"))
    host = _normalize_host(settings.get("proxy_host"))
    port = _normalize_port(settings.get("proxy_port"))
    if host is None or port is None:
        return None

    username = str(settings.get("proxy_username") or "").strip()
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += f":{quote(password, safe='')}"
        auth += "@"
    display_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{auth}{display_host}:{port}"


async def check_proxy_connection(proxy_url: str) -> dict[str, Any]:
    """Test the saved proxy against a fixed endpoint without exposing its URL."""

    started = perf_counter()
    try:
        timeout = httpx.Timeout(12.0, connect=8.0)
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(DEFAULT_PROXY_TEST_URL)
        latency_ms = max(0, round((perf_counter() - started) * 1000))
        response_text = str(getattr(response, "text", "") or "")
        html_intercept = response.status_code < 300 and looks_like_html(response_text)
        success = response.status_code < 400 and not html_intercept
        return {
            "success": success,
            "message": (
                "已通过代理连接 Telegram API"
                if success
                else (
                    f"代理返回了网页而非 Telegram API (HTTP {response.status_code})"
                    if html_intercept
                    else f"代理连接测试失败 (HTTP {response.status_code})"
                )
            ),
            "status_code": response.status_code,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = max(0, round((perf_counter() - started) * 1000))
        return {
            "success": False,
            "message": f"代理连接测试失败 ({type(exc).__name__})",
            "status_code": None,
            "latency_ms": latency_ms,
        }
