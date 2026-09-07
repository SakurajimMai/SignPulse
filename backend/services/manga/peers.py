"""Normalize Telegram chat ids so Telethon-era source_key values still match."""

from __future__ import annotations


def telegram_raw_peer_id(chat_id: int | str | None) -> int | None:
    """Strip the Bot API ``-100`` prefix used by Pyrogram / event.chat_id.

    Historical manga ``source_key`` values were built from Telethon ``entity.id``
    (the raw channel / megagroup id without ``-100``). Incoming Pyrogram chats
    use the marked form. Comparing raw ids keeps later albums appending to the
    same chapter instead of opening a duplicate.
    """
    if chat_id is None or chat_id == "":
        return None
    try:
        value = int(chat_id)
    except (TypeError, ValueError):
        return None
    if value < 0:
        text = str(abs(value))
        if text.startswith("100") and len(text) > 10:
            return int(text[3:])
        return abs(value)
    return value


def same_telegram_peer(left: int | str | None, right: int | str | None) -> bool:
    raw_left = telegram_raw_peer_id(left)
    raw_right = telegram_raw_peer_id(right)
    return raw_left is not None and raw_left == raw_right


def marked_channel_id(chat_id: int | str | None) -> int | None:
    raw = telegram_raw_peer_id(chat_id)
    if raw is None:
        return None
    return -int(f"100{raw}")
