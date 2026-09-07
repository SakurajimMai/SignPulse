from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.services.ai.refine import refine_game_caption
from backend.services.manga.config import MangaSettings, load_manga_settings
from backend.services.manga.filters import grouped_id
from backend.services.manga.hmw.telegram_link import (
    TelegramLinkError,
    parse_telegram_post_url,
    safe_folder_name,
)
from backend.services.manga.hmw.telegram_zip import (
    TelegramZipError,
    _borrow_client,
    _document_name,
    _parse_chat,
    _release_client,
)

from .config import GamesSettings
from .parser import parse_game_caption
from .paths import (
    ARCHIVE_SUFFIXES,
    IMAGE_SUFFIXES,
    GamesDirs,
    games_dirs,
    relative_to_games,
)

logger = logging.getLogger("backend.games.telegram")

IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/avif",
    "image/bmp",
}

DOWNLOAD_MARKERS = (
    "下载",
    "下載",
    "通常版本",
    "ntr版本",
    "pc版本",
    "安卓版本",
)
VARIANT_MARKERS = (
    "通常版本",
    "ntr版本",
    "pc版本",
    "安卓版本",
    "apk",
)
_VERSION_IN_LABEL = re.compile(r"v?\s*(\d+\.\d+(?:\.\d+)*)", re.I)
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
PREVIEW_VIDEO_MAX_BYTES = 100 * 1024 * 1024
DOWNLOAD_SPACE_RESERVE_MAX_BYTES = 512 * 1024 * 1024
_SPLIT_ARCHIVE_PART = re.compile(
    r"(?:\.z\d{2,3}|\.r\d{2,3}|\.part\d+\.rar|\.(?:zip|7z)\.\d{3}|\.\d{3})$",
    re.I,
)


class GamesTelegramError(RuntimeError):
    pass


def _manga_settings() -> MangaSettings:
    return load_manga_settings()


def resolve_account(settings: GamesSettings, account: str = "") -> str:
    name = (account or settings.telegram_account_name or "").strip()
    if not name:
        name = str(_manga_settings().telegram_account_name or "").strip()
    if not name:
        raise GamesTelegramError("请选择已登录的 Telegram 账号")
    return name


def _is_image(message: Any) -> bool:
    if getattr(message, "photo", None) is not None:
        return True
    doc = getattr(message, "document", None)
    if doc is None:
        return False
    mime = str(getattr(doc, "mime_type", "") or "").casefold()
    name = _document_name(message).casefold()
    if mime in IMAGE_MIMES:
        return True
    return any(name.endswith(suffix) for suffix in IMAGE_SUFFIXES)


def _file_media(message: Any) -> Any:
    return getattr(message, "document", None) or getattr(message, "video", None)


def _media_name(message: Any) -> str:
    name = _document_name(message)
    if name:
        return name
    media = _file_media(message)
    return str(getattr(media, "file_name", None) or "").strip()


