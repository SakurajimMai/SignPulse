"""Small, token-safe Telegram Bot API client used by control and alerts."""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Sequence

import httpx

from backend.utils.outbound import httpx_async_client_kwargs, httpx_client_kwargs
from tg_signer.security import decrypt_secret, is_encrypted_secret

from .ids import notification_targets

logger = logging.getLogger("backend.telegram_bot.api")

BOT_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096


class TelegramBotApiError(RuntimeError):
    def __init__(
        self,
        method: str,
        description: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.method = str(method)
        self.description = str(description or "Telegram Bot API request failed")[:500]
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(
            f"Telegram Bot API {self.method} failed"
            + (f" ({status_code})" if status_code is not None else "")
            + f": {self.description}"
        )


def bot_token_from_settings(settings: Mapping[str, Any]) -> str:
    """Return the usable token, accepting encrypted storage and legacy plaintext."""
    raw = str(settings.get("telegram_bot_token") or "").strip()
    if not raw:
        return ""
    if not is_encrypted_secret(raw):
        return raw
    try:
        return str(decrypt_secret(raw) or "").strip()
    except Exception:
        logger.warning("Telegram Bot Token 解密失败")
        return ""


def _chat_id(value: Any) -> str:
    return str(value or "").strip()


def _thread_id(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def telegram_bot_ready(settings: Mapping[str, Any]) -> bool:
    """Whether the globally enabled Telegram notification transport is usable."""
    return bool(
        settings.get("telegram_bot_notify_enabled")
        and bot_token_from_settings(settings)
        and notification_targets(settings)
    )


def _clean_description(value: Any, token: str) -> str:
    text = str(value or "Telegram Bot API request failed")
    if token:
        text = text.replace(token, "[REDACTED]")
    return text[:500]


def _api_error(
    *,
    method: str,
    token: str,
    response: httpx.Response | None = None,
    network: bool = False,
) -> TelegramBotApiError:
    status_code = response.status_code if response is not None else None
    description = "network request failed" if network else "invalid Telegram response"
    retry_after = None
    if response is not None:
        try:
            body = response.json()
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict):
            description = _clean_description(body.get("description"), token)
            parameters = body.get("parameters")
            if isinstance(parameters, dict):
                try:
                    retry_after = max(1, min(int(parameters.get("retry_after")), 3600))
                except (TypeError, ValueError):
                    retry_after = None
    return TelegramBotApiError(
        method,
        description,
        status_code=status_code,
        retry_after=retry_after,
    )


def _message_payload(
    *,
    chat_id: str | int,
    text: str,
    parse_mode: str | None = None,
    message_thread_id: int | None = None,
    reply_markup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": str(text or "")[:TELEGRAM_MESSAGE_LIMIT],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)
    if reply_markup:
        payload["reply_markup"] = dict(reply_markup)
    return payload


class TelegramBotApi:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = BOT_API_BASE,
    ) -> None:
        self._token = str(token or "").strip()
        if not self._token or any(char.isspace() for char in self._token):
            raise ValueError("invalid Telegram Bot Token")
        self._base_url = str(base_url).rstrip("/")
        self._client = client or httpx.AsyncClient(**httpx_async_client_kwargs())
        self._owns_client = client is None

    def _url(self, method: str) -> str:
        return f"{self._base_url}/bot{self._token}/{method}"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float = 15.0,
    ) -> Any:
        method_name = str(method or "").strip()
        if not method_name or not method_name.replace("_", "").isalnum():
            raise ValueError("invalid Telegram Bot API method")
        try:
            response = await self._client.post(
                self._url(method_name),
                json=dict(payload or {}),
                timeout=max(1.0, float(timeout)),
            )
        except httpx.RequestError:
            raise _api_error(
                method=method_name, token=self._token, network=True
            ) from None
        if response.status_code >= 400:
            raise _api_error(
                method=method_name, token=self._token, response=response
            )
        try:
            body = response.json()
        except (ValueError, TypeError) as exc:
            raise _api_error(
                method=method_name, token=self._token, response=response
            ) from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise _api_error(
                method=method_name, token=self._token, response=response
            )
        return body.get("result")

    async def get_me(self) -> dict[str, Any]:
        result = await self.request("getMe")
        return dict(result) if isinstance(result, dict) else {}

    async def get_webhook_info(self) -> dict[str, Any]:
        result = await self.request("getWebhookInfo")
        return dict(result) if isinstance(result, dict) else {}

    async def delete_webhook(self, *, drop_pending_updates: bool = True) -> None:
        """Remove webhook delivery so this process can long-poll getUpdates."""
        await self.request(
            "deleteWebhook",
            {"drop_pending_updates": bool(drop_pending_updates)},
        )

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int = 25,
        allowed_updates: Sequence[str] = ("message", "callback_query"),
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": max(1, min(int(timeout), 50)),
            "allowed_updates": list(allowed_updates),
        }
        if offset is not None:
            payload["offset"] = int(offset)
        result = await self.request(
            "getUpdates", payload, timeout=float(payload["timeout"]) + 5.0
        )
        if not isinstance(result, list):
            return []
        return [dict(item) for item in result if isinstance(item, dict)]

    async def send_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self.request(
            "sendMessage",
            _message_payload(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
            ),
        )
        return dict(result) if isinstance(result, dict) else {}

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str = "", show_alert: bool = False
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": str(callback_query_id),
            "show_alert": bool(show_alert),
        }
        if text:
            payload["text"] = str(text)[:200]
        await self.request("answerCallbackQuery", payload)

    async def set_my_commands(
        self,
        commands: Sequence[Mapping[str, str]],
        *,
        scope: Mapping[str, Any] | None = None,
        language_code: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "commands": [dict(item) for item in commands],
        }
        if scope:
            payload["scope"] = dict(scope)
        if language_code:
            payload["language_code"] = str(language_code)
        await self.request("setMyCommands", payload)

    async def delete_my_commands(
        self,
        *,
        scope: Mapping[str, Any] | None = None,
        language_code: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if scope:
            payload["scope"] = dict(scope)
        if language_code:
            payload["language_code"] = str(language_code)
        await self.request("deleteMyCommands", payload)

    async def set_chat_menu_button(
        self, *, url: str, text: str = "打开 Mini App", chat_id: int | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "menu_button": {
                "type": "web_app",
                "text": str(text)[:64],
                "web_app": {"url": str(url)},
            }
        }
        if chat_id is not None:
            payload["chat_id"] = int(chat_id)
        await self.request("setChatMenuButton", payload)

    async def set_commands_menu_button(self, *, chat_id: int | None = None) -> None:
        """Show the slash-command list on the chat input menu button."""
        payload: dict[str, Any] = {"menu_button": {"type": "commands"}}
        if chat_id is not None:
            payload["chat_id"] = int(chat_id)
        await self.request("setChatMenuButton", payload)

    async def set_default_chat_menu_button(
        self, *, chat_id: int | None = None
    ) -> None:
        payload: dict[str, Any] = {"menu_button": {"type": "default"}}
        if chat_id is not None:
            payload["chat_id"] = int(chat_id)
        await self.request("setChatMenuButton", payload)


def send_telegram_bot_message_sync(
    settings: Mapping[str, Any],
    *,
    text: str,
    chat_id: str | int | None = None,
    parse_mode: str | None = None,
    message_thread_id: int | None = None,
    timeout: float = 15.0,
) -> None:
    """Synchronous alert-channel sender with encrypted-token support."""
    token = bot_token_from_settings(settings)
    if chat_id is not None:
        targets = [
            (
                _chat_id(chat_id),
                message_thread_id
                if message_thread_id is not None
                else _thread_id(settings.get("telegram_bot_message_thread_id")),
            )
        ]
    else:
        targets = notification_targets(settings)
    if not token or not any(item[0] for item in targets):
        raise TelegramBotApiError("sendMessage", "Telegram Bot target is not configured")
    last_error: TelegramBotApiError | None = None
    sent = 0
    method = "sendMessage"
    for target, thread_id in targets:
        if not target:
            continue
        payload = _message_payload(
            chat_id=target,
            text=text,
            parse_mode=parse_mode,
            message_thread_id=thread_id,
        )
        for attempt in range(2):
            try:
                with httpx.Client(
                    **httpx_client_kwargs(timeout=max(1.0, float(timeout)))
                ) as client:
                    response = client.post(
                        f"{BOT_API_BASE}/bot{token}/{method}", json=payload
                    )
            except httpx.RequestError:
                last_error = _api_error(method=method, token=token, network=True)
            else:
                if response.status_code >= 400:
                    last_error = _api_error(
                        method=method, token=token, response=response
                    )
                else:
                    try:
                        body = response.json()
                    except (ValueError, TypeError):
                        body = None
                    if isinstance(body, dict) and body.get("ok") is True:
                        sent += 1
                        last_error = None
                        break
                    last_error = _api_error(
                        method=method, token=token, response=response
                    )
            if attempt == 0 and last_error is not None:
                time.sleep(min(last_error.retry_after or 1, 3))
    if sent == 0:
        raise last_error or TelegramBotApiError(
            "sendMessage", "Telegram Bot target is not configured"
        )
