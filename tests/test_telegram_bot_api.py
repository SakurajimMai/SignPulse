from __future__ import annotations

import json

import httpx
import pytest

from backend.services.telegram_bot import api as api_module
from backend.services.telegram_bot.api import (
    TelegramBotApi,
    TelegramBotApiError,
    bot_token_from_settings,
    send_telegram_bot_message_sync,
    telegram_bot_ready,
)
from tg_signer.security import encrypt_secret

TOKEN = "123456:super-secret-token"


def test_token_helpers_accept_plaintext_and_encrypted(monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "telegram-bot-api-test-key")
    encrypted = encrypt_secret(TOKEN)
    assert bot_token_from_settings({"telegram_bot_token": TOKEN}) == TOKEN
    assert bot_token_from_settings({"telegram_bot_token": encrypted}) == TOKEN
    assert telegram_bot_ready(
        {
            "telegram_bot_notify_enabled": True,
            "telegram_bot_token": encrypted,
            "telegram_bot_chat_id": "42",
        }
    )
    assert telegram_bot_ready(
        {
            "telegram_bot_notify_enabled": True,
            "telegram_bot_token": encrypted,
            "telegram_bot_allowed_user_ids": [42],
        }
    )
    assert not telegram_bot_ready(
        {"telegram_bot_token": encrypted, "telegram_bot_chat_id": "42"}
    )


@pytest.mark.asyncio
async def test_async_api_redacts_token_and_suppresses_network_cause():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed URL {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        api = TelegramBotApi(TOKEN, client=client)
        with pytest.raises(TelegramBotApiError) as raised:
            await api.get_me()
    assert TOKEN not in str(raised.value)
    assert raised.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_async_api_redacts_server_description_and_reads_retry_after():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "description": f"bad token {TOKEN}",
                "parameters": {"retry_after": 7},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = TelegramBotApi(TOKEN, client=client)
        with pytest.raises(TelegramBotApiError) as raised:
            await api.get_me()
    assert TOKEN not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)
    assert raised.value.retry_after == 7


@pytest.mark.asyncio
async def test_default_menu_button_payload_and_error_are_token_safe():
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(
            400,
            json={"ok": False, "description": f"invalid token {TOKEN}"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = TelegramBotApi(TOKEN, client=client)
        await api.set_default_chat_menu_button(chat_id=42)
        with pytest.raises(TelegramBotApiError) as raised:
            await api.set_default_chat_menu_button()

    assert requests[0] == {
        "chat_id": 42,
        "menu_button": {"type": "default"},
    }
    assert raised.value.method == "setChatMenuButton"
    assert TOKEN not in str(raised.value)


@pytest.mark.asyncio
async def test_slash_command_registration_payloads_include_private_scope():
    requests: list[tuple[str, dict]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        method = str(request.url).rsplit("/", 1)[-1]
        requests.append((method, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = TelegramBotApi(TOKEN, client=client)
        await api.delete_my_commands(
            scope={"type": "all_private_chats"}, language_code="zh"
        )
        await api.set_my_commands(
            [{"command": "start", "description": "打开控制中心"}],
            scope={"type": "all_private_chats"},
        )
        await api.set_commands_menu_button(chat_id=42)

    assert requests == [
        (
            "deleteMyCommands",
            {"scope": {"type": "all_private_chats"}, "language_code": "zh"},
        ),
        (
            "setMyCommands",
            {
                "commands": [{"command": "start", "description": "打开控制中心"}],
                "scope": {"type": "all_private_chats"},
            },
        ),
        ("setChatMenuButton", {"chat_id": 42, "menu_button": {"type": "commands"}}),
    ]


@pytest.mark.asyncio
async def test_delete_webhook_payload_defaults_to_dropping_pending_updates():
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert TOKEN in str(request.url)
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = TelegramBotApi(TOKEN, client=client)
        await api.delete_webhook()
        await api.delete_webhook(drop_pending_updates=False)

    assert requests == [
        {"drop_pending_updates": True},
        {"drop_pending_updates": False},
    ]


def test_sync_sender_uses_safe_wrapper(monkeypatch):
    captured: dict = {"calls": 0}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, json):
            captured["calls"] += 1
            captured["url"] = url
            captured["json"] = json
            if captured["calls"] == 1:
                return httpx.Response(
                    503, json={"ok": False, "description": "temporarily unavailable"}
                )
            return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(api_module.time, "sleep", lambda _seconds: None)
    send_telegram_bot_message_sync(
        {
            "telegram_bot_token": TOKEN,
            "telegram_bot_chat_id": "42",
            "telegram_bot_message_thread_id": "7",
        },
        text="alert",
    )
    assert captured["json"]["chat_id"] == "42"
    assert captured["json"]["message_thread_id"] == 7
    assert captured["json"]["text"] == "alert"
    assert captured["calls"] == 2
