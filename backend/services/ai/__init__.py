"""流水线 AI：用系统设置里的模型，在游戏/漫画等步骤上跑可改提示词。"""

from .client import ai_available, complete_json, parse_json_object
from .refine import refine_game_caption, refine_manga_chapter
from .tasks import (
    TASK_GAMES_REFINE,
    TASK_MANGA_REFINE,
    default_prompt,
    run_json_task,
)

__all__ = [
    "TASK_GAMES_REFINE",
    "TASK_MANGA_REFINE",
    "ai_available",
    "complete_json",
    "default_prompt",
    "parse_json_object",
    "refine_game_caption",
    "refine_manga_chapter",
    "run_json_task",
]
