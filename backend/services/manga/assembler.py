from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backend.services.ai.refine import refine_manga_chapter

from .config import MangaSettings
from .filters import parse_caption

logger = logging.getLogger(__name__)

# Telegram albums are at most 10 photos. Uploaders often pause several minutes
# between albums. A short idle (8–90s) was closing IF-line chapters at ~30P.
REPLY_CHECKPOINT_SECONDS = 90.0
REPLY_IDLE_FLOOR_SECONDS = 900.0


def bucket_idle_seconds(bucket_key: str, idle: float, reply_idle: float = REPLY_IDLE_FLOOR_SECONDS) -> float:
    """How long to keep a live bucket open after the last page.

    Discussion reply threads use a much longer floor so a slow album burst
    stays in the same chapter. Non-reply albums still follow the short idle.
    """
    if ":r:" in bucket_key:
        return max(float(idle), float(reply_idle), REPLY_IDLE_FLOOR_SECONDS)
    return max(float(idle), 1.0)


def bucket_checkpoint_seconds(bucket_key: str, idle: float) -> float:
    """Publish what we already have without closing a slow reply thread."""
    if ":r:" in bucket_key:
        return max(float(idle), REPLY_CHECKPOINT_SECONDS)
    return bucket_idle_seconds(bucket_key, idle)


def collapse_reply_root(
    reply_id: int,
    *,
    parent_of: dict[int, int],
    known_roots: set[int],
    page_to_root: dict[int, int],
    max_hops: int = 24,
) -> int:
    """Walk reply-to-previous-page chains back to the original title-card id."""
    current = int(reply_id)
    seen: set[int] = set()
    for _ in range(max_hops):
        if current in known_roots:
            return current
        mapped = page_to_root.get(current)
        if mapped is not None:
            return int(mapped)
        if current in seen:
            break
        seen.add(current)
        parent = parent_of.get(current)
        if parent is None:
            break
        current = int(parent)
    return int(reply_id)


@dataclass
class PendingPage:
    message_id: int
    local_path: Path
    filename: str
    caption: str | None
    grouped_id: int | None
    date: datetime
    sender_id: int | None
    reply_to_msg_id: int | None
    metadata_caption: str | None = None
    kind: str = "image"


@dataclass
class PendingChapter:
    bucket_key: str
    chat_id: int
    chat_title: str | None
    pages: list[PendingPage] = field(default_factory=list)
    last_update: float = 0.0
    title_hint: str | None = None
    author_hint: str | None = None
    tags: list[str] = field(default_factory=list)
    source_key: str | None = None
    # False = 讨论帖还在继续（checkpoint）；True = 线程收齐，可以发频道
    is_final: bool = True


def make_source_key(chat_id: int, pages: list[PendingPage]) -> str:
    ids = sorted(p.message_id for p in pages)
    raw = f"{chat_id}:{','.join(map(str, ids))}"
    if pages and pages[0].reply_to_msg_id is not None:
        raw = f"{chat_id}:r{pages[0].reply_to_msg_id}"
    elif pages and pages[0].grouped_id is not None:
        raw = f"{chat_id}:g{pages[0].grouped_id}"
    return hashlib.sha1(raw.encode()).hexdigest()


