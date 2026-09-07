from __future__ import annotations

import json
import re
from pathlib import Path

from .models import TaskState


class TaskStateStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, task_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
        if not safe_id:
            raise ValueError("任务 ID 不能为空")
        return self.directory / f"{safe_id}.json"

    def save(self, state: TaskState) -> Path:
        target = self.path_for(state.task_id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    def load(self, task_id: str) -> TaskState:
        value = json.loads(self.path_for(task_id).read_text(encoding="utf-8"))
        return TaskState.from_dict(value)

    def list(self) -> list[TaskState]:
        paths = sorted(
            self.directory.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [TaskState.from_dict(json.loads(path.read_text(encoding="utf-8"))) for path in paths]

    @staticmethod
    def completed_keys(state: TaskState) -> set[str]:
        return {item.key for item in state.uploaded_objects if item.completed}
