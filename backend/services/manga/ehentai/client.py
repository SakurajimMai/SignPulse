from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.utils.outbound import httpx_async_client_kwargs

from .parser import (
    GalleryMeta,
    GalleryRef,
    append_nl,
    has_content_warning,
    merge_image_pages,
    parse_full_image_url,
    parse_gallery_page,
    parse_next_image_page,
    parse_nl_token,
    parse_search_results,
)

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
# 509.gif 很小；真正漫画页几乎都大于这个体积
MIN_IMAGE_BYTES = 2048


class EhentaiError(RuntimeError):
    pass


class ShowpageMissing(EhentaiError):
    """单张看图页或原图不存在，跳过这一张即可，不要整本失败。"""


@dataclass
class Showpage:
    image_url: str
    html: str
    page_url: str
    next_url: str | None = None


class EhentaiClient:
    def __init__(
        self,
        *,
        cookie: str = "",
        exhentai: bool = False,
        delay_seconds: float = 1.0,
        http: httpx.AsyncClient | None = None,
    ):
        self.cookie = (cookie or "").strip()
        self.exhentai = bool(exhentai and self.cookie)
        self.delay_seconds = max(float(delay_seconds), 0.0)
        self.base = "https://exhentai.org" if self.exhentai else "https://e-hentai.org"
        self._owns_http = http is None
        headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        self._http = http or httpx.AsyncClient(
            **httpx_async_client_kwargs(
                headers=headers,
                follow_redirects=True,
                timeout=httpx.Timeout(45.0, connect=15.0),
                cookies={"nw": "1"},
            )
        )
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, url: str, *, allow_missing: bool = False, **kwargs: Any) -> httpx.Response:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        async with self._lock:
            response = await self._http.get(url, **kwargs)
        if response.status_code in {401, 403} or "bounce_login" in str(response.url):
            raise EhentaiError("E-Hentai cookie 无效或已过期")
        if allow_missing and response.status_code == 404:
            return response
        response.raise_for_status()
        return response

    def search_url(self, query: str, *, cats: str | int = "704", page: int = 0) -> str:
        params = {
            "f_cats": str(cats or "0"),
            "f_search": query,
            "inline_set": "dm_c",
        }
        if page > 0:
            params["page"] = str(page)
        return f"{self.base}/?{urlencode(params)}"

    async def search(self, query: str, *, cats: str | int = "704", pages: int = 1) -> list[GalleryRef]:
        found: list[GalleryRef] = []
        seen: set[str] = set()
        limit = max(1, min(int(pages or 1), 10))
        for page in range(limit):
            url = self.search_url(query, cats=cats, page=page)
            html_text = (await self._get(url)).text
            batch = parse_search_results(html_text, self.base)
            if not batch:
                break
            for ref in batch:
                if ref.source_key in seen:
                    continue
                seen.add(ref.source_key)
                found.append(ref)
        return found

    async def fetch_gallery(self, ref: GalleryRef) -> GalleryMeta:
        html_text = (await self._get(ref.url)).text
        if has_content_warning(html_text):
            html_text = (await self._get(f"{ref.url}?nw=always")).text
        meta = parse_gallery_page(html_text, ref.url, self.base)
        if meta is None:
            raise EhentaiError(f"无法解析画廊 {ref.url}")
        await self._complete_image_pages(meta)
        return meta

    async def _complete_image_pages(self, meta: GalleryMeta) -> None:
        """首页通常只有约 20 张缩略图；Length 更大时继续翻 ?p=。"""
        needed = int(meta.page_count or 0)
        pager = max(int(meta.result_pages or 1), 1)
        first_batch = len(meta.image_pages)
        if needed and first_batch:
            pager = max(pager, (needed + first_batch - 1) // first_batch)
        elif needed and not first_batch:
            pager = max(pager, 2)
        pager = min(max(pager, 1), 200)
        if pager <= 1:
            return
        if needed and len(meta.image_pages) >= needed:
            return
        for page in range(1, pager):
            if needed and len(meta.image_pages) >= needed:
                return
            more = (await self._get(f"{meta.ref.url}?p={page}")).text
            extra = parse_gallery_page(more, meta.ref.url, self.base)
            if extra is None:
                break
            before = len(meta.image_pages)
            meta.image_pages = merge_image_pages(meta.image_pages, extra.image_pages)
            added = len(meta.image_pages) - before
            if extra.result_pages:
                pager = min(max(pager, int(extra.result_pages)), 200)
            if added == 0:
                break

    async def fetch_showpage(self, page_url: str) -> Showpage | None:
        """拉看图页。HTTP 404 或没有原图时返回 None，由调用方跳过这一张。"""
        response = await self._get(page_url, allow_missing=True)
        if response.status_code == 404:
            logger.warning("E-Hentai 看图页 404，跳过 %s", page_url)
            return None
        html_text = response.text
        image_url = parse_full_image_url(html_text)
        if not image_url:
            logger.warning("E-Hentai 看图页没有原图，跳过 %s", page_url)
            return None
        return Showpage(
            image_url=image_url,
            html=html_text,
            page_url=str(response.url),
            next_url=parse_next_image_page(html_text, self.base),
        )

    async def fetch_image_url(self, page_url: str) -> str:
        page = await self.fetch_showpage(page_url)
        if page is None:
            raise ShowpageMissing(f"看图页不存在 {page_url}")
        return page.image_url

    async def download_image(self, image_url: str, page_url: str | None = None) -> bytes:
        response = await self._get(image_url, allow_missing=True)
        if response.status_code == 404:
            raise ShowpageMissing(f"原图不存在 {image_url}")
        data = response.content
        content_type = (response.headers.get("content-type") or "").lower()
        looks_blocked = (
            len(data) < MIN_IMAGE_BYTES
            or "509.gif" in image_url
            or (content_type.startswith("image/gif") and len(data) < 20_000)
        )
        if looks_blocked and page_url:
            html_text = (await self._get(page_url, allow_missing=True)).text
            token = parse_nl_token(html_text)
            if token:
                retry_page = append_nl(page_url, token)
                retry_html = (await self._get(retry_page, allow_missing=True)).text
                retry_url = parse_full_image_url(retry_html)
                if retry_url:
                    retry = await self._get(retry_url, allow_missing=True)
                    if retry.status_code != 404:
                        data = retry.content
        if len(data) < MIN_IMAGE_BYTES:
            raise EhentaiError("图片过小，可能是 509 限额页")
        return data