def _has_video_duration(obj: Any) -> bool:
    try:
        return int(getattr(obj, "duration", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_preview_video(message: Any) -> bool:
    """频道预览 mp4/动图，不是游戏压缩包，也不当封面图。"""
    if getattr(message, "video", None) is not None:
        return True
    if getattr(message, "animation", None) is not None:
        return True
    if getattr(message, "video_note", None) is not None:
        return True
    doc = getattr(message, "document", None)
    if doc is None:
        return False
    name = _media_name(message).casefold()
    if _SPLIT_ARCHIVE_PART.search(name) or name.endswith((".7z.001", ".zip.001")):
        return False
    mime = str(getattr(doc, "mime_type", "") or "").casefold()
    looks_video = name.endswith(tuple(VIDEO_SUFFIXES)) or mime.startswith("video/")
    if not looks_video:
        return False
    if _has_video_duration(doc):
        return True
    for attr in getattr(doc, "attributes", None) or []:
        if _has_video_duration(attr):
            return True
    size = int(getattr(doc, "file_size", 0) or 0)
    return name.endswith(tuple(VIDEO_SUFFIXES)) and size < PREVIEW_VIDEO_MAX_BYTES


def _is_archive(message: Any) -> bool:
    if _is_preview_video(message):
        return False
    media = _file_media(message)
    if media is None:
        return False
    name = _media_name(message).casefold()
    if any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES):
        return True
    if name.endswith((".7z.001", ".zip.001")):
        return True
    mime = str(getattr(media, "mime_type", "") or "").casefold()
    if "zip" in mime or "7z" in mime or "rar" in mime:
        return True
    # 频道里常见把压缩包伪装成视频/二进制
    size = int(getattr(media, "file_size", 0) or 0)
    if mime.startswith("video/") and size >= PREVIEW_VIDEO_MAX_BYTES:
        return True
    if (
        mime in {"application/octet-stream", "application/x-msdownload"}
        and size >= 1024 * 1024
    ):
        return True
    return False


def _is_archive_member(message: Any) -> bool:
    if _is_archive(message):
        return True
    return bool(_SPLIT_ARCHIVE_PART.search(_media_name(message).casefold()))


def _is_primary_archive(message: Any) -> bool:
    name = _media_name(message).casefold()
    if not name:
        return _is_archive(message)
    if re.search(r"\.(?:z|r)\d{2,3}$", name):
        return False
    part = re.search(r"\.part(\d+)\.rar$", name)
    if part:
        return int(part.group(1)) == 1
    if re.search(r"\.(?:zip|7z)\.(\d{3})$", name):
        return name.endswith(".001")
    if re.search(r"\.\d{3}$", name):
        return name.endswith(".001")
    return _is_archive(message)


def _visual_download(message: Any) -> tuple[Any, str] | None:
    if _is_preview_video(message):
        return None
    if _is_image(message):
        suffix = Path(_media_name(message)).suffix.casefold()
        return message, suffix if suffix in IMAGE_SUFFIXES else ".jpg"
    return None


def _message_text(message: Any) -> str:
    return str(
        getattr(message, "caption", None) or getattr(message, "text", None) or ""
    ).strip()


def _utf16_slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    start = max(int(offset), 0) * 2
    end = max(int(offset) + int(length), 0) * 2
    return raw[start:end].decode("utf-16-le", errors="ignore")


def message_links(message: Any) -> list[dict[str, str]]:
    text = _message_text(message)
    entities = list(
        getattr(message, "caption_entities", None)
        or getattr(message, "entities", None)
        or []
    )
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for entity in entities:
        try:
            offset = int(getattr(entity, "offset", 0) or 0)
            length = int(getattr(entity, "length", 0) or 0)
        except (TypeError, ValueError):
            continue
        label = _utf16_slice(text, offset, length).strip()
        url = str(getattr(entity, "url", None) or "").strip()
        entity_type = str(getattr(entity, "type", "") or "").casefold()
        if not url and (entity_type.endswith(".url") or entity_type == "url"):
            url = label
        url = url.rstrip(".,，。;；")
        if not url or url in seen:
            continue
        before = _utf16_slice(text, max(offset - 12, 0), min(offset, 12))
        after = _utf16_slice(text, offset + length, 32)
        context = _utf16_slice(text, max(offset - 12, 0), length + 24)
        seen.add(url)
        links.append(
            {
                "url": url,
                "label": label,
                "before": before,
                "after": after,
                "context": context,
            }
        )
    return links


def _link_marker_blob(item: dict[str, str]) -> str:
    return f"{item.get('label') or ''}\n{item.get('before') or ''}".casefold()


def _link_version_blob(item: dict[str, str]) -> str:
    # 只看链接同一行后的标注，避免上一行标题/相邻旧版污染。
    after = str(item.get("after") or "").split("\n", 1)[0]
    return f"{item.get('label') or ''} {after}"


def _is_variant_download(item: dict[str, str]) -> bool:
    blob = _link_version_blob(item).casefold()
    return any(marker in blob for marker in VARIANT_MARKERS)


def _link_version(item: dict[str, str]) -> tuple[int, ...] | None:
    found = _VERSION_IN_LABEL.findall(_link_version_blob(item))
    if not found:
        return None

    def _key(raw: str) -> tuple[int, ...]:
        parts = tuple(int(part) for part in raw.split(".") if part.isdigit())
        return parts or (0,)

    return max((_key(raw) for raw in found), default=None)


def _select_download_urls(preferred: list[dict[str, str]]) -> list[str]:
    """历史版本只保留最新一条；通常/NTR 等变体链接全部保留。"""
    if len(preferred) <= 1:
        return [item["url"] for item in preferred]
    variants = [item for item in preferred if _is_variant_download(item)]
    pool = variants or preferred
    versioned = [(item, _link_version(item)) for item in pool]
    present = [version for _item, version in versioned if version]
    if present:
        best = max(present)
        return [item["url"] for item, version in versioned if version == best]
    if variants:
        return [item["url"] for item in variants]
    return [preferred[0]["url"]]


def telegram_file_links(
    messages: list[Any], *, source_channel: str | int = ""
) -> list[str]:
    preferred: list[dict[str, str]] = []
    external: list[str] = []
    seen: set[str] = set()
    source = str(source_channel).strip().lstrip("@").casefold()
    for message in messages:
        for item in message_links(message):
            url = item["url"]
            try:
                ref = parse_telegram_post_url(url)
            except TelegramLinkError:
                continue
            if ref.comment_id is not None or url in seen:
                continue
            seen.add(url)
            if any(marker in _link_marker_blob(item) for marker in DOWNLOAD_MARKERS):
                preferred.append(item)
                continue
            target = str(ref.channel).strip().lstrip("@").casefold()
            if target and target != source:
                external.append(url)
    return _select_download_urls(preferred) or external


def _format_bytes(value: int) -> str:
    amount = float(max(int(value), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f}{unit}"
        amount /= 1024
    return f"{amount:.1f}TB"


def _ensure_download_space(root: Path, archive_bytes: int) -> None:
    if archive_bytes <= 0:
        return
    free = int(shutil.disk_usage(root).free)
    # 下载后会删源压缩包、打包后会删解压目录，峰值按「源文件 + 再留一份打包余量」估。
    # 安全余量随归档增长，避免几 KB 的文件也被固定 512 MiB 门槛拒绝。
    reserve = min(archive_bytes, DOWNLOAD_SPACE_RESERVE_MAX_BYTES)
    required = archive_bytes * 2 + reserve
    if free < required:
        raise GamesTelegramError(
            "磁盘空间不足：源文件约 "
            f"{_format_bytes(archive_bytes)}，自动解压重打包至少需要 "
            f"{_format_bytes(required)}，当前可用 {_format_bytes(free)}"
        )


def _as_list(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, list):
        return [item for item in result if item is not None]
    return [result]


async def _collect_messages(client: Any, chat: str | int, post_id: int) -> list[Any]:
    raw = await client.get_messages(chat, post_id)
    messages = _as_list(raw)
    if not messages:
        raise GamesTelegramError("找不到这条 Telegram 消息")
    seed = messages[0]
    grouped = grouped_id(seed)
    if grouped:
        try:
            group = await client.get_media_group(chat, post_id)
            extra = _as_list(group)
            if extra:
                by_id = {getattr(item, "id", None): item for item in extra}
                for item in messages:
                    by_id.setdefault(getattr(item, "id", None), item)
                messages = [
                    by_id[key] for key in sorted(k for k in by_id if k is not None)
                ]
        except Exception:
            logger.exception("读取媒体组失败，改用单条消息")
    return messages


def normalize_channel_ref(raw: str | int) -> str | int:
    text = str(raw or "").strip()
    if not text:
        raise GamesTelegramError("监听频道不能为空")
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        if (parsed.hostname or "").casefold() not in {
            "t.me",
            "www.t.me",
            "telegram.me",
            "telegram.dog",
        }:
            raise GamesTelegramError("监听频道必须是 t.me 地址或频道用户名")
        parts = [item for item in (parsed.path or "").split("/") if item]
        if not parts:
            raise GamesTelegramError("监听频道地址缺少频道名")
        if parts[0].casefold() == "c":
            if len(parts) < 2 or not parts[1].isdigit():
                raise GamesTelegramError("无法解析私有频道地址")
            return int(f"-100{parts[1]}")
        text = parts[0]
    text = text.lstrip("@")
    if text.startswith("-") and text[1:].isdigit():
        return int(text)
    if text.isdigit():
        return int(text)
    if not all(char.isalnum() or char == "_" for char in text):
        raise GamesTelegramError(f"无法解析监听频道：{raw}")
    return text


def _message_timestamp(message: Any) -> float | None:
    value = getattr(message, "date", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _private_channel_path(chat_id: int) -> str:
    raw = str(abs(int(chat_id)))
    return raw[3:] if raw.startswith("100") else raw


def _post_url(channel: str | int, username: str, chat_id: str, post_id: int) -> str:
    public_name = username.strip().lstrip("@")
    if public_name:
        return f"https://t.me/{public_name}/{post_id}"
    if isinstance(channel, str) and not channel.lstrip("-").isdigit():
        return f"https://t.me/{channel.lstrip('@')}/{post_id}"
    try:
        numeric_id = int(chat_id or channel)
    except (TypeError, ValueError):
        raise GamesTelegramError("频道没有公开用户名，且无法生成私有帖子链接")
    return f"https://t.me/c/{_private_channel_path(numeric_id)}/{post_id}"


def group_channel_history(
    messages: list[Any],
    *,
    channel: str | int,
    chat_id: str,
    username: str = "",
    settle_before: float | None = None,
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    latest_message_id = 0
    latest_settled_id = 0
    for message in messages:
        try:
            message_id = int(message.id)
        except (TypeError, ValueError):
            continue
        latest_message_id = max(latest_message_id, message_id)
        timestamp = _message_timestamp(message)
        settled = (
            settle_before is None or timestamp is None or timestamp <= settle_before
        )
        if settled:
            latest_settled_id = max(latest_settled_id, message_id)
        album_id = grouped_id(message)
        group_key = f"g:{album_id}" if album_id is not None else f"m:{message_id}"
        item = groups.setdefault(
            group_key,
            {
                "post_id": message_id,
                "max_message_id": message_id,
                "timestamp": timestamp,
                "settled": settled,
                "has_archive": False,
                "has_image": False,
                "caption": "",
                "messages": [],
            },
        )
        item["messages"].append(message)
        item["post_id"] = min(int(item["post_id"]), message_id)
        item["max_message_id"] = max(int(item["max_message_id"]), message_id)
        item["settled"] = bool(item["settled"] and settled)
        if timestamp is not None:
            previous = item.get("timestamp")
            item["timestamp"] = max(float(previous or timestamp), timestamp)
        item["has_archive"] = bool(item["has_archive"] or _is_archive(message))
        item["has_image"] = bool(item["has_image"] or _is_image(message))
        text = _message_text(message)
        if text and len(text) > len(str(item.get("caption") or "")):
            item["caption"] = text

    posts: list[dict[str, Any]] = []
    canonical_chat = chat_id or str(channel)
    for group_key, item in groups.items():
        file_urls = telegram_file_links(item.pop("messages"), source_channel=channel)
        item["file_urls"] = file_urls
        if not item["settled"] or (not item["has_archive"] and not file_urls):
            continue
        post_id = int(item["post_id"])
        posts.append(
            {
                **item,
                "channel": str(channel),
                "chat_id": canonical_chat,
                "source_key": f"{canonical_chat}:{group_key}",
                "url": _post_url(channel, username, canonical_chat, post_id),
            }
        )
    posts.sort(key=lambda item: (int(item["max_message_id"]), int(item["post_id"])))
    return {
        "channel": str(channel),
        "chat_id": canonical_chat,
        "username": username,
        "latest_message_id": latest_message_id,
        "latest_settled_id": latest_settled_id,
        "posts": posts,
    }


async def discover_telegram_posts(
    settings: GamesSettings,
    channel: str | int,
    *,
    account: str = "",
    limit: int = 100,
    settle_seconds: int = 15,
    after_message_id: int = 0,
) -> dict[str, Any]:
    channel_ref = normalize_channel_ref(channel)
    account_name = resolve_account(settings, account)
    manga = _manga_settings()
    client, lock, entered = await _borrow_client(manga, account_name)
    messages: list[Any] = []
    try:
        entity = await client.get_chat(channel_ref)
        async for message in client.get_chat_history(
            channel_ref, limit=max(10, min(int(limit or 100), 2000))
        ):
            messages.append(message)
            try:
                message_id = int(getattr(message, "id", 0) or 0)
            except (TypeError, ValueError):
                message_id = 0
            if after_message_id > 0 and message_id <= after_message_id:
                break
        chat_id = str(getattr(entity, "id", None) or channel_ref)
        username = str(getattr(entity, "username", None) or "")
    except Exception as exc:
        raise GamesTelegramError(f"读取监听频道 {channel} 失败：{exc}") from exc
    finally:
        await _release_client(client, lock, entered)
    settle_before = datetime.now(timezone.utc).timestamp() - max(int(settle_seconds), 0)
    result = group_channel_history(
        messages,
        channel=channel_ref,
        chat_id=chat_id,
        username=username,
        settle_before=settle_before,
    )
    result["account"] = account_name
    return result


async def pull_telegram_post(
    settings: GamesSettings,
    url: str,
    *,
    account: str = "",
    job_id: str,
    progress=None,
    dirs: GamesDirs | None = None,
) -> dict[str, Any]:
    try:
        ref = parse_telegram_post_url(url)
    except TelegramLinkError as exc:
        raise GamesTelegramError(str(exc)) from exc
    account_name = resolve_account(settings, account)
    manga = _manga_settings()
    dirs = dirs or games_dirs(settings)

    def _rel(path: Path) -> str:
        try:
            return str(path.expanduser().resolve().relative_to(dirs.root.resolve()))
        except ValueError:
            return relative_to_games(settings, path)

    dest = dirs.inbox / job_id
    dest.mkdir(parents=True, exist_ok=True)
    image_dir = dest / "images"
    archive_dir = dest / "archives"
    image_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)

    client, lock, entered = await _borrow_client(manga, account_name)
    images: list[dict[str, Any]] = []
    archives: list[dict[str, Any]] = []
    archive_members: list[dict[str, Any]] = []
    caption = ""
    try:
        chat = _parse_chat(str(ref.channel))
        messages = await _collect_messages(client, chat, ref.post_id)
        for message in messages:
            text = _message_text(message)
            if text and not caption:
                caption = text
            elif text and len(text) > len(caption):
                caption = text

        file_urls = telegram_file_links(messages, source_channel=ref.channel)
        archive_batches: list[tuple[str, list[Any], str]] = []
        direct_archives = [item for item in messages if _is_archive_member(item)]
        if direct_archives:
            archive_batches.append(("source", direct_archives, url))

        batch_keys: set[str] = set()
        link_errors: list[str] = []
        for file_url in file_urls:
            try:
                file_ref = parse_telegram_post_url(file_url)
                file_chat = _parse_chat(str(file_ref.channel))
                linked = await _collect_messages(client, file_chat, file_ref.post_id)
            except Exception as exc:
                link_errors.append(f"{file_url}: {exc}")
                logger.warning("读取游戏文件链接失败 url=%s error=%s", file_url, exc)
                continue
            linked_archives = [item for item in linked if _is_archive_member(item)]
            if not linked_archives:
                link_errors.append(f"{file_url}: 没有压缩文件")
                continue
            album = grouped_id(linked[0]) if linked else None
            batch_key = (
                f"{str(file_ref.channel).casefold()}:g:{album}"
                if album is not None
                else f"{str(file_ref.channel).casefold()}:m:{file_ref.post_id}"
            )
            if batch_key in batch_keys:
                continue
            batch_keys.add(batch_key)
            archive_batches.append((batch_key, linked_archives, file_url))

        visual_items = [
            item for item in (_visual_download(message) for message in messages) if item
        ]
        member_count = sum(len(batch[1]) for batch in archive_batches)
        archive_bytes = sum(
            int(getattr(_file_media(message), "file_size", 0) or 0)
            for _key, batch, _source in archive_batches
            for message in batch
        )
        _ensure_download_space(dirs.root, archive_bytes)
        total = max(len(visual_items) + member_count, 1)
        current = 0
        for image_index, (download_ref, suffix) in enumerate(visual_items, start=1):
            target = image_dir / f"{image_index:02d}{suffix}"
            if progress:
                await progress("pulling", current, total, f"下载图片 {image_index}")
            saved = await client.download_media(download_ref, file_name=str(target))
            current += 1
            path = Path(str(saved or target))
            if path.is_file():
                images.append(
                    {
                        "path": _rel(path),
                        "name": path.name,
                        "size": path.stat().st_size,
                    }
                )

        for batch_index, (_key, batch, source_url) in enumerate(
            archive_batches, start=1
        ):
            batch_dir = archive_dir / f"{batch_index:02d}"
            batch_dir.mkdir(parents=True, exist_ok=True)
            for message in batch:
                name = _media_name(message) or f"file-{getattr(message, 'id', 0)}"
                safe = safe_folder_name(name, fallback=name)[:160]
                target = batch_dir / safe
                current += 1
                if progress:
                    await progress("pulling", current, total, f"下载 {safe}")
                saved = await client.download_media(message, file_name=str(target))
                path = Path(str(saved or target))
                if path.is_file():
                    record = {
                        "path": _rel(path),
                        "name": path.name,
                        "size": path.stat().st_size,
                        "message_id": getattr(message, "id", None),
                        "source_url": source_url,
                    }
                    archive_members.append(record)
                    if _is_primary_archive(message):
                        archives.append(record)
        if not archives and link_errors:
            raise GamesTelegramError("；".join(link_errors[:3]))
    except GamesTelegramError:
        raise
    except TelegramZipError as exc:
        raise GamesTelegramError(str(exc)) from exc
    except Exception as exc:
        raise GamesTelegramError(f"拉取 Telegram 帖失败：{exc}") from exc
    finally:
        await _release_client(client, lock, entered)

    parsed = parse_game_caption(caption)
    refined = await refine_game_caption(settings, parsed, caption)
    parsed = refined.parsed
    entity_urls = [
        item["url"] for message in messages for item in message_links(message)
    ]
    urls = list(dict.fromkeys([*parsed.urls, *entity_urls]))
    return {
        "source_url": url,
        "source_key": f"{ref.channel}:m:{ref.post_id}",
        "account": account_name,
        "caption": caption,
        "title": parsed.title,
        "summary": parsed.summary,
        "tags": parsed.tags,
        "studio": parsed.studio,
        "password": parsed.password,
        "urls": urls,
        "telegram_file_urls": file_urls,
        "category_ids": parsed.category_ids,
        "ai_used": refined.used,
        "ai_skip": refined.skip,
        "ai_skip_reason": refined.reason,
        "images": images,
        "archives": archives,
        "archive_members": archive_members,
        "work_dir": _rel(dest),
    }
