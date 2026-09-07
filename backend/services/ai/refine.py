from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from backend.services.games.parser import ParsedGame, clean_game_summary, split_tags
from backend.services.manga.hmw.telegram_link import safe_folder_name

from .client import ai_available
from .tasks import TASK_GAMES_REFINE, TASK_MANGA_REFINE, run_json_task

logger = logging.getLogger("backend.ai.refine")

_CATEGORY_HINT = "640=PC游戏, 641=安卓游戏, 637=汉化游戏, 639=原生游戏"


@dataclass
class GameRefineResult:
    parsed: ParsedGame
    used: bool = False
    skip: bool = False
    reason: str = ""


def _string_list(raw: Any, *, limit: int) -> list[str]:
    if isinstance(raw, str):
        return split_tags(raw)[:limit]
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        text = str(item or "").strip()[:40]
        if text and text not in values:
            values.append(text)
        if len(values) >= limit:
            break
    return values


def _int_list(raw: Any, allowed: set[int]) -> list[int]:
    if not isinstance(raw, list):
        return []
    values: list[int] = []
    for item in raw:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number in allowed and number not in values:
            values.append(number)
    return values


async def refine_game_caption(
    settings: Any,
    parsed: ParsedGame,
    caption: str,
) -> GameRefineResult:
    if not getattr(settings, "ai_enabled", False):
        return GameRefineResult(parsed=parsed)
    if not str(caption or "").strip():
        return GameRefineResult(parsed=parsed)
    if not ai_available():
        logger.warning("游戏 AI 已打开，但系统设置里还没有可用的模型 Key")
        return GameRefineResult(parsed=parsed)
    payload = await run_json_task(
        TASK_GAMES_REFINE,
        prompt_override=str(getattr(settings, "ai_refine_prompt", "") or ""),
        variables={
            "caption": str(caption)[:8000],
            "heuristic": json.dumps(parsed.as_dict(), ensure_ascii=False)[:4000],
            "categories": _CATEGORY_HINT,
        },
    )
    if not payload:
        return GameRefineResult(parsed=parsed)
    skip = bool(payload.get("skip"))
    reason = str(payload.get("skip_reason") or "").strip()[:200]
    title = str(payload.get("title") or "").strip()
    if title:
        parsed.title = safe_folder_name(title, fallback=parsed.title or "未命名游戏")[:160]
    summary = clean_game_summary(str(payload.get("summary") or ""))
    if summary:
        parsed.summary = summary[:8000]
    tags = _string_list(payload.get("tags"), limit=8)
    if tags:
        parsed.tags = tags
    studio = str(payload.get("studio") or "").strip()
    if studio:
        parsed.studio = studio[:80]
    categories = _int_list(payload.get("category_ids"), {637, 639, 640, 641})
    if categories:
        parsed.category_ids = categories
    return GameRefineResult(parsed=parsed, used=True, skip=skip, reason=reason)


async def refine_manga_chapter(settings: Any, chapter: Any) -> Any:
    """用 AI 整理章节标题/作者/标签。skip 时保留规则解析结果，不丢章节。"""
    if not getattr(settings, "ai_enabled", False):
        return chapter
    if not ai_available():
        logger.warning("漫画 AI 已打开，但系统设置里还没有可用的模型 Key")
        return chapter
    captions: list[str] = []
    for page in getattr(chapter, "pages", None) or []:
        for raw in (getattr(page, "metadata_caption", None), getattr(page, "caption", None)):
            text = str(raw or "").strip()
            if text and text not in captions:
                captions.append(text)
    if getattr(chapter, "title_hint", None):
        captions.insert(0, str(chapter.title_hint))
    blob = "\n\n".join(captions).strip()
    if not blob:
        return chapter
    heuristic = {
        "title": getattr(chapter, "title_hint", None),
        "author": getattr(chapter, "author_hint", None),
        "tags": list(getattr(chapter, "tags", None) or []),
    }
    payload = await run_json_task(
        TASK_MANGA_REFINE,
        prompt_override=str(getattr(settings, "ai_refine_prompt", "") or ""),
        variables={
            "caption": blob[:6000],
            "heuristic": json.dumps(heuristic, ensure_ascii=False)[:2000],
        },
    )
    if not payload or payload.get("skip"):
        return chapter
    title = str(payload.get("title") or "").strip()
    if title:
        chapter.title_hint = title[:200]
    author = str(payload.get("author") or "").strip()
    if author:
        chapter.author_hint = author[:80]
    tags = _string_list(payload.get("tags"), limit=12)
    if tags:
        merged = list(getattr(chapter, "tags", None) or [])
        for tag in tags:
            if tag not in merged:
                merged.append(tag)
        chapter.tags = merged[:16]
    return chapter
