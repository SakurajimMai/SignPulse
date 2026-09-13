from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from backend.services.alerts import schedule_alert
from backend.services.manga.channel_publish import (
    ChannelPublisher,
    can_publish_outbound,
)
from backend.services.manga.config import MangaSettings
from backend.services.manga.db import Manga, get_session_factory
from backend.services.manga.imgbed import ImgBedClient
from backend.services.manga.publisher import (
    PublishedChapter,
    _find_chapter_by_source_key,
    list_chapter_image_urls,
    mark_outbound_sent,
    publish_chapter,
    read_outbound_state,
)
from backend.services.manga.site_publish import SitePublisher

from .categories import resolve_categories
from .client import ImageMissing, WnacgClient, WnacgError
from .parser import AlbumMeta, AlbumRef

logger = logging.getLogger(__name__)

FLUSH_EVERY = 10
RECENT_LIMIT = 40
SOURCE_CHAT_ID = "wnacg"
SOURCE_CHAT_TITLE = "WNACG"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        url = str(exc.request.url) if exc.request is not None else ""
        code = exc.response.status_code if exc.response is not None else "?"
        return f"WNACG 请求失败 HTTP {code} {url}".strip()[:300]
    text = str(exc).strip() or exc.__class__.__name__
    if "For more information check:" in text:
        text = text.split("For more information check:", 1)[0].strip()
    return text[:300]


class _RoutedGalleryError(RuntimeError):
    def __init__(self, rule_id: str, alert_title: str, cause: BaseException | str):
        detail = _public_error(cause) if isinstance(cause, BaseException) else str(cause)
        super().__init__(detail)
        self.rule_id = rule_id
        self.alert_title = alert_title


def poll_seconds(settings: MangaSettings) -> float:
    return max(float(settings.wnacg_poll_seconds or 300.0), 60.0)


