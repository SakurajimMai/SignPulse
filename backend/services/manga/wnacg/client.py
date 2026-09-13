from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from backend.utils.outbound import httpx_async_client_kwargs

from .categories import (
    WnacgCategory,
    album_url,
    gallery_url,
    list_url,
    normalize_base_url,
)
from .parser import (
    AlbumMeta,
    AlbumRef,
    parse_album_page,
    parse_image_urls,
    parse_list_results,
)

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
MIN_IMAGE_BYTES = 2048


class WnacgError(RuntimeError):
    pass


class ImageMissing(WnacgError):
    """单张原图不存在或过小，跳过这一张即可。"""


class WnacgClient:
    def __init__(
        self,
        *,
        base_url: str = "https://www.wnacg.com",
        delay_seconds: float = 1.0,
        http: httpx.AsyncClient | None = None,
    ):
        self.base = normalize_base_url(base_url)
        self.delay_seconds = max(float(delay_seconds), 0.0)
        self._owns_http = http is None
        headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{self.base}/",
        }
        self._http = http or httpx.AsyncClient(
            **httpx_async_client_kwargs(
                headers=headers,
                follow_redirects=True,
                timeout=httpx.Timeout(45.0, connect=15.0),
            )
        )
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, url: str, *, allow_missing: bool = False, **kwargs: Any) -> httpx.Response:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        headers = dict(kwargs.pop("headers", {}) or {})
        async with self._lock:
            response = await self._http.get(url, headers=headers or None, **kwargs)
        if allow_missing and response.status_code == 404:
            return response
        response.raise_for_status()
        return response

    async def list_albums(self, category: WnacgCategory, *, pages: int = 1) -> list[AlbumRef]:
        found: list[AlbumRef] = []
        seen: set[str] = set()
        limit = max(1, min(int(pages or 1), 10))
        for page in range(1, limit + 1):
            url = list_url(category, page, base=self.base)
            html_text = (await self._get(url)).text
            batch = parse_list_results(html_text, self.base)
            if not batch:
                break
            for ref in batch:
                if ref.source_key in seen:
                    continue
                seen.add(ref.source_key)
                found.append(ref)
        return found

    async def fetch_album(self, ref: AlbumRef) -> AlbumMeta:
        index_url = album_url(ref.aid, base=self.base)
        html_text = (await self._get(index_url)).text
        meta = parse_album_page(html_text, index_url, self.base)
        if meta is None:
            raise WnacgError(f"无法解析相册 {index_url}")
        gallery_html = (await self._get(gallery_url(ref.aid, base=self.base))).text
        meta.image_urls = parse_image_urls(gallery_html, self.base)
        if not meta.page_count:
            meta.page_count = len(meta.image_urls)
        if not meta.image_urls:
            raise WnacgError(f"相册没有图片 {index_url}")
        return meta

    async def download_image(self, image_url: str, referer: str | None = None) -> bytes:
        headers = {"Referer": referer or f"{self.base}/"}
        response = await self._get(image_url, allow_missing=True, headers=headers)
        if response.status_code == 404:
            raise ImageMissing(f"原图不存在 {image_url}")
        data = response.content
        if len(data) < MIN_IMAGE_BYTES:
            raise ImageMissing(f"图片过小 {image_url}")
        return data
