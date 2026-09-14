"""把重打包后的游戏文件发到自建公开频道（可选，不影响网盘）。"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

from backend.services.manga.hmw.telegram_link import safe_folder_name
from backend.services.manga.hmw.telegram_zip import (
    _borrow_client,
    _parse_chat,
    _release_client,
)

from .archive import pack_7z_parts
from .config import (
    TELEGRAM_SPLIT_VOLUME_MB_DEFAULT,
    GamesSettings,
    normalize_telegram_split_volume_mb,
)
from .telegram import GamesTelegramError, resolve_account

logger = logging.getLogger("backend.games.telegram_publish")

TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_ALBUM_LIMIT = 10


def normalize_channel_ref(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if "t.me/" in lowered:
        path = text.split("t.me/", 1)[-1].lstrip("/")
        path = path.split("?", 1)[0]
        parts = [item for item in path.split("/") if item]
        if (
            len(parts) >= 2
            and parts[0].casefold() == "c"
            and parts[1].isdigit()
        ):
            raise ValueError("请填写公开频道用户名（如 MyGames），不支持 t.me/c/ 私有链接")
        text = parts[0] if parts else ""
    return text.lstrip("@").strip()


def telegram_catalog_channel(settings: GamesSettings) -> str:
    return normalize_channel_ref(settings.telegram_catalog_channel)


def telegram_files_channel(settings: GamesSettings) -> str:
    return normalize_channel_ref(settings.telegram_files_channel) or telegram_catalog_channel(
        settings
    )


def telegram_publish_configured(settings: GamesSettings) -> bool:
    return bool(telegram_catalog_channel(settings) and telegram_files_channel(settings))


def telegram_publish_wanted(
    settings: GamesSettings,
    payload: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
) -> bool:
    data = payload or {}
    if data.get("telegram_publish") is not None:
        return bool(data.get("telegram_publish"))
    record = job or {}
    if record.get("telegram_publish") is not None:
        return bool(record.get("telegram_publish"))
    return bool(settings.telegram_publish_enabled)


def telegram_volume_mb(settings: GamesSettings | None = None, volume_mb: int | None = None) -> int:
    if volume_mb is not None:
        return normalize_telegram_split_volume_mb(volume_mb)
    if settings is not None:
        return normalize_telegram_split_volume_mb(settings.telegram_split_volume_mb)
    return TELEGRAM_SPLIT_VOLUME_MB_DEFAULT


def parts_fit_telegram(
    paths: list[Path], *, volume_mb: int | None = None, settings: GamesSettings | None = None
) -> bool:
    limit = telegram_volume_mb(settings, volume_mb) * 1024 * 1024
    return bool(paths) and all(
        path.is_file() and path.stat().st_size <= limit for path in paths
    )


def prepare_telegram_parts(
    packed_parts: list[Path],
    *,
    extract_root: Path | None,
    dest: Path,
    pack_password: str,
    volume_mb: int | None = None,
    settings: GamesSettings | None = None,
) -> list[Path]:
    """网盘分卷保持原样；仅当超过 TG 上限且源目录还在时另打一份频道分卷。"""
    limit_mb = telegram_volume_mb(settings, volume_mb)
    existing = [path for path in packed_parts if path.is_file()]
    if parts_fit_telegram(existing, volume_mb=limit_mb):
        return existing
    if extract_root is None or not extract_root.exists():
        raise GamesTelegramError(
            f"打包分卷超过 Telegram 单文件 {limit_mb}MiB 上限，且解压目录已清理，无法为频道重打分卷"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    return pack_7z_parts(
        extract_root,
        dest,
        pack_password,
        volume_bytes=limit_mb * 1024 * 1024,
    )


def public_message_url(chat: Any, message_id: int) -> str:
    username = str(getattr(chat, "username", "") or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{int(message_id)}"
    raw_id = str(getattr(chat, "id", "") or "")
    if raw_id.startswith("-100"):
        raw_id = raw_id[4:]
    return f"https://t.me/c/{raw_id}/{int(message_id)}"


def build_catalog_caption(
    *,
    title: str,
    summary: str,
    file_urls: list[str],
    pack_password: str = "",
) -> str:
    heading = html.escape((title or "").strip() or "未命名游戏")
    lines = [f"<b>{heading}</b>"]
    body = html.escape((summary or "").strip())
    if body:
        lines.extend(["", body])
    if file_urls:
        lines.extend(["", "下载"])
        lines.extend(html.escape(url) for url in file_urls if url)
    password = html.escape((pack_password or "").strip())
    if password:
        lines.extend(["", f"解压密码：{password}"])
    caption = "\n".join(lines).strip()
    return caption[:4096]


def _html_parse_mode() -> Any:
    try:
        from pyrogram.enums import ParseMode

        return ParseMode.HTML
    except Exception:
        return "html"


def _input_media_photo(path: Path, **kwargs: Any) -> Any:
    from pyrogram.types import InputMediaPhoto

    return InputMediaPhoto(str(path), **kwargs)


async def _resolve_chat(client: Any, raw: str) -> Any:
    chat_ref = _parse_chat(raw)
    return await client.get_chat(chat_ref)


async def _send_documents(
    client: Any,
    chat: Any,
    paths: list[Path],
    progress=None,
) -> list[str]:
    chat_id = getattr(chat, "id", None) or chat
    urls: list[str] = []
    total = max(len(paths), 1)
    for index, path in enumerate(paths, start=1):
        if progress:
            await progress(
                "telegram",
                index - 1,
                total,
                f"正在上传频道文件 {index}/{total}",
            )
        message = await client.send_document(
            chat_id,
            str(path),
            file_name=path.name,
        )
        urls.append(public_message_url(getattr(message, "chat", None) or chat, message.id))
    return urls


async def _send_catalog(
    client: Any,
    chat: Any,
    *,
    image_paths: list[Path],
    caption: str,
) -> str:
    chat_id = getattr(chat, "id", None) or chat
    parse_mode = _html_parse_mode()
    photos = [path for path in image_paths if path.is_file()][:TELEGRAM_ALBUM_LIMIT]
    media_caption = caption if len(caption) <= TELEGRAM_CAPTION_LIMIT else ""
    leftover = caption if not media_caption else ""
    if photos:
        if len(photos) == 1:
            kwargs: dict[str, Any] = {}
            if media_caption:
                kwargs["caption"] = media_caption
                kwargs["parse_mode"] = parse_mode
            message = await client.send_photo(chat_id, str(photos[0]), **kwargs)
        else:
            media = []
            for index, path in enumerate(photos):
                item_kwargs: dict[str, Any] = {}
                if index == 0 and media_caption:
                    item_kwargs["caption"] = media_caption
                    item_kwargs["parse_mode"] = parse_mode
                media.append(_input_media_photo(path, **item_kwargs))
            messages = await client.send_media_group(chat_id, media)
            message = messages[0] if messages else None
        if leftover:
            message = await client.send_message(chat_id, leftover, parse_mode=parse_mode)
        if message is None:
            raise GamesTelegramError("介绍频道发帖失败")
        return public_message_url(getattr(message, "chat", None) or chat, message.id)
    message = await client.send_message(chat_id, caption, parse_mode=parse_mode)
    return public_message_url(getattr(message, "chat", None) or chat, message.id)


async def publish_game_to_channels(
    settings: GamesSettings,
    *,
    title: str,
    summary: str,
    pack_password: str,
    file_paths: list[Path],
    image_paths: list[Path],
    account: str = "",
    client: Any | None = None,
    file_urls: list[str] | None = None,
    on_file_urls=None,
    progress=None,
) -> dict[str, Any]:
    catalog_ref = telegram_catalog_channel(settings)
    files_ref = telegram_files_channel(settings)
    if not catalog_ref or not files_ref:
        raise GamesTelegramError("请填写公开的介绍频道和文件频道（用户名，如 MyGames）")
    existing_urls = [str(url).strip() for url in (file_urls or []) if str(url).strip()]
    if not file_paths and not existing_urls:
        raise GamesTelegramError("没有可发到频道的打包文件")
    account_name = resolve_account(settings, account)
    own_client = client is None
    borrowed = None
    lock = None
    entered = False
    try:
        if own_client:
            from backend.services.manga.config import load_manga_settings

            borrowed, lock, entered = await _borrow_client(
                load_manga_settings(), account_name
            )
            client = borrowed
        files_chat = await _resolve_chat(client, files_ref)
        catalog_chat = await _resolve_chat(client, catalog_ref)
        sent_urls = existing_urls
        if not sent_urls:
            sent_urls = await _send_documents(
                client, files_chat, file_paths, progress=progress
            )
        if on_file_urls is not None:
            on_file_urls(sent_urls)
        caption = build_catalog_caption(
            title=title,
            summary=summary,
            file_urls=sent_urls,
            pack_password=pack_password,
        )
        if progress:
            await progress("telegram", 1, 1, "正在发介绍频道帖")
        catalog_url = await _send_catalog(
            client,
            catalog_chat,
            image_paths=image_paths,
            caption=caption,
        )
        return {
            "catalog_url": catalog_url,
            "file_urls": sent_urls,
            "catalog_channel": catalog_ref,
            "files_channel": files_ref,
        }
    finally:
        if own_client and borrowed is not None:
            await _release_client(borrowed, lock, entered)


def telegram_dest_name(title: str) -> str:
    return f"{safe_folder_name(title, fallback='game')}.7z"
