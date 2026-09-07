from __future__ import annotations

import asyncio
import os

import pytest

from backend.services import proxy_runtime


def test_apply_proxy_environment_sets_both_cases_and_restores(monkeypatch):
    keys = proxy_runtime._PROXY_ENV_KEYS
    initial = {key: os.environ.get(key) for key in keys}
    monkeypatch.setattr(proxy_runtime, "_INITIAL_PROXY_ENV", dict(initial))

    result = proxy_runtime.apply_proxy_environment(
        {
            "enabled": True,
            "url": "socks5://user:secret@proxy.example:1080",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
    )
    assert result["enabled"] is True
    assert os.environ["HTTPS_PROXY"].startswith("socks5://")
    assert os.environ["https_proxy"] == os.environ["HTTPS_PROXY"]
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert "secret" not in result["fingerprint"]

    proxy_runtime.apply_proxy_environment(
        {"enabled": False, "url": None, "no_proxy": ""}
    )
    assert {key: os.environ.get(key) for key in keys} == initial


def test_restore_initial_proxy_environment_clears_runtime_override(monkeypatch):
    keys = proxy_runtime._PROXY_ENV_KEYS
    initial = {key: os.environ.get(key) for key in keys}
    monkeypatch.setattr(proxy_runtime, "_INITIAL_PROXY_ENV", dict(initial))

    proxy_runtime.apply_proxy_environment(
        {
            "enabled": True,
            "url": "http://proxy.example:3128",
            "no_proxy": "localhost",
        }
    )
    proxy_runtime.restore_initial_proxy_environment()

    assert {key: os.environ.get(key) for key in keys} == initial


def test_schedule_proxy_runtime_reconcile_requires_running_loop():
    assert proxy_runtime.schedule_proxy_runtime_reconcile() is None


def test_enabled_proxy_without_url_does_not_modify_environment(monkeypatch):
    before = {key: os.environ.get(key) for key in proxy_runtime._PROXY_ENV_KEYS}
    monkeypatch.setattr(proxy_runtime, "_APPLIED_FINGERPRINT", "previous")

    with pytest.raises(RuntimeError, match="代理地址不可用"):
        proxy_runtime.apply_proxy_environment(
            {"enabled": True, "url": None, "no_proxy": "localhost"}
        )

    assert {key: os.environ.get(key) for key in proxy_runtime._PROXY_ENV_KEYS} == before
    assert proxy_runtime.applied_proxy_fingerprint() == "previous"


def test_reconcile_rotates_workers_in_order(monkeypatch):
    events: list[str] = []

    class Keywords:
        async def stop(self):
            events.append("keywords-stop")

        async def restart_from_tasks(self):
            events.append("keywords-start")

    class Manga:
        async def stop_ehentai(self):
            events.append("ehentai-stop")

        async def stop(self):
            events.append("manga-stop")

        async def start_if_enabled(self):
            events.append("manga-start")

    class Bot:
        def request_reconcile(self):
            events.append("bot-wake")

    import backend.services.keyword_monitor as keyword_module
    import backend.services.manga.runtime as manga_module
    import backend.services.telegram_bot as bot_module

    monkeypatch.setattr(
        proxy_runtime,
        "apply_proxy_environment",
        lambda: {"enabled": True, "no_proxy": "localhost", "fingerprint": "abc"},
    )
    monkeypatch.setattr(keyword_module, "get_keyword_monitor_service", Keywords)
    monkeypatch.setattr(manga_module, "get_manga_runtime", Manga)
    monkeypatch.setattr(bot_module, "get_telegram_bot_runtime", Bot)
    monkeypatch.setattr(proxy_runtime, "_RECONCILE_LOCK", None)

    result = asyncio.run(proxy_runtime.reconcile_proxy_runtime())

    assert events == [
        "keywords-stop",
        "ehentai-stop",
        "manga-stop",
        "keywords-start",
        "manga-start",
        "bot-wake",
    ]
    assert result["failures"] == []


def test_schedule_coalesces_rapid_requests(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_reconcile():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {}

    async def scenario():
        monkeypatch.setattr(proxy_runtime, "reconcile_proxy_runtime", fake_reconcile)
        monkeypatch.setattr(proxy_runtime, "_RECONCILE_TASK", None)
        monkeypatch.setattr(proxy_runtime, "_RECONCILE_PENDING", False)
        first = proxy_runtime.schedule_proxy_runtime_reconcile()
        assert first is not None
        await started.wait()
        second = proxy_runtime.schedule_proxy_runtime_reconcile()
        third = proxy_runtime.schedule_proxy_runtime_reconcile()
        assert second is first
        assert third is first
        release.set()
        await first

    asyncio.run(scenario())
    # One active pass plus one pass for all saves that arrived during it.
    assert calls == 2
