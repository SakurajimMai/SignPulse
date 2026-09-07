"""采集频道与讨论群的一一对应。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


INGEST_AUTO = "auto"
INGEST_DISCUSSION = "discussion"
INGEST_TELEGRAPH = "telegraph"


def normalize_ingest_mode(value: Any, *, discussion: str = "") -> str:
    raw = str(value or "").strip().lower()
    if raw in {"telegraph", "tgph", "telegra", "telegra.ph"}:
        return INGEST_TELEGRAPH
    if raw in {"discussion", "album", "group", "comments"}:
        return INGEST_DISCUSSION
    return INGEST_TELEGRAPH if not str(discussion or "").strip() else INGEST_DISCUSSION


@dataclass(frozen=True)
class SourceBinding:
    """一个采集频道对应一个讨论群，或一个只走 Telegraph 的频道。"""

    channel: str = ""
    discussion: str = ""
    ingest_mode: str = INGEST_AUTO
    forward_videos: bool = True
    ingest_enabled: bool = True

    def resolved_ingest_mode(self) -> str:
        return normalize_ingest_mode(self.ingest_mode, discussion=self.discussion)

    def is_telegraph(self) -> bool:
        return self.resolved_ingest_mode() == INGEST_TELEGRAPH

    def wants_history_backfill(self) -> bool:
        """Telegraph 频道只听新帖，不回扫历史。讨论群相册仍可补历史。"""
        return not self.is_telegraph()

    def as_dict(self) -> dict[str, Any]:
        stored = str(self.ingest_mode or INGEST_AUTO).strip().lower() or INGEST_AUTO
        if stored not in {INGEST_AUTO, INGEST_DISCUSSION, INGEST_TELEGRAPH}:
            stored = INGEST_AUTO
        return {
            "channel": self.channel,
            "discussion": self.discussion,
            "ingest_mode": stored,
            "forward_videos": self.forward_videos,
            "ingest_enabled": self.ingest_enabled,
        }


def _as_ref(value: Any) -> str:
    return str(value or "").strip()


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_source_bindings(
    bindings: Any,
    *,
    legacy_chats: str = "",
) -> list[SourceBinding]:
    """解析绑定列表；空列表时回退到旧的逗号分隔源。"""
    raw = bindings
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None

    items: list[SourceBinding] = []
    if isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, dict):
                channel = _as_ref(entry.get("channel") or entry.get("channel_id"))
                discussion = _as_ref(
                    entry.get("discussion")
                    or entry.get("discussion_group")
                    or entry.get("group")
                )
                ingest_mode = _as_ref(
                    entry.get("ingest_mode") or entry.get("mode") or INGEST_AUTO
                ) or INGEST_AUTO
                if "ingest_enabled" in entry:
                    ingest_enabled = _as_bool(entry.get("ingest_enabled"), True)
                elif "skip_ingest" in entry:
                    ingest_enabled = not _as_bool(entry.get("skip_ingest"), False)
                else:
                    ingest_enabled = True
                if channel or discussion:
                    items.append(
                        SourceBinding(
                            channel=channel,
                            discussion=discussion,
                            ingest_mode=ingest_mode,
                            forward_videos=_as_bool(entry.get("forward_videos"), True),
                            ingest_enabled=ingest_enabled,
                        )
                    )
            elif isinstance(entry, str) and entry.strip():
                items.append(SourceBinding(channel=entry.strip(), discussion=""))

    if items:
        return items

    for ref in [item.strip() for item in str(legacy_chats or "").split(",") if item.strip()]:
        items.append(SourceBinding(channel=ref, discussion=""))
    return items


def serialize_source_bindings(bindings: list[SourceBinding] | tuple[SourceBinding, ...]) -> list[dict[str, Any]]:
    return [item.as_dict() for item in bindings]


def binding_chat_refs(bindings: list[SourceBinding]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for item in bindings:
        for ref in (item.channel, item.discussion):
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs
