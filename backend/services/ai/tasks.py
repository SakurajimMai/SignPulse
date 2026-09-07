from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import complete_json

TASK_GAMES_REFINE = "games.refine_caption"
TASK_MANGA_REFINE = "manga.refine_caption"

DEFAULT_GAMES_REFINE_PROMPT = """你是游戏网站的发布助手。根据 Telegram 频道帖原文，整理成可直接发布的文案。
只返回 JSON，字段如下：
{
  "title": "简洁作品名，去掉表情、装饰符号和无关广告",
  "summary": "干净的中文简介，保留剧情/玩法，去掉下载地址、入正、求订阅、关注频道、汉化组广告",
  "tags": ["SLG", "PC"],
  "studio": "厂商或制作组，没有则空字符串",
  "category_ids": [640, 637],
  "skip": false,
  "skip_reason": ""
}
分类 ID：640=PC游戏，641=安卓游戏，637=汉化游戏，639=原生/未汉化。
若原文不是可发布的游戏资源帖（纯广告、求订阅、无作品信息），设 skip=true 并填写 skip_reason。
不要编造原文没有的剧情。标题不超过 80 字，简介不超过 1200 字，标签最多 8 个。"""

DEFAULT_MANGA_REFINE_PROMPT = """你是漫画采集助手。根据 Telegram 标题卡/caption 提取入库元数据。
只返回 JSON，字段如下：
{
  "title": "作品名，可含章节如 第10话 / +幕后图，去掉模板行",
  "author": "作者或汉化组，没有则空字符串",
  "tags": ["标签"],
  "skip": false,
  "skip_reason": ""
}
去掉这些模板：原作、角色、来源、更新时间、在评论区观看、telegraph 观看、下载链接、网盘、求订阅。
如果是广告/求下载而不是漫画标题卡，设 skip=true。
不要编造。标题不超过 120 字，标签最多 12 个。"""


@dataclass(frozen=True)
class TaskSpec:
    id: str
    system: str
    user_template: str


TASKS: dict[str, TaskSpec] = {
    TASK_GAMES_REFINE: TaskSpec(
        id=TASK_GAMES_REFINE,
        system=DEFAULT_GAMES_REFINE_PROMPT,
        user_template=(
            "频道原文：\n{caption}\n\n"
            "规则解析结果（仅供参考）：\n{heuristic}\n\n"
            "分类对照：{categories}"
        ),
    ),
    TASK_MANGA_REFINE: TaskSpec(
        id=TASK_MANGA_REFINE,
        system=DEFAULT_MANGA_REFINE_PROMPT,
        user_template=(
            "标题卡/caption：\n{caption}\n\n"
            "规则解析结果（仅供参考）：\n{heuristic}"
        ),
    ),
}


def default_prompt(task_id: str) -> str:
    spec = TASKS.get(task_id)
    return spec.system if spec else ""


async def run_json_task(
    task_id: str,
    *,
    variables: dict[str, Any],
    prompt_override: str | None = None,
) -> dict[str, Any] | None:
    spec = TASKS[task_id]
    system = str(prompt_override or "").strip() or spec.system
    try:
        user = spec.user_template.format(**variables)
    except KeyError:
        user = spec.user_template
    return await complete_json(system, user)
