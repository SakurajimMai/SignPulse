from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from backend.utils.outbound import (
    get_proxy_runtime_settings,
    should_bypass_proxy,
)
from backend.utils.socket_proxy import create_proxy_connection
from tg_signer.security import decrypt_secret, is_encrypted_secret

logger = logging.getLogger("backend.alerts.smtp")


def _decrypt_password(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if is_encrypted_secret(text):
        try:
            return decrypt_secret(text) or ""
        except Exception:
            logger.warning("SMTP 密码解密失败")
            return ""
    return text


def smtp_settings(global_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    if global_settings is None:
        from backend.services.config import get_config_service

        global_settings = get_config_service().get_global_settings()
    encryption = str(global_settings.get("smtp_encryption") or "ssl").strip().lower()
    if encryption not in {"ssl", "starttls", "none"}:
        encryption = "ssl"
    try:
        port = int(
            global_settings.get("smtp_port") or (465 if encryption == "ssl" else 587)
        )
    except (TypeError, ValueError):
        port = 465 if encryption == "ssl" else 587
    recipients = [
        item.strip()
        for item in str(global_settings.get("smtp_notify_email") or "")
        .replace("；", ";")
        .replace("，", ",")
        .replace(";", ",")
        .split(",")
        if item.strip()
    ]
    sender = (
        str(global_settings.get("smtp_from") or "").strip()
        or str(global_settings.get("smtp_username") or "").strip()
    )
    host = str(global_settings.get("smtp_host") or "").strip()
    port = max(1, min(port, 65535))
    proxy_runtime = get_proxy_runtime_settings()
    proxy_url = ""
    if proxy_runtime["enabled"]:
        proxy_url = str(proxy_runtime.get("url") or "").strip()
        if not proxy_url:
            raise RuntimeError(
                "系统代理已启用，但代理地址不可用；已禁止 SMTP 直连"
            )
        display_host = f"[{host}]" if ":" in host else host
        endpoint = f"smtp://{display_host}:{port}"
        if should_bypass_proxy(endpoint, proxy_runtime["no_proxy"]):
            proxy_url = ""
    return {
        "enabled": bool(global_settings.get("smtp_enabled")),
        "host": host,
        "port": port,
        "username": str(global_settings.get("smtp_username") or "").strip(),
        "password": _decrypt_password(global_settings.get("smtp_password")),
        "encryption": encryption,
        "sender": sender,
        "recipients": recipients,
        "proxy_url": proxy_url,
    }


def _cfg_ready(cfg: dict[str, Any]) -> bool:
    auth_ready = not cfg.get("username") or bool(cfg.get("password"))
    return bool(
        cfg.get("enabled")
        and cfg.get("host")
        and cfg.get("sender")
        and cfg.get("recipients")
        and auth_ready
    )


def _clean_subject(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def smtp_ready(settings: dict[str, Any] | None = None) -> bool:
    return _cfg_ready(smtp_settings(settings))


class _ProxySMTP(smtplib.SMTP):
    def __init__(self, *args: Any, proxy_url: str, **kwargs: Any) -> None:
        self._proxy_url = proxy_url
        super().__init__(*args, **kwargs)

    def _get_socket(self, host: str, port: int, timeout: float):
        return create_proxy_connection(
            self._proxy_url,
            host,
            port,
            timeout=timeout,
            source_address=self.source_address,
        )


class _ProxySMTP_SSL(smtplib.SMTP_SSL):
    def __init__(self, *args: Any, proxy_url: str, **kwargs: Any) -> None:
        self._proxy_url = proxy_url
        super().__init__(*args, **kwargs)

    def _get_socket(self, host: str, port: int, timeout: float):
        raw_socket = create_proxy_connection(
            self._proxy_url,
            host,
            port,
            timeout=timeout,
            source_address=self.source_address,
        )
        try:
            return self.context.wrap_socket(raw_socket, server_hostname=self._host)
        except BaseException:
            raw_socket.close()
            raise


def send_smtp_mail(
    *,
    subject: str,
    body: str,
    settings: dict[str, Any] | None = None,
) -> None:
    cfg = smtp_settings(settings)
    if not _cfg_ready(cfg):
        raise RuntimeError(
            "SMTP 未配置完整：需要主机、发件人、通知邮箱，用户名存在时还需要密码"
        )
    message = EmailMessage()
    message["Subject"] = _clean_subject(subject)[:180]
    message["From"] = cfg["sender"]
    message["To"] = ", ".join(cfg["recipients"])
    message.set_content(body or "")
    context = ssl.create_default_context()
    proxy_url = str(cfg.get("proxy_url") or "").strip()
    if cfg["encryption"] == "ssl":
        smtp_ssl_class = _ProxySMTP_SSL if proxy_url else smtplib.SMTP_SSL
        smtp_ssl_kwargs: dict[str, Any] = {"timeout": 20, "context": context}
        if proxy_url:
            smtp_ssl_kwargs["proxy_url"] = proxy_url
        server: smtplib.SMTP = smtp_ssl_class(
            cfg["host"], cfg["port"], **smtp_ssl_kwargs
        )
    else:
        smtp_class = _ProxySMTP if proxy_url else smtplib.SMTP
        smtp_kwargs: dict[str, Any] = {"timeout": 20}
        if proxy_url:
            smtp_kwargs["proxy_url"] = proxy_url
        server = smtp_class(cfg["host"], cfg["port"], **smtp_kwargs)
    try:
        server.ehlo()
        if cfg["encryption"] == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"])
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass
