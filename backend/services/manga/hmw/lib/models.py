from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(slots=True)
class ChapterCandidate:
    directory: Path
    name: str
    number: float
    title: str
    images: list[Path]
    action: Literal["upsert", "skip"] = "upsert"


@dataclass(slots=True)
class SourceDiscovery:
    root: Path
    cover: Path
    chapters: list[ChapterCandidate]


@dataclass(slots=True)
class ConvertedImage:
    source: Path
    destination: Path
    filename: str
    width: int
    height: int
    size: int
    sha256: str


@dataclass(slots=True)
class UploadedObject:
    source: str
    local_path: str
    key: str
    url: str
    width: int
    height: int
    completed: bool = True
    kind: Literal["cover", "page"] = "page"
    chapter_number: float | None = None
    page_index: int | None = None


@dataclass(slots=True)
class TaskState:
    task_id: str
    source_path: str
    stage: str = "discovered"
    manga_slug: str = ""
    manga_payload: dict[str, Any] = field(default_factory=dict)
    chapter_mappings: list[dict[str, Any]] = field(default_factory=list)
    expected_version: str | None = None
    preflight: dict[str, Any] = field(default_factory=dict)
    uploaded_objects: list[UploadedObject] = field(default_factory=list)
    database_committed: bool = False
    publish_result: dict[str, Any] = field(default_factory=dict)
    orphaned_keys: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskState":
        data = dict(value)
        data["uploaded_objects"] = [
            item if isinstance(item, UploadedObject) else UploadedObject(**item)
            for item in data.get("uploaded_objects", [])
        ]
        return cls(**data)
