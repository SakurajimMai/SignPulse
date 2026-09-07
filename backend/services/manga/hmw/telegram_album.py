from __future__ import annotations

import logging
import mimetypes
import shutil
from pathlib import Path
from typing import Any

from backend.services.manga.config import MangaSettings
from backend.services.manga.filters import (
    grouped_id,
    is_image_message,
    message_text,
    parse_caption,
    reply_to_id,
)
from backend.services.manga.hmw.paths import hmw_dirs
from backend.services.manga.hmw.service import relative_to_hmw
from backend.services.manga.hmw.telegram_link import (
    TelegramLinkError,
    parse_telegram_post_url,
    safe_folder_name,
    split_series_title,
    title_from_text,
)
from backend.services.manga.hmw.telegram_zip import (
    TelegramZipError,
    _borrow_client,
    _parse_chat,
    _release_client,
)

logger = logging.getLogger("backend.manga.hmw.album")


def _message_id(message: Any) -> int | None:
    try:
        value = int(getattr(message, "id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value or None


def _sorted_unique(messages: list[Any]) -> list[Any]:
    by_id: dict[int, Any] = {}
    for message in messages:
        mid = _message_id(message)
        if mid is None:
            continue
        by_id[mid] = message
    return [by_id[key] for key in sorted(by_id)]


def cluster_comment_album(messages: list[Any], seed_id: int | None) -> list[Any]:
    """从讨论区消息里抽出 seed 这一话的图片。"""
    ordered = _sorted_unique(messages)
    if not ordered:
        return []
    by_id = {_message_id(item): item for item in ordered}
    seed = by_id.get(seed_id) if seed_id else None
    if seed is None:
        seed = next((item for item in ordered if is_image_message(item)), ordered[0])
        seed_id = _message_id(seed)

    root_id = reply_to_id(seed) or seed_id
    group_ids = {grouped_id(seed)} - {None}

    selected: dict[int, Any] = {}

    def take(message: Any) -> None:
        mid = _message_id(message)
        if mid is None or mid in selected:
            return
        selected[mid] = message
        gid = grouped_id(message)
        if gid is not None:
            group_ids.add(gid)

    for message in ordered:
        mid = _message_id(message)
        rid = reply_to_id(message)
        gid = grouped_id(message)
        if mid in {seed_id, root_id} or rid in {seed_id, root_id} or (gid is not None and gid in group_ids):
            take(message)

    changed = True
    while changed:
        changed = False
        for message in ordered:
            mid = _message_id(message)
            if mid in selected:
                continue
            gid = grouped_id(message)
            if gid is not None and gid in group_ids:
                take(message)
                changed = True

    pages = [item for item in selected.values() if is_image_message(item)]
    if len(pages) >= 2:
        return _sorted_unique(pages)

    # 连续图片：从 seed 向两侧扩到下一条标题卡为止
    index = next((i for i, item in enumerate(ordered) if _message_id(item) == seed_id), 0)
    start = index
    end = index
    while start > 0 and is_image_message(ordered[start - 1]) and not message_text(ordered[start - 1]):
        start -= 1
    while end + 1 < len(ordered) and is_image_message(ordered[end + 1]):
        nxt = ordered[end + 1]
        text = message_text(nxt)
        if text and not grouped_id(nxt) and not is_image_message(nxt):
            break
        if text and len(text) > 12 and not is_image_message(nxt):
            break
        if not is_image_message(nxt):
            break
        end += 1
    contiguous = [item for item in ordered[start : end + 1] if is_image_message(item)]
    return _sorted_unique(contiguous or pages or ([seed] if is_image_message(seed) else []))


async def _get_one(client, chat: str | int, message_id: int) -> Any | None:
    try:
        result = await client.get_messages(chat, message_id)
    except Exception:
        logger.debug("get_messages failed chat=%s id=%s", chat, message_id, exc_info=True)
        return None
    if isinstance(result, list):
        return result[0] if result else None
    return result


async def _iter_discussion_replies(client, channel: str | int, post_id: int) -> list[Any]:
    getter = getattr(client, "get_discussion_replies", None)
    if getter is None:
        return []
    collected: list[Any] = []
    try:
        async for message in getter(channel, post_id):
            collected.append(message)
            if len(collected) >= 2500:
                break
    except Exception:
        logger.debug("get_discussion_replies failed channel=%s post=%s", channel, post_id, exc_info=True)
        return []
    return collected


async def _linked_discussion_chat(client, channel: str | int) -> str | int | None:
    try:
        entity = await client.get_chat(channel)
    except Exception:
        logger.debug("get_chat failed %s", channel, exc_info=True)
        return None
    linked = getattr(entity, "linked_chat", None)
    if linked is None:
        getter = getattr(client, "get_chat", None)
        try:
            full = await getter(getattr(entity, "id", channel))
            linked = getattr(full, "linked_chat", None)
        except Exception:
            linked = None
    if linked is None:
        return None
    return getattr(linked, "id", None) or getattr(linked, "username", None)


async def _nearby_messages(client, chat: str | int, seed_id: int, span: int = 400) -> list[Any]:
    start = max(1, seed_id - 8)
    end = seed_id + span
    ids = list(range(start, end + 1))
    collected: list[Any] = []
    getter = getattr(client, "get_messages", None)
    if getter is None:
        return []
    for offset in range(0, len(ids), 200):
        chunk = ids[offset : offset + 200]
        try:
            result = await getter(chat, chunk)
        except Exception:
            logger.debug("nearby get_messages failed", exc_info=True)
            continue
        if result is None:
            continue
        if not isinstance(result, list):
            result = [result]
        for message in result:
            if message is not None and _message_id(message):
                collected.append(message)
    return collected


def _pick_title(*texts: str | None) -> str:
    for text in texts:
        title = title_from_text(text)
        if title:
            return title
    return ""


async def pull_discussion_album(
    settings: MangaSettings,
    *,
    account: str,
    url: str,
    title: str | None = None,
    manga_title: str | None = None,
) -> dict[str, Any]:
    try:
        ref = parse_telegram_post_url(url)
    except TelegramLinkError as exc:
        raise TelegramZipError(str(exc)) from exc

    client, lock, entered = await _borrow_client(settings, account)
    try:
        channel = ref.channel if isinstance(ref.channel, int) else _parse_chat(str(ref.channel))
        comments = await _iter_discussion_replies(client, channel, ref.post_id)
        discussion = await _linked_discussion_chat(client, channel)
        seed = None
        if ref.comment_id:
            if discussion is not None:
                seed = await _get_one(client, discussion, ref.comment_id)
            if seed is None:
                seed = next((item for item in comments if _message_id(item) == ref.comment_id), None)
        if seed is None:
            seed = await _get_one(client, channel, ref.post_id)
        if seed is None:
            raise TelegramZipError("找不到这条评论或帖子，确认采集号已加入讨论组")

        seed_chat = getattr(getattr(seed, "chat", None), "id", None) or discussion or channel
        seed_id = _message_id(seed)
        pool = list(comments)
        if seed is not None:
            pool.append(seed)
        if ref.comment_id and seed_chat is not None:
            pool.extend(await _nearby_messages(client, seed_chat, ref.comment_id))
        media_group = getattr(client, "get_media_group", None)
        if media_group is not None and seed_id and grouped_id(seed):
            try:
                extra = await media_group(seed_chat, seed_id)
                if extra:
                    pool.extend(extra if isinstance(extra, list) else [extra])
            except Exception:
                logger.debug("get_media_group failed", exc_info=True)

        pages = cluster_comment_album(pool, seed_id or ref.comment_id)
        if not pages:
            raise TelegramZipError("这条评论附近没有可下载的图片")

        channel_msg = await _get_one(client, channel, ref.post_id)
        root_id = reply_to_id(seed) or seed_id
        root_msg = None
        if discussion is not None and root_id:
            root_msg = await _get_one(client, discussion, root_id)
        chapter_title = (title or "").strip() or _pick_title(
            message_text(seed),
            message_text(root_msg) if root_msg is not None else None,
            message_text(channel_msg) if channel_msg is not None else None,
        )
        if not chapter_title:
            chapter_title = f"chapter-{ref.comment_id or ref.post_id}"
        series, chapter_name = split_series_title(chapter_title)
        if manga_title and manga_title.strip():
            series = manga_title.strip()
        parsed = parse_caption(
            message_text(root_msg)
            or message_text(seed)
            or (message_text(channel_msg) if channel_msg is not None else "")
        )

        inbox = hmw_dirs(settings).inbox
        manga_dir = inbox / safe_folder_name(series)
        chapter_dir = manga_dir / safe_folder_name(chapter_name)
        if chapter_dir.exists():
            shutil.rmtree(chapter_dir)
        chapter_dir.mkdir(parents=True, exist_ok=True)

        saved_pages = 0
        for index, message in enumerate(pages, start=1):
            ext = ".jpg"
            doc = getattr(message, "document", None)
            mime = getattr(doc, "mime_type", None) if doc is not None else None
            if mime:
                ext = mimetypes.guess_extension(str(mime)) or ext
            filename = getattr(doc, "file_name", None) if doc is not None else None
            if filename and Path(str(filename)).suffix:
                ext = Path(str(filename)).suffix
            dest = chapter_dir / f"{index:04d}{ext.lower()}"
            saved = await client.download_media(message, file_name=str(dest))
            path = Path(str(saved)) if saved else dest
            if path.exists() and path.stat().st_size > 0:
                if path != dest:
                    path.replace(dest)
                saved_pages += 1
        if saved_pages <= 0:
            shutil.rmtree(chapter_dir, ignore_errors=True)
            raise TelegramZipError("图片下载失败")

        return {
            "name": manga_dir.name,
            "path": relative_to_hmw(settings, manga_dir),
            "chapter_path": relative_to_hmw(settings, chapter_dir),
            "kind": "directory",
            "location": "inbox",
            "title": series,
            "chapter_title": chapter_name,
            "author": parsed.author or "",
            "pages": saved_pages,
            "comment_id": ref.comment_id,
            "post_id": ref.post_id,
        }
    except TelegramZipError:
        raise
    except Exception as exc:
        raise TelegramZipError(f"拉取讨论组相册失败：{exc}") from exc
    finally:
        await _release_client(client, lock, entered)
