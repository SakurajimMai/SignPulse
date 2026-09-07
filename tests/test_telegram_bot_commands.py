from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.telegram_bot import commands
from backend.services.telegram_bot.commands import BotCommandHandler


class FakeApi:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.answers: list[dict] = []

    async def send_message(self, **payload):
        self.messages.append(payload)
        return {}

    async def answer_callback_query(self, callback_query_id, **payload):
        self.answers.append({"id": callback_query_id, **payload})


class FakePlane:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str, str, str]] = []

    def list_tasks(self):
        return [
            {
                "name": "daily",
                "account_name": "*",
                "account_names": ["acct-a", "acct-b"],
                "enabled": True,
            },
            {
                "name": "disabled",
                "account_name": "acct-a",
                "account_names": ["acct-a"],
                "enabled": False,
            },
        ]

    def bootstrap(self):
        return {
            "system": {
                "status": "ready",
                "accounts_connected": 1,
                "accounts_total": 1,
                "tasks_enabled": 1,
                "tasks_total": 2,
                "active_runs_total": 0,
            }
        }

    def recent_alerts(self, limit=5):
        return {"items": [], "rules": [], "total": 0}

    async def start_task_run(
        self, account_name, task_name, request_id=None, *, actor_id="telegram"
    ):
        self.runs.append((account_name, task_name, request_id, actor_id))
        return {"run_id": "run-1", "state": "running"}


def _message(text: str, *, user_id: int = 42, chat_type: str = "private") -> dict:
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": chat_type},
            "text": text,
        },
    }


def _callback(callback_id: str, data: str, *, user_id: int = 42) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": user_id, "type": "private"}},
            "data": data,
        },
    }


@pytest.mark.asyncio
async def test_tasks_uses_concrete_aggregate_account_and_two_step_confirmation(
    monkeypatch,
):
    api = FakeApi()
    plane = FakePlane()
    settings = {
        "telegram_bot_control_enabled": True,
        "telegram_bot_allowed_user_ids": [42],
        "telegram_bot_mini_app_url": "https://panel.example.com/mini-app",
    }
    monkeypatch.setattr(
        commands,
        "get_config_service",
        lambda: SimpleNamespace(get_global_settings=lambda: settings),
    )
    monkeypatch.setattr(commands, "get_control_plane", lambda: plane)
    handler = BotCommandHandler(api)

    await handler.handle_update(_message("/tasks"))
    keyboard = api.messages[-1]["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 2
    assert "daily" in keyboard[0][0]["text"]
    assert "acct-a" in keyboard[0][0]["text"]
    assert "acct-b" in keyboard[1][0]["text"]
    prepare_data = keyboard[0][0]["callback_data"]

    await handler.handle_update(_callback("prepare-callback", prepare_data))
    confirm_data = api.messages[-1]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    assert confirm_data.startswith("run:confirm:")
    await handler.handle_update(_callback("confirm-callback", confirm_data))

    assert plane.runs[0][0:2] == ("acct-a", "daily")
    assert [item["id"] for item in api.answers] == [
        "prepare-callback",
        "confirm-callback",
    ]


@pytest.mark.asyncio
async def test_disabled_and_unauthorized_commands_cannot_run(monkeypatch):
    api = FakeApi()
    plane = FakePlane()
    settings = {
        "telegram_bot_control_enabled": True,
        "telegram_bot_allowed_user_ids": [42],
    }
    monkeypatch.setattr(
        commands,
        "get_config_service",
        lambda: SimpleNamespace(get_global_settings=lambda: settings),
    )
    monkeypatch.setattr(commands, "get_control_plane", lambda: plane)
    handler = BotCommandHandler(api)

    await handler.handle_update(_message("/run disabled acct-a"))
    assert "\u505c\u7528" in api.messages[-1]["text"]
    assert plane.runs == []

    await handler.handle_update(_message("/run daily acct-a", user_id=99))
    assert "99" in api.messages[-1]["text"]
    assert plane.runs == []

    await handler.handle_update(_callback("unknown", "unexpected", user_id=42))
    assert api.answers[-1]["id"] == "unknown"
    assert api.answers[-1]["show_alert"] is True


@pytest.mark.asyncio
async def test_control_switch_blocks_allowed_user_commands_and_callbacks(monkeypatch):
    api = FakeApi()
    plane = FakePlane()
    settings = {
        "telegram_bot_control_enabled": False,
        "telegram_bot_allowed_user_ids": [42],
    }
    monkeypatch.setattr(
        commands,
        "get_config_service",
        lambda: SimpleNamespace(get_global_settings=lambda: settings),
    )
    monkeypatch.setattr(commands, "get_control_plane", lambda: plane)
    handler = BotCommandHandler(api)

    await handler.handle_update(_message("/run daily acct-a"))
    assert "尚未启用" in api.messages[-1]["text"]
    await handler.handle_update(_callback("disabled", "show:tasks"))
    assert api.answers[-1]["show_alert"] is True
    assert plane.runs == []


@pytest.mark.asyncio
async def test_empty_allowlist_keeps_id_discovery_but_rejects_control(monkeypatch):
    api = FakeApi()
    plane = FakePlane()
    settings = {
        "telegram_bot_control_enabled": True,
        "telegram_bot_allowed_user_ids": [],
    }
    monkeypatch.setattr(
        commands,
        "get_config_service",
        lambda: SimpleNamespace(get_global_settings=lambda: settings),
    )
    monkeypatch.setattr(commands, "get_control_plane", lambda: plane)
    handler = BotCommandHandler(api)

    await handler.handle_update(_message("/id", user_id=99))
    assert "99" in api.messages[-1]["text"]

    await handler.handle_update(_message("/status", user_id=99))
    assert "99" in api.messages[-1]["text"]
    assert plane.runs == []


@pytest.mark.asyncio
async def test_run_command_prompts_for_aggregate_account(monkeypatch):
    api = FakeApi()
    plane = FakePlane()
    settings = {
        "telegram_bot_control_enabled": True,
        "telegram_bot_allowed_user_ids": [42],
    }
    monkeypatch.setattr(
        commands,
        "get_config_service",
        lambda: SimpleNamespace(get_global_settings=lambda: settings),
    )
    monkeypatch.setattr(commands, "get_control_plane", lambda: plane)
    handler = BotCommandHandler(api)

    await handler.handle_update(_message("/run daily"))
    keyboard = api.messages[-1]["reply_markup"]["inline_keyboard"]
    assert "多个账号" in api.messages[-1]["text"]
    assert [row[0]["text"] for row in keyboard] == ["acct-a", "acct-b"]

    await handler.handle_update(
        _callback("select", keyboard[1][0]["callback_data"])
    )
    confirm_data = api.messages[-1]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    await handler.handle_update(_callback("confirm", confirm_data))

    assert plane.runs[0][0:2] == ("acct-b", "daily")
