from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.services.telegram_bot import runtime
from backend.services.telegram_bot.runtime import TelegramBotRuntime


class FakeBotApi:
    instances: list["FakeBotApi"] = []
    webhook_url = ""
    sticky_webhook = False
    delete_webhook_error: Exception | None = None
    get_updates_errors: list[Exception] = []
    empty_updates_before_block = 0

    def __init__(self, token: str) -> None:
        self.token = token
        self.messages: list[dict] = []
        self.answers: list[dict] = []
        self.closed = False
        self.calls = 0
        self.offsets: list[int | None] = []
        self.default_menu_calls: list[dict] = []
        self.delete_webhook_calls: list[dict] = []
        self.command_calls: list[dict] = []
        self.delete_command_calls: list[dict] = []
        self.menu_calls: list[dict] = []
        self.block = asyncio.Event()
        type(self).instances.append(self)

    async def close(self):
        self.closed = True
        self.block.set()

    async def get_webhook_info(self):
        return {"url": type(self).webhook_url}

    async def delete_webhook(self, *, drop_pending_updates: bool = True):
        if type(self).delete_webhook_error is not None:
            raise type(self).delete_webhook_error
        self.delete_webhook_calls.append(
            {"drop_pending_updates": drop_pending_updates}
        )
        if not type(self).sticky_webhook:
            type(self).webhook_url = ""

    async def get_me(self):
        return {
            "id": 9001,
            "is_bot": True,
            "username": "ops_bot",
            "first_name": "Ops",
        }

    async def set_my_commands(self, commands, *, scope=None, language_code=None):
        call: dict = {"commands": list(commands)}
        if scope is not None:
            call["scope"] = scope
        if language_code is not None:
            call["language_code"] = language_code
        self.command_calls.append(call)
        self.commands = commands

    async def delete_my_commands(self, *, scope=None, language_code=None):
        call: dict = {}
        if scope is not None:
            call["scope"] = scope
        if language_code is not None:
            call["language_code"] = language_code
        self.delete_command_calls.append(call)

    async def set_commands_menu_button(self, **payload):
        self.menu_calls.append(payload)

    async def set_chat_menu_button(self, **payload):
        self.menu = payload

    async def set_default_chat_menu_button(self, **payload):
        self.default_menu_calls.append(payload)

    async def get_updates(self, **kwargs):
        self.calls += 1
        self.offsets.append(kwargs.get("offset"))
        if type(self).get_updates_errors:
            raise type(self).get_updates_errors.pop(0)
        if type(self).empty_updates_before_block > 0:
            type(self).empty_updates_before_block -= 1
            return []
        if self.calls == 1:
            return [
                {
                    "update_id": 10,
                    "message": {
                        "from": {"id": 42},
                        "chat": {"id": 42, "type": "private"},
                        "text": "/id",
                    },
                }
            ]
        await self.block.wait()
        return []

    async def send_message(self, **payload):
        self.messages.append(payload)
        return {}

    async def answer_callback_query(self, callback_query_id, **payload):
        self.answers.append({"id": callback_query_id, **payload})


def _patch_runtime(monkeypatch, tmp_path, *, primary=True, enabled=True):
    settings = {
        "telegram_bot_control_enabled": enabled,
        "telegram_bot_token": "123456:runtime-secret-token",
        "telegram_bot_allowed_user_ids": [],
        "telegram_bot_mini_app_url": "https://panel.example.com/mini-app",
    }
    monkeypatch.setattr(
        runtime,
        "get_config_service",
        lambda: SimpleNamespace(get_global_settings=lambda: settings),
    )
    monkeypatch.setattr(runtime, "has_scheduler_lock", lambda: primary)
    monkeypatch.setattr(
        runtime,
        "get_settings",
        lambda: SimpleNamespace(resolve_base_dir=lambda: tmp_path),
    )
    monkeypatch.setattr(runtime, "TelegramBotApi", FakeBotApi)
    FakeBotApi.instances.clear()
    FakeBotApi.webhook_url = ""
    FakeBotApi.sticky_webhook = False
    FakeBotApi.delete_webhook_error = None
    FakeBotApi.get_updates_errors = []
    FakeBotApi.empty_updates_before_block = 0
    return settings


