from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.ai.client import parse_json_object
from backend.services.ai.refine import refine_game_caption, refine_manga_chapter
from backend.services.ai.tasks import TASK_GAMES_REFINE, default_prompt
from backend.services.games.config import (
    games_config_public,
    load_games_settings,
    save_games_settings,
)
from backend.services.games.parser import parse_game_caption
from backend.services.manga.assembler import PendingChapter, PendingPage
from backend.services.manga.config import (
    load_manga_settings,
    manga_config_public,
    save_manga_settings,
)


def test_parse_json_object_strips_fence_and_repairs():
    data = parse_json_object('```json\n{"title": "巨乳幻想3", "skip": false}\n```')
    assert data["title"] == "巨乳幻想3"
    assert data["skip"] is False
    repaired = parse_json_object('{"title": "ok", "tags": ["SLG"],}')
    assert repaired["title"] == "ok"
    assert repaired["tags"] == ["SLG"]


@pytest.mark.asyncio
async def test_refine_game_caption_merges_model_output(monkeypatch):
    parsed = parse_game_caption("🤩 测试游戏\n#SLG #PC\n求订阅")

    async def fake_task(_task_id, **kwargs):
        assert "caption" in kwargs["variables"]
        return {
            "title": "测试游戏",
            "summary": "干净简介",
            "tags": ["SLG", "步兵"],
            "studio": "AniCore",
            "category_ids": [640, 637],
            "skip": False,
        }

    monkeypatch.setattr("backend.services.ai.refine.ai_available", lambda: True)
    monkeypatch.setattr("backend.services.ai.refine.run_json_task", fake_task)
    settings = SimpleNamespace(ai_enabled=True, ai_refine_prompt="")
    result = await refine_game_caption(settings, parsed, "原文")
    assert result.used is True
    assert result.skip is False
    assert result.parsed.title == "测试游戏"
    assert result.parsed.summary == "干净简介"
    assert result.parsed.tags == ["SLG", "步兵"]
    assert result.parsed.studio == "AniCore"
    assert result.parsed.category_ids == [640, 637]


@pytest.mark.asyncio
async def test_refine_game_caption_can_skip(monkeypatch):
    parsed = parse_game_caption("关注频道领福利")

    async def fake_task(_task_id, **_kwargs):
        return {"skip": True, "skip_reason": "广告帖"}

    monkeypatch.setattr("backend.services.ai.refine.ai_available", lambda: True)
    monkeypatch.setattr("backend.services.ai.refine.run_json_task", fake_task)
    result = await refine_game_caption(
        SimpleNamespace(ai_enabled=True, ai_refine_prompt="自定义"),
        parsed,
        "关注频道领福利",
    )
    assert result.used is True
    assert result.skip is True
    assert result.reason == "广告帖"


@pytest.mark.asyncio
async def test_refine_game_caption_disabled_keeps_parser():
    parsed = parse_game_caption("游戏名称：原标题")
    result = await refine_game_caption(
        SimpleNamespace(ai_enabled=False, ai_refine_prompt=""),
        parsed,
        "游戏名称：原标题",
    )
    assert result.used is False
    assert result.parsed.title == "原标题"


@pytest.mark.asyncio
async def test_refine_manga_chapter_updates_metadata(monkeypatch):
    async def fake_task(_task_id, **kwargs):
        assert "caption" in kwargs["variables"]
        return {
            "title": "乱世书 第11话",
            "author": "点点滴滴",
            "tags": ["NTR"],
            "skip": False,
        }

    monkeypatch.setattr("backend.services.ai.refine.ai_available", lambda: True)
    monkeypatch.setattr("backend.services.ai.refine.run_json_task", fake_task)
    chapter = PendingChapter(
        bucket_key="1:r:2",
        chat_id=1,
        chat_title="g",
        pages=[
            PendingPage(
                message_id=2,
                local_path=Path("/tmp/2.jpg"),
                filename="2.jpg",
                caption="[点点滴滴]乱世书 更新时间：今天",
                grouped_id=None,
                date=datetime.now(timezone.utc),
                sender_id=1,
                reply_to_msg_id=2,
            )
        ],
        title_hint="乱世书 更新时间：今天",
        tags=["旧"],
    )
    updated = await refine_manga_chapter(
        SimpleNamespace(ai_enabled=True, ai_refine_prompt=""),
        chapter,
    )
    assert updated.title_hint == "乱世书 第11话"
    assert updated.author_hint == "点点滴滴"
    assert "NTR" in updated.tags


def test_games_and_manga_ai_settings_persist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GAMES_CONFIG_FILE", str(tmp_path / ".games_config.json"))
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setattr("backend.services.ai.client.ai_available", lambda: False)

    games = save_games_settings(
        {
            "ai_enabled": True,
            "ai_skip_non_games": False,
            "ai_refine_prompt": "只整理标题",
        }
    )
    assert games.ai_enabled is True
    assert games.ai_skip_non_games is False
    assert load_games_settings().ai_refine_prompt == "只整理标题"
    public = games_config_public(games)
    assert public["ai_refine_prompt"] == "只整理标题"
    assert public["ai_refine_prompt_default"] == default_prompt(TASK_GAMES_REFINE)
    assert "category_ids" in public["ai_refine_prompt_default"]

    manga = save_manga_settings(
        {"ai_enabled": True, "ai_refine_prompt": "只提取作品名"}
    )
    assert manga.ai_enabled is True
    assert load_manga_settings().ai_refine_prompt == "只提取作品名"
    manga_public = manga_config_public(manga)
    assert manga_public["ai_enabled"] is True
    assert "title" in manga_public["ai_refine_prompt_default"]


@pytest.mark.asyncio
async def test_custom_prompt_override_is_passed(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_complete(system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return {"title": "ok"}

    monkeypatch.setattr("backend.services.ai.tasks.complete_json", fake_complete)

    from backend.services.ai.tasks import run_json_task

    result = await run_json_task(
        TASK_GAMES_REFINE,
        prompt_override="自定义说明",
        variables={
            "caption": "原文",
            "heuristic": "{}",
            "categories": "640",
        },
    )
    assert result == {"title": "ok"}
    assert captured["system"] == "自定义说明"
    assert "原文" in captured["user"]
