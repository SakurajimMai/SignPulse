from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from backend.services.alerts import schedule_alert
from backend.services.manga.channel_publish import (
    ChannelPublisher,
    can_publish_outbound,
)
from backend.services.manga.config import MangaSettings
from backend.services.manga.db import Chapter, Manga, get_session_factory
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

from .client import EhentaiClient, EhentaiError, ShowpageMissing
from .parser import GalleryMeta, parse_gallery_ref, prefer_showpage_url, split_searches
from .tags import display_tags

logger = logging.getLogger(__name__)

FLUSH_EVERY = 10
RECENT_LIMIT = 40


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public_error(exc: BaseException) -> str:
    """面板上不要甩 httpx 的 MDN 长尾。"""
    if isinstance(exc, httpx.HTTPStatusError):
        url = str(exc.request.url) if exc.request is not None else ""
        code = exc.response.status_code if exc.response is not None else "?"
        return f"E-Hentai 请求失败 HTTP {code} {url}".strip()[:300]
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
    return max(float(settings.ehentai_poll_seconds or 300.0), 60.0)


class EhentaiWorker:
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
        self._http: EhentaiClient | None = None

    def request_pass(self) -> None:
        """打断轮询等待，马上再搜一轮新本。"""
        self._wake.set()

    def status(self) -> dict[str, Any]:
        settings = self.settings
        running = not self._stopped.is_set() and not self._closing
        return {
            "worker_status": "running" if running else "stopped",
            "last_error": self.last_error,
            "cookie_configured": bool((settings.ehentai_cookie or "").strip()),
            "exhentai": bool(settings.ehentai_exhentai),
            "search_count": len(split_searches(settings.ehentai_search)),
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
        cookie = (settings.ehentai_cookie or "").strip()
        if not cookie and settings.ehentai_exhentai:
            raise EhentaiError("ExHentai 需要填写 cookie")
        if not settings.cfbed_upload_url:
            raise EhentaiError("请先配置图床，E-Hentai 图片要先上传再发主站")
        queries = split_searches(settings.ehentai_search)
        if not queries:
            raise EhentaiError("请至少填写一条搜索词，例如：female:NTR language:Chinese")
        self._http = EhentaiClient(
            cookie=cookie,
            exhentai=bool(settings.ehentai_exhentai),
            delay_seconds=float(settings.ehentai_delay_seconds or 1.0),
        )
        while not self._stopped.is_set():
            self.phase = "searching"
            self.next_pass_at = None
            try:
                await self._run_pass(queries)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = _public_error(exc)
                logger.exception("E-Hentai 采集一轮失败")
                schedule_alert(
                    "manga_ehentai_gallery_fail",
                    title="E-Hentai 采集一轮失败",
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

    async def _ensure_http(self) -> EhentaiClient:
        if self._http is not None:
            return self._http
        settings = self.settings
        cookie = (settings.ehentai_cookie or "").strip()
        if not cookie and settings.ehentai_exhentai:
            raise EhentaiError("ExHentai 需要填写 cookie")
        self._http = EhentaiClient(
            cookie=cookie,
            exhentai=bool(settings.ehentai_exhentai),
            delay_seconds=float(settings.ehentai_delay_seconds or 1.0),
        )
        return self._http

    async def list_source_keys(self) -> list[str]:
        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    select(Chapter.source_key).where(Chapter.source_key.like("eh:%"))
                )
            ).scalars().all()
        return [str(key) for key in rows if key]

    async def backfill_incomplete(self, source_keys: list[str] | None = None) -> dict[str, Any]:
        """把已入库但被首页 20 张截断的画廊从断页续传到齐，并重发主站。"""
        await self._ensure_http()
        keys = source_keys if source_keys is not None else await self.list_source_keys()
        report: dict[str, Any] = {
            "checked": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        }
        base = self._http.base if self._http is not None else "https://e-hentai.org"
        for key in keys:
            if self._stopped.is_set():
                break
            ref = parse_gallery_ref(key, base)
            if ref is None:
                report["failed"] += 1
                report["items"].append({"source_key": key, "status": "invalid"})
                continue
            before = await self._existing_page_count(key)
            report["checked"] += 1
            try:
                await self._ingest_gallery(ref)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report["failed"] += 1
                report["items"].append(
                    {"source_key": key, "status": "failed", "before": before, "detail": _public_error(exc)[:200]}
                )
                logger.exception("E-Hentai 补全失败 %s", key)
                continue
            after = await self._existing_page_count(key)
            status = "updated" if after > before else "skipped"
            report[status] += 1
            report["items"].append(
                {"source_key": key, "status": status, "before": before, "after": after}
            )
            logger.info("E-Hentai 补全 %s %s -> %s", key, before, after)
        return report

    async def _run_pass(self, queries: list[str]) -> None:
        assert self._http is not None
        self.last_error = None
        settings = self.settings
        cats = settings.ehentai_cats or "704"
        pages = max(1, min(int(settings.ehentai_search_pages or 1), 10))
        gallery_delay = max(float(settings.ehentai_gallery_delay_seconds or 3.0), 0.0)
        for query in queries:
            if self._stopped.is_set():
                return
            logger.info("E-Hentai 搜索 query=%s pages=%s", query, pages)
            refs = await self._http.search(query, cats=cats, pages=pages)
            for ref in refs:
                if self._stopped.is_set():
                    return
                existing = await self._existing_page_count(ref.source_key)
                if existing:
                    self.stats["skipped"] += 1
                    extra = await self._card_fields(ref.source_key)
                    self._remember(
                        ref.url,
                        title=str(extra.get("title") or ref.source_key),
                        status="duplicate",
                        pages=int(extra.get("page_count") or existing),
                        extra=extra,
                    )
                    continue
                try:
                    await self._ingest_gallery(ref)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.stats["failed"] += 1
                    detail = _public_error(exc)
                    self.last_error = detail
                    extra = await self._card_fields(ref.source_key)
                    self._remember(
                        ref.url,
                        title=str(extra.get("title") or ref.source_key),
                        status="failed",
                        detail=detail,
                        extra=extra,
                    )
                    logger.exception("E-Hentai 画廊失败 %s", ref.url)
                    rule_id = "manga_ehentai_gallery_fail"
                    alert_title = "E-Hentai 画廊失败"
                    if isinstance(exc, _RoutedGalleryError):
                        rule_id = exc.rule_id
                        alert_title = exc.alert_title
                    schedule_alert(
                        rule_id,
                        title=f"{alert_title}：{extra.get('title') or ref.source_key}",
                        detail=f"{ref.url}\n{detail}",
                        fingerprint=str(ref.source_key or ref.url),
                    )
                if gallery_delay:
                    try:
                        await asyncio.wait_for(self._stopped.wait(), timeout=gallery_delay)
                    except asyncio.TimeoutError:
                        pass

    async def _ingest_gallery(self, ref) -> None:
        assert self._http is not None
        settings = self.settings
        existing_pages = await self._existing_page_count(ref.source_key)
        max_pages = max(int(settings.ehentai_max_pages or 400), 1)
        meta = await self._http.fetch_gallery(ref)
        title = meta.title
        self.current = {
            "title": title,
            "url": ref.url,
            "pages": existing_pages,
            "total": meta.page_count or len(meta.image_pages),
        }
        expected = meta.page_count or len(meta.image_pages)
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
        image_pages = meta.image_pages
        if existing_pages:
            image_pages = image_pages[existing_pages:]
        if not image_pages:
            self.stats["skipped"] += 1
            self._remember(ref.url, title=title, status="empty")
            return

        tags = display_tags(meta.raw_tags)
        published: PublishedChapter | None = None
        site_urls: list[str] = []
        batch: list[str] = []
        stored = existing_pages
        skipped_pages = 0
        prev_next: str | None = None

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
                    source_chat_id="ehentai",
                    source_chat_title="E-Hentai",
                    author=meta.artist,
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
                        source_chat_id="ehentai",
                        source_chat_title="E-Hentai",
                        author=meta.artist,
                        tags=tags,
                        image_urls=site_urls,
                        local=published,
                    )
                    if body is None:
                        raise _RoutedGalleryError(
                            "manga_site_fail",
                            "E-Hentai 主站发布失败",
                            "站点返回空响应",
                        )
                except asyncio.CancelledError:
                    raise
                except _RoutedGalleryError:
                    raise
                except Exception as exc:
                    raise _RoutedGalleryError(
                        "manga_site_fail", "E-Hentai 主站发布失败", exc
                    ) from exc

        for page_url in image_pages:
            if self._stopped.is_set():
                await flush()
                return
            candidate = prefer_showpage_url(page_url, prev_next)
            showpage = await self._http.fetch_showpage(candidate)
            if showpage is None and candidate != page_url:
                showpage = await self._http.fetch_showpage(page_url)
            if showpage is None:
                skipped_pages += 1
                prev_next = None
                logger.warning("E-Hentai 跳过失效看图页 %s", page_url)
                continue
            try:
                data = await self._http.download_image(showpage.image_url, showpage.page_url)
            except ShowpageMissing:
                skipped_pages += 1
                prev_next = showpage.next_url
                logger.warning("E-Hentai 跳过失效原图 %s", showpage.image_url)
                continue
            except httpx.HTTPStatusError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                skipped_pages += 1
                prev_next = showpage.next_url
                logger.warning("E-Hentai 跳过 404 原图 %s", showpage.image_url)
                continue
            stored += 1
            try:
                uploaded = await self.imgbed.upload_bytes(
                    data, filename=f"eh{ref.gid}-{stored:03d}.jpg"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _RoutedGalleryError(
                    "manga_imgbed_upload_fail",
                    "E-Hentai 图床上传失败",
                    exc,
                ) from exc
            batch.append(uploaded)
            self.current["pages"] = stored
            prev_next = showpage.next_url
            if len(batch) >= FLUSH_EVERY:
                await flush()

        await flush()
        if published is None:
            self.stats["failed"] += 1
            detail = "没有写入页"
            if skipped_pages:
                detail = f"看图页失效 {skipped_pages} 张，没有写入页"
            self._remember(ref.url, title=title, status="failed", detail=detail)
            schedule_alert(
                "manga_ehentai_gallery_fail",
                title=f"E-Hentai 画廊没有写入页：{title or ref.source_key}",
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
        extra.setdefault("author", meta.artist)
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
        logger.info("E-Hentai 入库 %s pages=%s", title, published.page_count)

    async def _outbound(
        self,
        meta: GalleryMeta,
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
                    source_chat_id="ehentai",
                    source_chat_title="E-Hentai",
                    author=meta.artist,
                    tags=display_tags(meta.raw_tags),
                    image_urls=site_urls,
                    local=published,
                )
                if body is None:
                    raise _RoutedGalleryError(
                        "manga_site_fail",
                        "E-Hentai 主站发布失败",
                        "站点返回空响应",
                    )
            except asyncio.CancelledError:
                raise
            except _RoutedGalleryError:
                raise
            except Exception as exc:
                raise _RoutedGalleryError(
                    "manga_site_fail", "E-Hentai 主站发布失败", exc
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
                author=meta.artist,
                tags=display_tags(meta.raw_tags),
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
                    "E-Hentai 出站频道发布失败",
                    "频道发布返回失败",
                )
        except asyncio.CancelledError:
            raise
        except _RoutedGalleryError:
            raise
        except Exception as exc:
            raise _RoutedGalleryError(
                "manga_channel_publish_fail", "E-Hentai 出站频道发布失败", exc
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