@pytest.mark.asyncio
async def test_runtime_public_lifecycle_polls_and_persists_offset(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path)
    bot_runtime = TelegramBotRuntime()
    await bot_runtime.start()
    for _ in range(100):
        if FakeBotApi.instances and FakeBotApi.instances[0].messages:
            break
        await asyncio.sleep(0.01)

    status = bot_runtime.status()
    assert status["state"] == "running"
    assert status["running"] is True
    assert status["bot"]["username"] == "ops_bot"
    assert "runtime-secret-token" not in repr(status)
    assert "42" in FakeBotApi.instances[0].messages[0]["text"]
    state = json.loads((tmp_path / ".telegram_bot_state.json").read_text())
    assert state["version"] == 1
    assert state["bot_id"] == 9001
    assert len(state["token_fingerprint"]) == 24
    assert state["last_update_id"] == 11
    assert state["managed_menu_user_ids"] == []

    stopped = await bot_runtime.stop()
    assert stopped["state"] == "stopped"
    assert FakeBotApi.instances[0].closed is True


@pytest.mark.asyncio
async def test_runtime_clears_saved_offset_when_bot_token_rotates(monkeypatch, tmp_path):
    settings = _patch_runtime(monkeypatch, tmp_path)
    first_runtime = TelegramBotRuntime()
    await first_runtime.reconcile()
    for _ in range(100):
        if FakeBotApi.instances and FakeBotApi.instances[0].calls:
            break
        await asyncio.sleep(0.01)

    first_api = FakeBotApi.instances[0]
    assert first_api.offsets[0] is None
    first_state = json.loads((tmp_path / ".telegram_bot_state.json").read_text())
    await first_runtime.stop()

    settings["telegram_bot_token"] = "654321:rotated-runtime-token"
    second_runtime = TelegramBotRuntime()
    await second_runtime.reconcile()
    for _ in range(100):
        if len(FakeBotApi.instances) > 1 and FakeBotApi.instances[1].calls:
            break
        await asyncio.sleep(0.01)

    second_api = FakeBotApi.instances[1]
    rotated_state = json.loads((tmp_path / ".telegram_bot_state.json").read_text())
    assert second_api.offsets[0] is None
    assert rotated_state["bot_id"] == first_state["bot_id"]
    assert rotated_state["token_fingerprint"] != first_state["token_fingerprint"]
    assert rotated_state["last_update_id"] == 11
    await second_runtime.stop()


