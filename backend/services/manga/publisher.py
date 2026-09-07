from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Chapter, IngestEvent, Manga, Page, utcnow
from .filters import clean_display_title
from .peers import marked_channel_id, telegram_raw_peer_id
from .slugify import slugify

logger = logging.getLogger(__name__)

OUTBOUND_RETRY_DELAYS_SECONDS = (15 * 60, 60 * 60, 6 * 60 * 60, 24 * 60 * 60, 72 * 60 * 60)
OUTBOUND_MAX_ATTEMPTS = len(OUTBOUND_RETRY_DELAYS_SECONDS) + 1


def _parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = value.replace('，', ',').replace('、', ',').split(',')
    if not isinstance(parsed, list):
        return []
    return [str(tag).strip() for tag in parsed if str(tag).strip()][:30]


def _merge_tags(existing: str | None, incoming: list[str]) -> list[str]:
    merged: list[str] = []
    for tag in _parse_tags(existing) + incoming:
        tag = str(tag).strip()
        if tag and tag not in merged:
            merged.append(tag)
    return merged[:30]


def _parse_sent_videos(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def read_outbound_state(chapter: Chapter) -> tuple[bool, list[str]]:
    return bool(getattr(chapter, "tg_album_sent", False)), _parse_sent_videos(
        getattr(chapter, "tg_sent_videos", None)
    )


@dataclass
class PublishedChapter:
    manga_id: int
    chapter_id: int
    slug: str
    number: int
    page_count: int
    created: bool = False
    new_video_urls: list[str] | None = None
    album_sent: bool = False
    sent_video_urls: list[str] | None = None
    outbound_attempts: int = 0
    site_published_page_count: int = 0
    site_manga_id: int | None = None
    site_chapter_id: int | None = None


@dataclass(frozen=True)
class OutboundRetryState:
    attempts: int
    retry_at: datetime | None
    exhausted: bool


@dataclass
class StoredOutbound:
    """已入库、主站已有图，但讨论帖收齐后还没转发到运营频道的章节。"""

    source_key: str
    published: PublishedChapter
    title: str
    author: str | None
    tags: list[str]
    source_chat_id: str | None
    source_chat_title: str | None
    image_urls: list[str]
    video_urls: list[str]


async def _unique_slug(session: AsyncSession, base: str) -> str:
    candidate = base
    n = 2
    while True:
        exists = await session.execute(select(Manga.id).where(Manga.slug == candidate))
        if exists.scalar_one_or_none() is None:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def site_publish_ids(body: dict[str, object]) -> tuple[int | None, int | None]:
    """Read remote IDs from both current and wrapped site API responses."""
    candidates = [body]
    nested = body.get("data")
    if isinstance(nested, dict):
        candidates.append(nested)
    manga_id = None
    chapter_id = None
    for item in candidates:
        manga_id = manga_id or _positive_int(item.get("mangaId") or item.get("manga_id"))
        chapter_id = chapter_id or _positive_int(
            item.get("chapterId") or item.get("chapter_id")
        )
    return manga_id, chapter_id


async def get_or_create_manga(
    session: AsyncSession,
    *,
    title: str,
    source_chat_id: str | None,
    source_chat_title: str | None,
    cover_url: str | None = None,
    description: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
) -> Manga:
    # Prefer matching by source chat + exact title so multi-series chats still work
    stmt = select(Manga).where(Manga.title == title)
    preferred = stmt.where(Manga.source_chat_id == source_chat_id) if source_chat_id else stmt
    result = await session.execute(preferred.limit(1))
    manga = result.scalar_one_or_none()
    if manga is None and source_chat_id:
        # 频道视频和讨论区图片 source_chat_id 不同，按标题挂回同一部，避免拆成两套
        result = await session.execute(stmt.limit(1))
        manga = result.scalar_one_or_none()
    if manga:
        changed = False
        if cover_url and not manga.cover_url:
            manga.cover_url = cover_url
            changed = True
        if description and not manga.description:
            manga.description = description
            changed = True
        if author and not manga.author:
            manga.author = author.strip()
            changed = True
        if tags:
            merged_tags = _merge_tags(manga.tags, tags)
            encoded_tags = json.dumps(merged_tags, ensure_ascii=False)
            if manga.tags != encoded_tags:
                manga.tags = encoded_tags
                changed = True
        if source_chat_title and manga.source_chat_title != source_chat_title:
            manga.source_chat_title = source_chat_title
            changed = True
        if changed:
            manga.updated_at = utcnow()
        return manga

    slug = await _unique_slug(session, slugify(title))
    manga = Manga(
        slug=slug,
        title=title,
        description=description,
        author=author.strip() if author else None,
        tags=json.dumps(_merge_tags(None, tags or []), ensure_ascii=False) if tags else None,
        cover_url=cover_url,
        source_chat_id=source_chat_id,
        source_chat_title=source_chat_title,
        is_published=True,
    )
    session.add(manga)
    await session.flush()
    return manga


def alternate_source_keys(source_key: str) -> list[str]:
    """Same chapter may be keyed as -100xxx or the raw channel id."""
    keys = [source_key]
    parts = source_key.split(":", 2)
    if len(parts) < 2:
        return keys
    raw = telegram_raw_peer_id(parts[0])
    if raw is None:
        return keys
    rest = source_key[len(parts[0]) :]
    for prefix in (str(raw), str(marked_channel_id(raw) or "")):
        if not prefix:
            continue
        candidate = f"{prefix}{rest}"
        if candidate not in keys:
            keys.append(candidate)
    return keys


async def _find_chapter_by_source_key(session: AsyncSession, source_key: str) -> Chapter | None:
    result = await session.execute(
        select(Chapter).where(Chapter.source_key.in_(alternate_source_keys(source_key)))
    )
    return result.scalars().first()


async def update_published_titles(
    session: AsyncSession,
    source_key: str,
    *,
    title: str,
) -> Chapter | None:
    """补全书封漏掉的话数，不改 slug。"""
    cleaned = clean_display_title(title) or (title or "").strip()
    if not cleaned:
        return None
    chapter = await _find_chapter_by_source_key(session, source_key)
    if chapter is None:
        return None
    manga_result = await session.execute(select(Manga).where(Manga.id == chapter.manga_id))
    manga = manga_result.scalar_one_or_none()
    changed = False
    if chapter.title != cleaned:
        chapter.title = cleaned
        chapter.updated_at = utcnow()
        changed = True
    if manga is not None:
        chapter_count = int(manga.chapter_count or 1)
        if chapter_count <= 1 and manga.title != cleaned:
            manga.title = cleaned
            manga.updated_at = utcnow()
            changed = True
    if changed:
        await session.commit()
        logger.info("Updated titles source_key=%s title=%s", source_key, cleaned)
    return chapter


async def _refresh_manga_counts(session: AsyncSession, manga: Manga) -> None:
    chapter_count = (
        await session.execute(select(func.count()).select_from(Chapter).where(Chapter.manga_id == manga.id))
    ).scalar_one()
    page_count = (
        await session.execute(
            select(func.count())
            .select_from(Page)
            .join(Chapter, Page.chapter_id == Chapter.id)
            .where(
                Chapter.manga_id == manga.id,
                or_(Page.media_kind.is_(None), Page.media_kind != "video"),
            )
        )
    ).scalar_one()
    manga.chapter_count = int(chapter_count)
    manga.page_count = int(page_count)
    manga.updated_at = utcnow()


async def _append_chapter_pages(
    session: AsyncSession,
    *,
    chapter: Chapter,
    image_urls: list[str],
    message_ids: list[int],
    author: str | None,
    tags: list[str] | None,
    media_kinds: list[str] | None = None,
) -> PublishedChapter | None:
    """同一讨论帖后续分批发来的图片追加进已有章节，而不是当成重复丢掉。"""
    existing_pages = (
        await session.execute(select(Page).where(Page.chapter_id == chapter.id).order_by(Page.index))
    ).scalars().all()
    seen_ids = {page.tg_message_id for page in existing_pages if page.tg_message_id is not None}
    seen_urls = {page.image_url for page in existing_pages}
    next_index = (max((page.index for page in existing_pages), default=-1) + 1)
    added = 0
    added_videos: list[str] = []
    try:
        stored_ids = list(json.loads(chapter.source_message_ids or "[]") or [])
    except (TypeError, ValueError):
        stored_ids = []
    for offset, url in enumerate(image_urls):
        mid = message_ids[offset] if offset < len(message_ids) else None
        if mid is not None and mid in seen_ids:
            continue
        if url in seen_urls:
            continue
        kind = "image"
        if media_kinds and offset < len(media_kinds) and media_kinds[offset] == "video":
            kind = "video"
        session.add(
            Page(
                chapter_id=chapter.id,
                index=next_index,
                image_url=url,
                media_kind=kind,
                tg_message_id=mid,
            )
        )
        if kind == "video":
            added_videos.append(url)
        if mid is not None:
            stored_ids.append(mid)
            seen_ids.add(mid)
        seen_urls.add(url)
        next_index += 1
        added += 1

    manga_result = await session.execute(select(Manga).where(Manga.id == chapter.manga_id))
    manga = manga_result.scalar_one_or_none()
    if manga and (author or tags):
        if author and not manga.author:
            manga.author = author.strip()
        if tags:
            manga.tags = json.dumps(_merge_tags(manga.tags, tags), ensure_ascii=False)

    if added == 0:
        if manga:
            manga.updated_at = utcnow()
            await session.commit()
        return None

    chapter.tg_outbound_attempts = 0
    chapter.tg_outbound_retry_at = None
    chapter.tg_outbound_last_error = None
    await session.flush()
    image_count = (
        await session.execute(
            select(func.count()).select_from(Page).where(
                Page.chapter_id == chapter.id,
                or_(Page.media_kind.is_(None), Page.media_kind != "video"),
            )
        )
    ).scalar_one()
    chapter.page_count = int(image_count)
    chapter.source_message_ids = json.dumps(stored_ids)
    chapter.updated_at = utcnow()
    if manga:
        await _refresh_manga_counts(session, manga)
    await session.commit()
    logger.info(
        "Appended %d pages to chapter=%s source_key=%s total=%d",
        added,
        chapter.id,
        chapter.source_key,
        chapter.page_count,
    )
    album_sent, sent_videos = read_outbound_state(chapter)
    return PublishedChapter(
        manga_id=chapter.manga_id,
        chapter_id=chapter.id,
        slug=manga.slug if manga else "",
        number=chapter.number,
        page_count=chapter.page_count,
        created=False,
        new_video_urls=added_videos,
        album_sent=album_sent,
        sent_video_urls=sent_videos,
        outbound_attempts=int(getattr(chapter, "tg_outbound_attempts", 0) or 0),
        site_published_page_count=int(
            getattr(chapter, "site_published_page_count", 0) or 0
        ),
        site_manga_id=_positive_int(getattr(chapter, "site_manga_id", None)),
        site_chapter_id=_positive_int(getattr(chapter, "site_chapter_id", None)),
    )


async def list_chapter_image_urls(session: AsyncSession, chapter_id: int) -> list[str]:
    rows = (
        await session.execute(
            select(Page.image_url)
            .where(
                Page.chapter_id == chapter_id,
                or_(Page.media_kind.is_(None), Page.media_kind != "video"),
            )
            .order_by(Page.index)
        )
    ).all()
    return [str(row[0]) for row in rows if row[0]]


async def list_chapter_videos(session: AsyncSession, chapter_id: int) -> list[str]:
    rows = (
        await session.execute(
            select(Page.image_url)
            .where(Page.chapter_id == chapter_id, Page.media_kind == "video")
            .order_by(Page.index)
        )
    ).all()
    return [str(row[0]) for row in rows if row[0]]


async def next_chapter_number(session: AsyncSession, manga_id: int) -> int:
    result = await session.execute(
        select(func.coalesce(func.max(Chapter.number), 0)).where(Chapter.manga_id == manga_id)
    )
    return int(result.scalar_one()) + 1


async def publish_chapter(
    session: AsyncSession,
    *,
    title: str,
    chapter_title: str | None,
    source_key: str,
    source_chat_id: str | None,
    source_chat_title: str | None,
    image_urls: list[str],
    message_ids: list[int],
    author: str | None = None,
    tags: list[str] | None = None,
    media_kinds: list[str] | None = None,
    force: bool = False,
) -> PublishedChapter | None:
    title = clean_display_title(title) or (title or "").strip()
    chapter_title = clean_display_title(chapter_title) or None
    if not image_urls:
        return None

    duplicate = await _find_chapter_by_source_key(session, source_key)
    if duplicate is not None and not force:
        appended = await _append_chapter_pages(
            session,
            chapter=duplicate,
            image_urls=image_urls,
            message_ids=message_ids,
            author=author,
            tags=tags,
            media_kinds=media_kinds,
        )
        if appended is None:
            logger.info("Skip duplicate chapter source_key=%s", source_key)
        return appended

    video_only = bool(media_kinds) and all(
        kind == "video" for kind in media_kinds[: len(image_urls)]
    )
    if video_only and not force:
        # 剧情视频只挂到已有同名作品；绝不单独开 0 页漫画（广告片/群广告）
        existing = (
            await session.execute(select(Manga).where(Manga.title == title).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            latest = (
                await session.execute(
                    select(Chapter)
                    .where(Chapter.manga_id == existing.id)
                    .order_by(Chapter.number.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is not None:
                return await _append_chapter_pages(
                    session,
                    chapter=latest,
                    image_urls=image_urls,
                    message_ids=message_ids,
                    author=author,
                    tags=tags,
                    media_kinds=media_kinds,
                )
        logger.info("Skip video-only chapter without existing manga title=%s", title)
        return None

    manga = await get_or_create_manga(
        session,
        title=title,
        source_chat_id=source_chat_id,
        source_chat_title=source_chat_title,
        cover_url=next(
            (
                url
                for idx, url in enumerate(image_urls)
                if not (media_kinds and idx < len(media_kinds) and media_kinds[idx] == "video")
            ),
            None,
        ),
        author=author,
        tags=tags,
    )
    number = await next_chapter_number(session, manga.id)
    image_count = sum(
        1
        for idx, _url in enumerate(image_urls)
        if not (media_kinds and idx < len(media_kinds) and media_kinds[idx] == "video")
    )
    chapter = Chapter(
        manga_id=manga.id,
        number=number,
        title=chapter_title or f"第 {number} 话",
        source_key=source_key,
        source_message_ids=json.dumps(message_ids),
        page_count=image_count,
        is_published=True,
    )
    session.add(chapter)
    await session.flush()

    for idx, url in enumerate(image_urls):
        kind = "video" if media_kinds and idx < len(media_kinds) and media_kinds[idx] == "video" else "image"
        session.add(
            Page(
                chapter_id=chapter.id,
                index=idx,
                image_url=url,
                media_kind=kind,
                tg_message_id=message_ids[idx] if idx < len(message_ids) else None,
            )
        )

    await _refresh_manga_counts(session, manga)
    first_image = next(
        (
            url
            for idx, url in enumerate(image_urls)
            if not (media_kinds and idx < len(media_kinds) and media_kinds[idx] == "video")
        ),
        None,
    )
    if first_image and not manga.cover_url:
        manga.cover_url = first_image
    manga.updated_at = utcnow()

    session.add(
        IngestEvent(
            event_type="chapter_published",
            source_key=source_key,
            detail=json.dumps(
                {
                    "manga_id": manga.id,
                    "chapter_id": chapter.id,
                    "pages": chapter.page_count,
                    "title": title,
                },
                ensure_ascii=False,
            ),
        )
    )
    await session.commit()
    logger.info(
        "Published manga=%s chapter=%s pages=%d source_key=%s",
        manga.slug,
        number,
        chapter.page_count,
        source_key,
    )
    new_video_urls = [
        url
        for idx, url in enumerate(image_urls)
        if media_kinds and idx < len(media_kinds) and media_kinds[idx] == "video"
    ]
    album_sent, sent_videos = read_outbound_state(chapter)
    return PublishedChapter(
        manga_id=manga.id,
        chapter_id=chapter.id,
        slug=manga.slug,
        number=number,
        page_count=chapter.page_count,
        created=True,
        new_video_urls=new_video_urls,
        album_sent=album_sent,
        sent_video_urls=sent_videos,
        outbound_attempts=int(getattr(chapter, "tg_outbound_attempts", 0) or 0),
        site_published_page_count=int(
            getattr(chapter, "site_published_page_count", 0) or 0
        ),
        site_manga_id=_positive_int(getattr(chapter, "site_manga_id", None)),
        site_chapter_id=_positive_int(getattr(chapter, "site_chapter_id", None)),
    )


async def load_stored_outbound(session: AsyncSession, source_key: str) -> StoredOutbound | None:
    """按讨论帖 source_key 取出已入库章节，供空桶 is_final / 启动补发使用。"""
    chapter = await _find_chapter_by_source_key(session, source_key)
    if chapter is None:
        return None
    manga = (
        await session.execute(select(Manga).where(Manga.id == chapter.manga_id))
    ).scalar_one_or_none()
    album_sent, sent_videos = read_outbound_state(chapter)
    published = PublishedChapter(
        manga_id=chapter.manga_id,
        chapter_id=chapter.id,
        slug=manga.slug if manga else "",
        number=chapter.number,
        page_count=int(chapter.page_count or 0),
        created=False,
        album_sent=album_sent,
        sent_video_urls=sent_videos,
        outbound_attempts=int(getattr(chapter, "tg_outbound_attempts", 0) or 0),
        site_published_page_count=int(
            getattr(chapter, "site_published_page_count", 0) or 0
        ),
        site_manga_id=_positive_int(getattr(chapter, "site_manga_id", None)),
        site_chapter_id=_positive_int(getattr(chapter, "site_chapter_id", None)),
    )
    image_urls = await list_chapter_image_urls(session, chapter.id)
    video_urls = await list_chapter_videos(session, chapter.id)
    if not image_urls and not video_urls:
        return None
    title = ((manga.title if manga else "") or chapter.title or "").strip()
    author = (manga.author if manga else None) or None
    if author:
        author = author.strip() or None
    return StoredOutbound(
        source_key=str(chapter.source_key),
        published=published,
        title=title,
        author=author,
        tags=_parse_tags(manga.tags if manga else None),
        source_chat_id=manga.source_chat_id if manga else None,
        source_chat_title=manga.source_chat_title if manga else None,
        image_urls=image_urls,
        video_urls=video_urls,
    )


async def list_pending_reply_outbound(
    session: AsyncSession,
    *,
    idle_before: datetime,
    limit: int = 40,
    include_outbound: bool = True,
    include_site: bool = False,
) -> list[StoredOutbound]:
    """Return closed Telegram chapters whose outbound post is due for retry."""
    delivery_conditions = []
    if include_outbound:
        delivery_conditions.append(
            or_(
                Chapter.tg_album_sent.is_not(True),
                func.coalesce(Chapter.tg_outbound_attempts, 0) > 0,
                exists().where(
                    Page.chapter_id == Chapter.id,
                    Page.media_kind == "video",
                ),
            )
        )
    if include_site:
        delivery_conditions.append(
            func.coalesce(Chapter.site_published_page_count, 0) < Chapter.page_count
        )
    if not delivery_conditions:
        return []
    rows = (
        await session.execute(
            select(Chapter.source_key)
            .where(
                or_(
                    Chapter.source_key.like("%:r:%"),
                    Chapter.source_key.like("%:g:%"),
                    Chapter.source_key.like("%:m:%"),
                ),
                or_(*delivery_conditions),
                Chapter.page_count > 0,
                Chapter.updated_at <= idle_before,
                func.coalesce(Chapter.tg_outbound_attempts, 0) < OUTBOUND_MAX_ATTEMPTS,
                or_(
                    Chapter.tg_outbound_retry_at.is_(None),
                    Chapter.tg_outbound_retry_at <= utcnow(),
                ),
            )
            .order_by(Chapter.updated_at.asc())
        )
    ).all()
    found: list[StoredOutbound] = []
    for (source_key,) in rows:
        stored = await load_stored_outbound(session, str(source_key))
        if stored is None:
            continue
        sent_videos = set(stored.published.sent_video_urls or [])
        album_pending = include_outbound and bool(stored.image_urls) and not stored.published.album_sent
        video_pending = include_outbound and any(
            url not in sent_videos for url in stored.video_urls
        )
        site_pending = (
            include_site
            and bool(stored.image_urls)
            and stored.published.site_published_page_count < len(stored.image_urls)
        )
        if not album_pending and not video_pending and not site_pending:
            continue
        found.append(stored)
        if len(found) >= max(1, int(limit)):
            break
    return found


async def mark_site_publish_succeeded(
    session: AsyncSession,
    chapter_id: int,
    *,
    page_count: int,
    response: dict[str, object],
) -> tuple[int | None, int | None]:
    """Persist remote publish progress and IDs used by the public reader URL."""
    chapter = (
        await session.execute(select(Chapter).where(Chapter.id == chapter_id))
    ).scalar_one_or_none()
    if chapter is None:
        return site_publish_ids(response)
    manga_id, remote_chapter_id = site_publish_ids(response)
    manga_id = manga_id or _positive_int(getattr(chapter, "site_manga_id", None))
    chapter.site_published_page_count = max(
        int(getattr(chapter, "site_published_page_count", 0) or 0),
        max(0, int(page_count)),
    )
    if manga_id is not None:
        chapter.site_manga_id = manga_id
    if remote_chapter_id is not None:
        chapter.site_chapter_id = remote_chapter_id
    await session.commit()
    return (
        _positive_int(getattr(chapter, "site_manga_id", None)),
        _positive_int(getattr(chapter, "site_chapter_id", None)),
    )


async def mark_outbound_failed(
    session: AsyncSession,
    chapter_id: int,
    error: BaseException | str,
    *,
    now: datetime | None = None,
) -> OutboundRetryState:
    """Persist bounded retry state so one bad post cannot run every sweep forever."""
    chapter = (
        await session.execute(select(Chapter).where(Chapter.id == chapter_id))
    ).scalar_one_or_none()
    if chapter is None:
        return OutboundRetryState(OUTBOUND_MAX_ATTEMPTS, None, True)
    attempts = int(getattr(chapter, "tg_outbound_attempts", 0) or 0) + 1
    attempts = min(attempts, OUTBOUND_MAX_ATTEMPTS)
    exhausted = attempts >= OUTBOUND_MAX_ATTEMPTS
    retry_at = None
    if not exhausted:
        delay = OUTBOUND_RETRY_DELAYS_SECONDS[attempts - 1]
        retry_at = (now or utcnow()) + timedelta(seconds=delay)
    chapter.tg_outbound_attempts = attempts
    chapter.tg_outbound_retry_at = retry_at
    chapter.tg_outbound_last_error = str(error).strip()[:2000] or type(error).__name__
    await session.commit()
    return OutboundRetryState(attempts, retry_at, exhausted)


async def mark_outbound_sent(
    session: AsyncSession,
    chapter_id: int,
    *,
    album: bool,
    video_urls: list[str] | None = None,
) -> None:
    """记下已经发到运营频道的预览图/视频，补页时不再重发。"""
    chapter = (
        await session.execute(select(Chapter).where(Chapter.id == chapter_id))
    ).scalar_one_or_none()
    if chapter is None:
        return
    if album:
        chapter.tg_album_sent = True
    sent = _parse_sent_videos(getattr(chapter, "tg_sent_videos", None))
    for url in video_urls or []:
        item = str(url).strip()
        if item and item not in sent:
            sent.append(item)
    chapter.tg_sent_videos = json.dumps(sent, ensure_ascii=False)
    chapter.tg_outbound_attempts = 0
    chapter.tg_outbound_retry_at = None
    chapter.tg_outbound_last_error = None
    chapter.updated_at = utcnow()
    await session.commit()
