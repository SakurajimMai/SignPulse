from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.utils.http_error_text import CF_STATUS_PHRASE, looks_like_html
from backend.utils.outbound import httpx_async_client_kwargs

from .config import MangaSettings
from .filters import clean_display_title
from .publisher import PublishedChapter

logger = logging.getLogger(__name__)


class SitePublishError(RuntimeError):
    pass


_SUCCESS_STATUSES = frozenset({"ok", "success", "created", "updated", "duplicate"})
_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})


def _failure_message(resp: Any, reason: str) -> str:
    status_code = int(getattr(resp, "status_code", 0) or 0)
    response_text = str(getattr(resp, "text", "") or "")
    if status_code in CF_STATUS_PHRASE:
        return (
            f"Site publish failed HTTP {status_code}: "
            f"{CF_STATUS_PHRASE[status_code]}"
        )
    if looks_like_html(response_text):
        for cloudflare_status, phrase in CF_STATUS_PHRASE.items():
            if str(cloudflare_status) in response_text[:4096]:
                return f"Site publish failed HTTP {cloudflare_status}: {phrase}"
        return f"Site publish failed HTTP {status_code}: invalid HTML response"
    return f"Site publish failed HTTP {status_code}: {reason}"


def _is_success_response(body: Any) -> bool:
    if not isinstance(body, dict) or not body:
        return False
    candidates = [body]
    if isinstance(body.get("data"), dict):
        candidates.append(body["data"])
    for item in candidates:
        status = str(item.get("status") or "").strip().lower()
        if (
            item.get("ok") is False
            or item.get("success") is False
            or status in _FAILURE_STATUSES
            or bool(item.get("error"))
            or bool(item.get("errors"))
        ):
            return False
    return any(
        item.get("ok") is True
        or item.get("success") is True
        or str(item.get("status") or "").strip().lower() in _SUCCESS_STATUSES
        for item in candidates
    )


class SitePublisher:
    """Push a published chapter to AnimeStream (hentaiworkers) using the admin-configured key."""

    def __init__(self, settings: MangaSettings):
        self.settings = settings
        self._client = httpx.AsyncClient(
            **httpx_async_client_kwargs(
                timeout=httpx.Timeout(60.0, connect=20.0)
            )
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.site_publish_url and self.settings.site_publish_secret)

    async def publish_chapter(
        self,
        *,
        title: str,
        chapter_title: str | None,
        source_key: str,
        source_chat_id: str | None,
        source_chat_title: str | None,
        image_urls: list[str],
        author: str | None = None,
        tags: list[str] | None = None,
        local: PublishedChapter | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            logger.debug("Site publish skipped (SITE_PUBLISH_URL / SITE_PUBLISH_SECRET unset)")
            return None

        pages = [
            url
            for url in image_urls
            if url and not str(url).startswith("tg:") and not _looks_like_video(url)
        ]
        if not pages:
            logger.info("Site publish skipped: no manga images (videos stay on the channel only)")
            return None

        payload = {
            "title": clean_display_title(title) or title,
            "chapterTitle": clean_display_title(chapter_title) or chapter_title,
            "sourceKey": source_key,
            "sourceChatId": source_chat_id,
            "sourceChatTitle": source_chat_title,
            "author": author,
            "tags": tags or [],
            "imageUrls": pages,
            "coverUrl": pages[0],
        }
        headers = {
            "Content-Type": "application/json",
            "X-Manga-Publish-Key": self.settings.site_publish_secret,
            "Authorization": f"Bearer {self.settings.site_publish_secret}",
        }
        url = self.settings.site_publish_url
        logger.info("Publishing chapter to site %s (pages=%d)", url, len(pages))
        resp = await self._client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise SitePublishError(_failure_message(resp, "upstream request rejected"))
        try:
            body = resp.json()
        except Exception as exc:
            raise SitePublishError(_failure_message(resp, "invalid JSON response")) from exc
        if not _is_success_response(body):
            raise SitePublishError(
                _failure_message(resp, "response did not confirm success")
            )
        logger.info(
            "Site publish ok: %s",
            body,
        )
        return body


def _looks_like_video(url: str) -> bool:
    lowered = str(url or "").lower()
    return lowered.endswith((".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"))