@pytest.mark.asyncio
async def test_runtime_registers_commands_and_operator_mini_app_menu(
    monkeypatch, tmp_path
):
    settings = _patch_runtime(monkeypatch, tmp_path)
    settings["telegram_bot_allowed_user_ids"] = [42]
    bot_runtime = TelegramBotRuntime()
    await bot_runtime.reconcile()

    api = FakeBotApi.instances[0]
    scopes = [call.get("scope") for call in api.command_calls]
    assert None in scopes
    assert {"type": "all_private_chats"} in scopes
    assert {"type": "chat", "chat_id": 42} in scopes
    assert [item["command"] for item in api.commands] == [
        "start",
        "help",
        "status",
        "tasks",
        "run",
        "alerts",
        "id",
    ]
    assert {"language_code": "zh"} in api.delete_command_calls
    assert all(call.get("language_code") == "zh" for call in api.delete_command_calls)
    assert api.menu_calls == [{}]
    assert api.menu == {
        "url": "https://panel.example.com/mini-app",
        "text": "打开 Mini App",
        "chat_id": 42,
    }
    assert api.default_menu_calls == []

    settings["telegram_bot_mini_app_url"] = None
    await bot_runtime.reconcile()
    second_api = FakeBotApi.instances[1]
    assert api.closed is True
    assert second_api.menu_calls == [{}, {"chat_id": 42}]
    assert not hasattr(second_api, "menu")
    assert "runtime-secret-token" not in repr(bot_runtime.status())
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_runtime_removes_menu_for_revoked_operator(monkeypatch, tmp_path):
    settings = _patch_runtime(monkeypatch, tmp_path)
    settings["telegram_bot_allowed_user_ids"] = [42]
    bot_runtime = TelegramBotRuntime()
    await bot_runtime.reconcile()

    settings["telegram_bot_allowed_user_ids"] = []
    await bot_runtime.reconcile()

    second_api = FakeBotApi.instances[1]
    assert second_api.default_menu_calls == [{"chat_id": 42}]
    state = json.loads((tmp_path / ".telegram_bot_state.json").read_text())
    assert state["managed_menu_user_ids"] == []
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_runtime_resets_operator_menu_when_control_disabled(
    monkeypatch, tmp_path
):
    settings = _patch_runtime(monkeypatch, tmp_path)
    settings["telegram_bot_allowed_user_ids"] = [42]
    bot_runtime = TelegramBotRuntime()
    await bot_runtime.reconcile()
    api = FakeBotApi.instances[0]

    settings["telegram_bot_control_enabled"] = False
    status = await bot_runtime.reconcile()

    assert status["state"] == "disabled"
    assert api.default_menu_calls == [{"chat_id": 42}]
    state = json.loads((tmp_path / ".telegram_bot_state.json").read_text())
    assert state["managed_menu_user_ids"] == []
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_runtime_clears_transient_poll_error_after_recovery(
    monkeypatch, tmp_path
):
    from backend.services.telegram_bot.api import TelegramBotApiError

    _patch_runtime(monkeypatch, tmp_path)
    FakeBotApi.get_updates_errors = [
        TelegramBotApiError("getUpdates", "temporary", status_code=503)
    ]
    FakeBotApi.empty_updates_before_block = 1
    original_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await original_sleep(0)

    monkeypatch.setattr(runtime.asyncio, "sleep", fast_sleep)
    bot_runtime = TelegramBotRuntime()
    await bot_runtime.reconcile()
    api = FakeBotApi.instances[0]
    for _ in range(100):
        if api.calls >= 2 and bot_runtime.status()["last_error"] == "":
            break
        await original_sleep(0.01)

    assert api.calls >= 2
    assert bot_runtime.status()["state"] == "running"
    assert bot_runtime.status()["last_error"] == ""
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_notification_only_does_not_poll_or_delete_webhook(monkeypatch, tmp_path):
    settings = _patch_runtime(monkeypatch, tmp_path, enabled=False)
    settings["telegram_bot_notify_enabled"] = True
    settings["telegram_bot_allowed_user_ids"] = [42]
    FakeBotApi.webhook_url = "https://hooks.example.com/telegram"

    bot_runtime = TelegramBotRuntime()
    status = await bot_runtime.reconcile()

    assert status["state"] == "disabled"
    assert status["control_enabled"] is False
    assert status["running"] is False
    assert FakeBotApi.instances == []
    assert FakeBotApi.webhook_url == "https://hooks.example.com/telegram"
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_disabled_runtime_cleans_persisted_operator_menu_after_restart(
    monkeypatch, tmp_path
):
    _patch_runtime(monkeypatch, tmp_path, enabled=False)
    (tmp_path / ".telegram_bot_state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "bot_id": 9001,
                "token_fingerprint": runtime._bot_token_fingerprint(
                    "123456:runtime-secret-token"
                ),
                "last_update_id": 11,
                "managed_menu_user_ids": [42],
            }
        ),
        encoding="utf-8",
    )
    FakeBotApi.webhook_url = "https://hooks.example.com/telegram"

    bot_runtime = TelegramBotRuntime()
    status = await bot_runtime.reconcile()

    assert status["state"] == "disabled"
    assert len(FakeBotApi.instances) == 1
    api = FakeBotApi.instances[0]
    assert api.default_menu_calls == [{"chat_id": 42}]
    assert api.calls == 0
    assert api.delete_webhook_calls == []
    assert FakeBotApi.webhook_url == "https://hooks.example.com/telegram"
    assert api.closed is True
    state = json.loads((tmp_path / ".telegram_bot_state.json").read_text())
    assert state["managed_menu_user_ids"] == []
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_supervisor_applies_control_setting_changes(monkeypatch, tmp_path):
    settings = _patch_runtime(monkeypatch, tmp_path, enabled=False)
    bot_runtime = TelegramBotRuntime()
    await bot_runtime.start()

    for _ in range(100):
        if bot_runtime.status()["state"] == "disabled":
            break
        await asyncio.sleep(0.01)
    assert bot_runtime.status()["state"] == "disabled"

    settings["telegram_bot_control_enabled"] = True
    bot_runtime.request_reconcile()
    for _ in range(100):
        if bot_runtime.status()["running"]:
            break
        await asyncio.sleep(0.01)
    assert bot_runtime.status()["state"] == "running"
    api = FakeBotApi.instances[0]

    settings["telegram_bot_control_enabled"] = False
    bot_runtime.request_reconcile()
    for _ in range(100):
        if bot_runtime.status()["state"] == "disabled":
            break
        await asyncio.sleep(0.01)
    assert bot_runtime.status()["running"] is False
    assert api.closed is True
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_no_proxy_change_rebuilds_polling_client(monkeypatch, tmp_path):
    settings = _patch_runtime(monkeypatch, tmp_path)
    settings.update(
        proxy_enabled=True,
        global_proxy="http://proxy.example.com:8080",
        proxy_no_proxy="localhost",
    )
    bot_runtime = TelegramBotRuntime()
    await bot_runtime.reconcile()
    first_api = FakeBotApi.instances[0]

    settings["proxy_no_proxy"] = "localhost,api.telegram.org"
    await bot_runtime.reconcile()

    assert first_api.closed is True
    assert len(FakeBotApi.instances) == 2
    assert bot_runtime.status()["running"] is True
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_runtime_does_not_poll_on_secondary_and_clears_webhook_on_primary(
    monkeypatch, tmp_path
):
    _patch_runtime(monkeypatch, tmp_path, primary=False)
    secondary = TelegramBotRuntime()
    status = await secondary.reconcile()
    assert status["state"] == "standby"
    assert FakeBotApi.instances == []

    monkeypatch.setattr(runtime, "has_scheduler_lock", lambda: True)
    FakeBotApi.webhook_url = "https://hooks.example.com/telegram"
    primary = TelegramBotRuntime()
    status = await primary.reconcile()
    api = FakeBotApi.instances[0]
    assert status["state"] == "running"
    assert status["running"] is True
    assert status["webhook_conflict"] is False
    assert status["last_error"] == ""
    assert api.delete_webhook_calls == [{"drop_pending_updates": True}]
    assert [item["command"] for item in api.commands] == [
        "start",
        "help",
        "status",
        "tasks",
        "run",
        "alerts",
        "id",
    ]
    await primary.stop()