class WnacgWorker:
    def __init__(self, settings: MangaSettings, client_provider: Any | None = None):
        self.settings = settings
        self.imgbed = ImgBedClient(settings)
        self.site = SitePublisher(settings)
        self.channel = ChannelPublisher(settings, client_provider or (lambda: None))
        self._stopped = asyncio.Event()
        self._wake = asyncio.Event()
        self._closing = False
        self._closed = False
        self.last_error: str | None = None
        self.phase = "idle"
        self.next_pass_at: str | None = None
        self.current: dict[str, Any] = {}
        self.stats = {"processed": 0, "skipped": 0, "failed": 0}
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_LIMIT)
        self._http: WnacgClient | None = None

    def request_pass(self) -> None:
        self._wake.set()

    def status(self) -> dict[str, Any]:
        from .categories import CATEGORIES, parse_category_ids

        settings = self.settings
        running = not self._stopped.is_set() and not self._closing
        selected = parse_category_ids(settings.wnacg_categories)
        return {
            "worker_status": "running" if running else "stopped",
            "last_error": self.last_error,
            "base_url": settings.wnacg_base_url,
            "categories": [item.as_dict() for item in CATEGORIES],
            "selected": selected,
            "category_count": len(selected),
            "current": dict(self.current),
            "processed": self.stats["processed"],
            "skipped": self.stats["skipped"],
            "failed": self.stats["failed"],
            "recent": list(self.recent),
            "phase": self.phase,
            "next_pass_at": self.next_pass_at,
            "poll_seconds": poll_seconds(settings),
            "listening": running,
        }

    async def stop(self) -> None:
        self._closing = True
        self._stopped.set()
        self._wake.set()
        if self._closed:
            return
        self._closed = True
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        await self.imgbed.aclose()
        await self.site.aclose()

    async def wait(self) -> None:
        await self._stopped.wait()

    async def run_until_stopped(self) -> None:
        try:
            await self._loop()
        finally:
            await self.stop()

    async def _loop(self) -> None:
        settings = self.settings
        if not settings.cfbed_upload_url:
            raise WnacgError("请先配置图床，WNACG 图片要先上传再发主站")
        categories = resolve_categories(settings.wnacg_categories)
        if not categories:
            raise WnacgError("请至少选择一个 WNACG 分类")
        self._http = WnacgClient(
            base_url=settings.wnacg_base_url,
            delay_seconds=float(settings.wnacg_delay_seconds or 1.0),
        )
        while not self._stopped.is_set():
            self.phase = "searching"
            self.next_pass_at = None
            try:
                await self._run_pass(categories)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = _public_error(exc)
                logger.exception("WNACG 采集一轮失败")
                schedule_alert(
                    "manga_wnacg_gallery_fail",
                    title="WNACG 采集一轮失败",
                    detail=self.last_error,
                    fingerprint="pass",
                )
            self.current = {}
            if self._stopped.is_set():
                break
            await self._sleep_until_next_pass(poll_seconds(self.settings))
        self.phase = "idle"
        self.next_pass_at = None

    async def _sleep_until_next_pass(self, interval: float) -> None:
        if self._stopped.is_set():
            return
        if self._wake.is_set():
            self._wake.clear()
            return
        until = datetime.now(timezone.utc) + timedelta(seconds=interval)
        self.next_pass_at = until.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.phase = "waiting"
        stop_wait = asyncio.create_task(self._stopped.wait())
        wake_wait = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_wait, wake_wait},
                timeout=interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (stop_wait, wake_wait):
                if not task.done():
                    task.cancel()
            for task in (stop_wait, wake_wait):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._wake.clear()

    async def _ensure_http(self) -> WnacgClient:
        if self._http is not None:
            return self._http
        settings = self.settings
        self._http = WnacgClient(
            base_url=settings.wnacg_base_url,
            delay_seconds=float(settings.wnacg_delay_seconds or 1.0),
        )
        return self._http

    async def _run_pass(self, categories) -> None:
        assert self._http is not None
        self.last_error = None
        settings = self.settings
        pages = max(1, min(int(settings.wnacg_list_pages or 1), 10))
        gallery_delay = max(float(settings.wnacg_gallery_delay_seconds or 3.0), 0.0)
        seen: set[str] = set()
        for category in categories:
            if self._stopped.is_set():
                return
            logger.info("WNACG 分类 %s pages=%s", category.id, pages)
            refs = await self._http.list_albums(category, pages=pages)
            for ref in refs:
                if self._stopped.is_set():
                    return
                if ref.source_key in seen:
                    continue
                seen.add(ref.source_key)
                await self._handle_ref(ref)
                if gallery_delay:
                    try:
                        await asyncio.wait_for(self._stopped.wait(), timeout=gallery_delay)
                    except asyncio.TimeoutError:
                        pass

    async def _handle_ref(self, ref: AlbumRef) -> None:
        settings = self.settings
        max_pages = max(int(settings.wnacg_max_pages or 400), 1)
        if ref.page_count and ref.page_count > max_pages:
            self.stats["skipped"] += 1
            extra = await self._card_fields(ref.source_key)
            self._remember(
                ref.url,
                title=ref.title,
                status="skipped",
                detail=f"页数 {ref.page_count} > {max_pages}",
                extra=extra,
            )
            return
        existing = await self._existing_page_count(ref.source_key)
        if existing:
            self.stats["skipped"] += 1
            extra = await self._card_fields(ref.source_key)
            self._remember(
                ref.url,
                title=str(extra.get("title") or ref.title or ref.source_key),
                status="duplicate",
                pages=int(extra.get("page_count") or existing),
                extra=extra,
            )
            return
        try:
            await self._ingest_album(ref)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats["failed"] += 1
            detail = _public_error(exc)
            self.last_error = detail
            extra = await self._card_fields(ref.source_key)
            self._remember(
                ref.url,
                title=str(extra.get("title") or ref.title or ref.source_key),
                status="failed",
                detail=detail,
                extra=extra,
            )
            logger.exception("WNACG 相册失败 %s", ref.url)
            rule_id = "manga_wnacg_gallery_fail"
            alert_title = "WNACG 相册失败"
            if isinstance(exc, _RoutedGalleryError):
                rule_id = exc.rule_id
                alert_title = exc.alert_title
            schedule_alert(
                rule_id,
                title=f"{alert_title}：{extra.get('title') or ref.title or ref.source_key}",
                detail=f"{ref.url}\n{detail}",
                fingerprint=str(ref.source_key or ref.url),
            )

    async def _ingest_album(self, ref: AlbumRef) -> None:
        assert self._http is not None
        settings = self.settings
        existing_pages = await self._existing_page_count(ref.source_key)
        max_pages = max(int(settings.wnacg_max_pages or 400), 1)
        meta = await self._http.fetch_album(ref)
        title = meta.title
        self.current = {
            "title": title,
            "url": ref.url,
            "pages": existing_pages,
            "total": meta.page_count or len(meta.image_urls),
        }
        expected = meta.page_count or len(meta.image_urls)
        if expected > max_pages:
            self.stats["skipped"] += 1
            extra = await self._card_fields(ref.source_key)
            self._remember(
                ref.url,
                title=title,
                status="skipped",
                detail=f"页数 {expected} > {max_pages}",
                extra=extra,
            )
            return
        if existing_pages and expected and existing_pages >= expected:
            self.stats["skipped"] += 1
            extra = await self._card_fields(ref.source_key)
            self._remember(
                ref.url,
                title=str(extra.get("title") or title),
                status="duplicate",
                pages=int(extra.get("page_count") or existing_pages),
                extra=extra,
            )
            return
        image_urls = meta.image_urls
        if existing_pages:
            image_urls = image_urls[existing_pages:]
        if not image_urls:
            self.stats["skipped"] += 1
            self._remember(ref.url, title=title, status="empty")
            return

        published: PublishedChapter | None = None
        site_urls: list[str] = []
        batch: list[str] = []
        stored = existing_pages
        skipped_pages = 0
        tags = list(meta.tags)

        async def flush() -> None:
            nonlocal published, site_urls, batch
            if not batch:
                return
            factory = get_session_factory()
            async with factory() as session:
                published = await publish_chapter(
                    session,
                    title=title,
                    chapter_title=title,
                    source_key=ref.source_key,
                    source_chat_id=SOURCE_CHAT_ID,
                    source_chat_title=SOURCE_CHAT_TITLE,
                    author=meta.author,
                    tags=tags,
                    image_urls=list(batch),
                    message_ids=[],
                )
                if published:
                    site_urls = await list_chapter_image_urls(session, published.chapter_id)
            batch = []
            if published and self.site.enabled and site_urls:
                try:
                    body = await self.site.publish_chapter(
                        title=title,
                        chapter_title=title,
                        source_key=ref.source_key,
                        source_chat_id=SOURCE_CHAT_ID,
                        source_chat_title=SOURCE_CHAT_TITLE,
                        author=meta.author,
                        tags=tags,
                        image_urls=site_urls,
                        local=published,
                    )
                    if body is None:
                        raise _RoutedGalleryError(
                            "manga_site_fail",
                            "WNACG 主站发布失败",
                            "站点返回空响应",
                        )
                except asyncio.CancelledError:
                    raise
                except _RoutedGalleryError:
                    raise
                except Exception as exc:
                    raise _RoutedGalleryError(
                        "manga_site_fail", "WNACG 主站发布失败", exc
                    ) from exc

        for image_url in image_urls:
            if self._stopped.is_set():
                await flush()
                return
            try:
                data = await self._http.download_image(image_url, ref.url)
            except ImageMissing:
                skipped_pages += 1
                logger.warning("WNACG 跳过失效原图 %s", image_url)
                continue
            except httpx.HTTPStatusError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                skipped_pages += 1
                logger.warning("WNACG 跳过 404 原图 %s", image_url)
                continue
            stored += 1
            try:
                uploaded = await self.imgbed.upload_bytes(
                    data, filename=f"wnacg{ref.aid}-{stored:03d}.jpg"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _RoutedGalleryError(
                    "manga_imgbed_upload_fail",
                    "WNACG 图床上传失败",
                    exc,
                ) from exc
            batch.append(uploaded)
            self.current["pages"] = stored
            if len(batch) >= FLUSH_EVERY:
                await flush()

        await flush()
        if published is None:
            self.stats["failed"] += 1
            detail = "没有写入页"
            if skipped_pages:
                detail = f"原图失效 {skipped_pages} 张，没有写入页"
            self._remember(ref.url, title=title, status="failed", detail=detail)
            schedule_alert(
                "manga_wnacg_gallery_fail",
                title=f"WNACG 相册没有写入页：{title or ref.source_key}",
                detail=f"{ref.url}\n{detail}",
                fingerprint=str(ref.source_key or ref.url),
            )
            return
        await self._outbound(meta, published, site_urls)
        self.stats["processed"] += 1
        extra = await self._card_fields(ref.source_key)
        extra.setdefault("manga_id", published.manga_id)
        extra.setdefault("slug", published.slug)
        extra.setdefault("page_count", published.page_count)
        extra.setdefault("chapter_count", 1)
        extra.setdefault("author", meta.author)
        extra.setdefault("title", title)
        detail = f"跳过失效页 {skipped_pages}" if skipped_pages else ""
        self._remember(
            ref.url,
            title=str(extra.get("title") or title),
            status="ok",
            pages=int(extra.get("page_count") or published.page_count),
            detail=detail,
            extra=extra,
        )
        logger.info("WNACG 入库 %s pages=%s", title, published.page_count)

    async def _outbound(
        self,
        meta: AlbumMeta,
        published: PublishedChapter,
        site_urls: list[str],
    ) -> None:
        if not self.channel.enabled:
            return
        album_sent, _sent = published.album_sent, published.sent_video_urls
        if album_sent:
            return
        site_ok: bool | None = True
        if self.site.enabled:
            if not site_urls:
                return
            try:
                body = await self.site.publish_chapter(
                    title=meta.title,
                    chapter_title=meta.title,
                    source_key=meta.ref.source_key,
                    source_chat_id=SOURCE_CHAT_ID,
                    source_chat_title=SOURCE_CHAT_TITLE,
                    author=meta.author,
                    tags=list(meta.tags),
                    image_urls=site_urls,
                    local=published,
                )
                if body is None:
                    raise _RoutedGalleryError(
                        "manga_site_fail",
                        "WNACG 主站发布失败",
                        "站点返回空响应",
                    )
            except asyncio.CancelledError:
                raise
            except _RoutedGalleryError:
                raise
            except Exception as exc:
                raise _RoutedGalleryError(
                    "manga_site_fail", "WNACG 主站发布失败", exc
                ) from exc
        if not can_publish_outbound(
            channel_enabled=True,
            send_album=True,
            video_urls=[],
            site_enabled=self.site.enabled,
            has_site_images=bool(site_urls),
            site_published=site_ok,
        ):
            return
        try:
            posted = await self.channel.publish_chapter(
                title=meta.title,
                author=meta.author,
                tags=list(meta.tags),
                image_urls=site_urls,
                local=PublishedChapter(
                    manga_id=published.manga_id,
                    chapter_id=published.chapter_id,
                    slug=published.slug,
                    number=published.number,
                    page_count=published.page_count,
                    created=True,
                ),
            )
            if not posted:
                raise _RoutedGalleryError(
                    "manga_channel_publish_fail",
                    "WNACG 出站频道发布失败",
                    "频道发布返回失败",
                )
        except asyncio.CancelledError:
            raise
        except _RoutedGalleryError:
            raise
        except Exception as exc:
            raise _RoutedGalleryError(
                "manga_channel_publish_fail", "WNACG 出站频道发布失败", exc
            ) from exc
        if posted:
            factory = get_session_factory()
            async with factory() as session:
                current = await _find_chapter_by_source_key(session, meta.ref.source_key)
                album_sent, _ = read_outbound_state(current) if current else (False, [])
                if not album_sent:
                    await mark_outbound_sent(session, published.chapter_id, album=True)

    async def _existing_page_count(self, source_key: str) -> int:
        factory = get_session_factory()
        async with factory() as session:
            chapter = await _find_chapter_by_source_key(session, source_key)
            if chapter is None:
                return 0
            return int(chapter.page_count or 0)

    async def _card_fields(self, source_key: str) -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            chapter = await _find_chapter_by_source_key(session, source_key)
            if chapter is None:
                return {"source_key": source_key}
            manga = await session.get(Manga, chapter.manga_id)
            if manga is None:
                return {"source_key": source_key, "page_count": int(chapter.page_count or 0)}
            return {
                "source_key": source_key,
                "manga_id": manga.id,
                "slug": manga.slug,
                "cover_url": manga.cover_url,
                "author": manga.author,
                "chapter_count": manga.chapter_count,
                "page_count": manga.page_count or chapter.page_count,
                "title": manga.title,
            }

    def _remember(
        self,
        url: str,
        *,
        title: str,
        status: str,
        pages: int = 0,
        detail: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "url": url,
            "title": title,
            "status": status,
            "pages": pages,
            "detail": (detail or "")[:200],
            "at": _now(),
        }
        for key, value in (extra or {}).items():
            if value not in (None, ""):
                payload[key] = value
        self.recent.appendleft(payload)