class ChapterAssembler:
    """Buffers media messages and flushes a chapter after idle timeout."""

    def __init__(self, settings: MangaSettings, on_ready):
        self.settings = settings
        self.on_ready = on_ready  # async callback(PendingChapter)
        self._buckets: dict[str, PendingChapter] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._watchdog())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        async with self._lock:
            keys = list(self._buckets.keys())
        for key in keys:
            await self._flush(key)

    def _bucket_key(self, page: PendingPage, chat_id: int) -> str:
        if page.reply_to_msg_id is not None:
            return f"{chat_id}:r:{page.reply_to_msg_id}"
        if page.grouped_id is not None:
            return f"{chat_id}:g:{page.grouped_id}"
        sender = page.sender_id or 0
        return f"{chat_id}:s:{sender}:open"

    async def add_page(self, chat_id: int, chat_title: str | None, page: PendingPage) -> None:
        key = self._bucket_key(page, chat_id)
        now = datetime.now(timezone.utc).timestamp()
        sibling_keys: list[str] = []
        async with self._lock:
            if key not in self._buckets and ":r:" in key:
                prefix = f"{chat_id}:r:"
                sibling_keys = [
                    existing
                    for existing in self._buckets
                    if existing.startswith(prefix) and existing != key
                ]
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = PendingChapter(
                    bucket_key=key,
                    chat_id=chat_id,
                    chat_title=chat_title,
                    last_update=now,
                )
                self._buckets[key] = bucket
            if not bucket.source_key:
                if page.reply_to_msg_id is not None:
                    bucket.source_key = f"{chat_id}:r:{page.reply_to_msg_id}"
                elif page.grouped_id is not None:
                    bucket.source_key = f"{chat_id}:g:{page.grouped_id}"
            bucket.pages.append(page)
            bucket.last_update = now
            for caption in (page.metadata_caption, page.caption):
                if not caption:
                    continue
                parsed = parse_caption(caption)
                if parsed.title and not bucket.title_hint:
                    bucket.title_hint = parsed.title
                if parsed.author and not bucket.author_hint:
                    bucket.author_hint = parsed.author
                if parsed.tags:
                    # merge unique tags
                    for t in parsed.tags:
                        if t not in bucket.tags:
                            bucket.tags.append(t)
            should_flush = len(bucket.pages) >= self.settings.chapter_max_pages
        for sibling in sibling_keys:
            await self._flush(sibling)
        if should_flush:
            await self._checkpoint(key)

    async def _watchdog(self) -> None:
        idle = self.settings.chapter_idle_seconds
        reply_idle = float(getattr(self.settings, "chapter_reply_idle_seconds", REPLY_IDLE_FLOOR_SECONDS))
        while True:
            await asyncio.sleep(max(1.0, min(idle, REPLY_CHECKPOINT_SECONDS) / 2))
            now = datetime.now(timezone.utc).timestamp()
            to_flush: list[str] = []
            to_checkpoint: list[str] = []
            async with self._lock:
                for key, bucket in self._buckets.items():
                    quiet = now - bucket.last_update
                    close_after = bucket_idle_seconds(key, idle, reply_idle)
                    if quiet >= close_after:
                        to_flush.append(key)
                    elif (
                        ":r:" in key
                        and bucket.pages
                        and quiet >= bucket_checkpoint_seconds(key, idle)
                    ):
                        to_checkpoint.append(key)
            for key in to_flush:
                await self._flush(key)
            for key in to_checkpoint:
                await self._checkpoint(key)

    async def _checkpoint(self, key: str) -> None:
        """Publish current pages but keep the reply-thread bucket open."""
        async with self._lock:
            bucket = self._buckets.get(key)
            if not bucket or not bucket.pages:
                return
            pages = list(bucket.pages)
            bucket.pages = []
            snapshot = PendingChapter(
                bucket_key=bucket.bucket_key,
                chat_id=bucket.chat_id,
                chat_title=bucket.chat_title,
                pages=pages,
                last_update=bucket.last_update,
                title_hint=bucket.title_hint,
                author_hint=bucket.author_hint,
                tags=list(bucket.tags),
                source_key=bucket.source_key,
                is_final=False,
            )
        snapshot.pages.sort(key=lambda p: p.message_id)
        snapshot = await refine_manga_chapter(self.settings, snapshot)
        try:
            await self.on_ready(snapshot)
        except Exception:
            logger.exception("Failed to checkpoint chapter for bucket %s", key)
            for p in snapshot.pages:
                try:
                    p.local_path.unlink(missing_ok=True)
                except Exception:
                    pass

    async def live_source_keys(self) -> set[str]:
        """Bucket keys still buffering pages (skip stale outbound sweep)."""
        async with self._lock:
            keys: set[str] = set()
            for key, bucket in self._buckets.items():
                keys.add(key)
                if bucket.source_key:
                    keys.add(bucket.source_key)
            return keys

    def _stable_source_key(self, bucket: PendingChapter, key: str) -> str | None:
        if bucket.source_key:
            return bucket.source_key
        if ":r:" in key or ":g:" in key:
            return key
        return None

    async def _flush(self, key: str) -> None:
        async with self._lock:
            bucket = self._buckets.pop(key, None)
        if not bucket:
            return
        source_key = self._stable_source_key(bucket, key)
        if not bucket.pages:
            # Checkpoint 已经把页发到主站；空桶仍要发 is_final，否则运营频道永远等不到收齐。
            if ":r:" not in key or not source_key:
                return
            snapshot = PendingChapter(
                bucket_key=bucket.bucket_key,
                chat_id=bucket.chat_id,
                chat_title=bucket.chat_title,
                pages=[],
                last_update=bucket.last_update,
                title_hint=bucket.title_hint,
                author_hint=bucket.author_hint,
                tags=list(bucket.tags),
                source_key=source_key,
                is_final=True,
            )
            logger.info(
                "Finalize discussion thread source_key=%s with checkpointed pages only",
                source_key,
            )
            try:
                await self.on_ready(snapshot)
            except Exception:
                logger.exception("Failed to finalize empty chapter for bucket %s", key)
            return
        bucket.pages.sort(key=lambda p: p.message_id)
        if source_key:
            bucket.source_key = source_key
        bucket.is_final = True
        bucket = await refine_manga_chapter(self.settings, bucket)
        try:
            await self.on_ready(bucket)
        except Exception:
            logger.exception("Failed to publish chapter for bucket %s", key)
            for p in bucket.pages:
                try:
                    p.local_path.unlink(missing_ok=True)
                except Exception:
                    pass