@pytest.mark.asyncio
async def test_runtime_reports_webhook_conflict_when_delete_fails(monkeypatch, tmp_path):
    from backend.services.telegram_bot.api import TelegramBotApiError

    _patch_runtime(monkeypatch, tmp_path)
    FakeBotApi.webhook_url = "https://hooks.example.com/telegram"
    FakeBotApi.delete_webhook_error = TelegramBotApiError(
        "deleteWebhook", "failed", status_code=400
    )
    bot_runtime = TelegramBotRuntime()
    status = await bot_runtime.reconcile()
    assert status["state"] == "webhook_conflict"
    assert status["webhook_conflict"] is True
    assert status["running"] is False
    assert status["last_error"] == "webhook_delete_failed_400"
    assert FakeBotApi.instances[0].closed is True
    await bot_runtime.stop()


@pytest.mark.asyncio
async def test_runtime_reports_webhook_conflict_when_url_remains(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path)
    FakeBotApi.webhook_url = "https://hooks.example.com/telegram"
    FakeBotApi.sticky_webhook = True
    bot_runtime = TelegramBotRuntime()
    status = await bot_runtime.reconcile()
    assert status["state"] == "webhook_conflict"
    assert status["last_error"] == "webhook_configured"
    assert FakeBotApi.instances[0].delete_webhook_calls == [
        {"drop_pending_updates": True}
    ]
    assert FakeBotApi.instances[0].closed is True
    await bot_runtime.stop()
