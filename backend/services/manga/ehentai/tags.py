from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_BUNDLED = Path(__file__).with_name("tag_names.json")
_CATALOG: Path | None = None
_FALLBACK_NS = {
    "female": "女性",
    "male": "男性",
    "mixed": "混合",
    "language": "语言",
    "other": "其他",
    "group": "团队",
    "artist": "艺术家",
    "parody": "原作",
    "character": "角色",
    "reclass": "重新分类",
    "cosplayer": "Coser",
    "location": "地点",
    "rows": "内容索引",
}


def bundled_path() -> Path:
    return _BUNDLED


def set_catalog_path(path: Path | None) -> None:
    """运行时词库：data/ehentai/tag_names.json，更新后无需重启进程。"""
    global _CATALOG
    _CATALOG = path
    _payload.cache_clear()


def _load_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


@lru_cache(maxsize=1)
def _payload() -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {"rows": dict(_FALLBACK_NS)}
    for path in (_CATALOG, _BUNDLED):
        if path is None:
            continue
        raw = _load_file(path)
        if not raw:
            continue
        for item in raw.get("data") or []:
            ns = str(item.get("namespace") or "").strip()
            if not ns:
                continue
            names: dict[str, str] = {}
            for key, value in (item.get("data") or {}).items():
                if isinstance(value, dict) and value.get("name"):
                    names[str(key)] = str(value["name"])
                elif isinstance(value, str) and value:
                    names[str(key)] = value
            if names:
                mapping[ns] = names
        break
    return mapping


def translate_namespace(namespace: str) -> str:
    key = (namespace or "").strip().lower()
    rows = _payload().get("rows") or {}
    return rows.get(key) or _FALLBACK_NS.get(key) or key


def translate_tag(namespace: str, name: str) -> str:
    ns = (namespace or "").strip().lower()
    raw = (name or "").strip()
    if not raw:
        return ""
    names = _payload().get(ns) or {}
    return names.get(raw) or names.get(raw.lower()) or raw


def display_tags(raw_tags: list[tuple[str, str]], *, limit: int = 24) -> list[str]:
    """主站/频道用的中文标签，跳过 language 以免每本都堆「汉语」。"""
    skip_ns = {"language", "reclass", "temp"}
    out: list[str] = []
    seen: set[str] = set()
    for namespace, name in raw_tags:
        if namespace in skip_ns:
            continue
        label = translate_tag(namespace, name)
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= limit:
            break
    return out
