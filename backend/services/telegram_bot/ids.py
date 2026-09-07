"""Telegram Bot destination helpers: operator user IDs and notification targets."""

from __future__ import annotations

from typing import Any, Mapping, Optional


def allowed_user_ids_from_settings(settings: Mapping[str, Any]) -> frozenset[int]:
    """Parse the typed setting while tolerating legacy comma-separated values."""
    raw = settings.get("telegram_bot_allowed_user_ids")
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        values: list[Any] = (
            raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
        )
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = list(raw)
    else:
        values = [raw]

    parsed: set[int] = set()
    for value in values:
        try:
            user_id = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if 0 < user_id < 2**63:
            parsed.add(user_id)
    return frozenset(parsed)


def _thread_id(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def notification_targets(
    settings: Mapping[str, Any],
) -> list[tuple[str, Optional[int]]]:
    """Private user chats first; optional legacy group chat_id is appended."""
    targets: list[tuple[str, Optional[int]]] = []
    seen: set[str] = set()
    for user_id in sorted(allowed_user_ids_from_settings(settings)):
        key = str(user_id)
        seen.add(key)
        targets.append((key, None))
    extra = str(settings.get("telegram_bot_chat_id") or "").strip()
    if extra and extra not in seen:
        targets.append(
            (extra, _thread_id(settings.get("telegram_bot_message_thread_id")))
        )
    return targets
