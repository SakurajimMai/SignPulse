from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import socks


@dataclass(frozen=True)
class SocketProxy:
    proxy_type: int
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def parse_socket_proxy(raw: str) -> SocketProxy:
    """Parse the system proxy for non-HTTP TCP clients without exposing it."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("代理地址为空")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    scheme = parsed.scheme.casefold()
    proxy_types = {
        "http": socks.PROXY_TYPE_HTTP,
        "socks5": socks.PROXY_TYPE_SOCKS5,
    }
    if scheme not in proxy_types:
        raise ValueError("TCP 出站代理仅支持 HTTP 或 SOCKS5")
    if not parsed.hostname or port is None:
        raise ValueError("代理主机和端口不能为空")
    return SocketProxy(
        proxy_type=proxy_types[scheme],
        host=parsed.hostname,
        port=port,
        username=unquote(parsed.username) if parsed.username is not None else None,
        password=unquote(parsed.password) if parsed.password is not None else None,
    )


def create_proxy_connection(
    proxy_url: str,
    host: str,
    port: int,
    *,
    timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Open a TCP stream through the configured HTTP CONNECT/SOCKS5 proxy."""
    proxy = parse_socket_proxy(proxy_url)
    sock = socks.socksocket()
    try:
        sock.set_proxy(
            proxy_type=proxy.proxy_type,
            addr=proxy.host,
            port=proxy.port,
            rdns=True,
            username=proxy.username,
            password=proxy.password,
        )
        if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        sock.connect((str(host), int(port)))
        return sock
    except BaseException:
        sock.close()
        raise
