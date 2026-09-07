"""把已入库章节转发到运营频道：预览图 + 标题/作者/标签 + 阅读链接。"""

from __future__ import annotations

import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

from backend.utils.outbound import httpx_async_client_kwargs

from .config import MangaSettings
from .publisher import PublishedChapter

logger = logging.getLogger(__name__)

_HASHTAG_KEEP = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_FAILED_MEDIA_INDEX = re.compile(r"failed to send message\s*#(\d+)", re.IGNORECASE)


def plan_outbound_post(
    *,
    is_final: bool,
    album_sent: bool,
    new_video_urls: list[str] | None,
    sent_video_urls: list[str] | None,
) -> tuple[bool, list[str]]:
    """讨论帖补页过程中不发频道；线程收齐后预览图只发一次，视频只补没发过的。"""
    already = set(sent_video_urls or [])
    videos: list[str] = []
    for url in new_video_urls or []:
        item = str(url).strip()
        if not item or item in already:
            continue
        videos.append(item)
        already.add(item)
    if not is_final:
        return False, []
    send_album = not bool(album_sent)
    if not send_album and not videos:
        return False, []
    return send_album, videos


def can_publish_outbound(
    *,
    channel_enabled: bool,
    send_album: bool,
    video_urls: list[str] | None,
    site_enabled: bool,
    has_site_images: bool,
    site_published: bool | None,
) -> bool:
    """TG 转发必须等主站发布成功；没有漫画图的视频可以只发频道。"""
    if not channel_enabled:
        return False
    if not send_album and not video_urls:
        return False
    if site_enabled and has_site_images and site_published is not True:
        return False
    return True


def resolve_site_origin(site_publish_url: str, override: str = "") -> str:
    raw = (override or "").strip().rstrip("/")
    if raw:
        if raw.startswith(("http://", "https://")):
            return raw
        return f"https://{raw}"
    parsed = urlparse((site_publish_url or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def build_reader_url(*, site_origin: str, manga_id: int, number: int) -> str:
    origin = site_origin.rstrip("/")
    if not origin or manga_id <= 0 or number <= 0:
        return ""
    return f"{origin}/manga/{manga_id}/read/{number}"


def _hashtag(tag: str) -> str:
    cleaned = _HASHTAG_KEEP.sub("_", (tag or "").strip()).strip("_")
    return cleaned


def build_read_button(button_text: str, read_url: str) -> dict[str, Any]:
    label = (button_text or "点击阅读").strip() or "点击阅读"
    return {"inline_keyboard": [[{"text": label[:64], "url": read_url}]]}


def build_outbound_caption(
    *,
    title: str,
    author: str | None = None,
    tags: list[str] | None = None,
    read_url: str = "",
    button_text: str = "点击阅读",
    include_link: bool = True,
) -> str:
    heading = html.escape((title or "").strip() or "未命名")
    lines = [f"<b>{heading}</b>"]
    author_text = (author or "").strip()
    author_tag = _hashtag(author_text)
    if author_tag:
        lines.append(f"作者：#{author_tag}")
    elif author_text:
        lines.append(f"作者：{html.escape(author_text)}")
    hashes = []
    for tag in tags or []:
        item = _hashtag(tag)
        if not item or item == author_tag:
            continue
        hashed = f"#{item}"
        if hashed not in hashes:
            hashes.append(hashed)
    if hashes:
        lines.append("标签：" + " ".join(hashes[:20]))
    url = (read_url or "").strip()
    label = html.escape((button_text or "点击阅读").strip() or "点击阅读")
    if include_link and url:
        lines.append("")
        lines.append(f'<a href="{html.escape(url, quote=True)}">{label}</a>')
    caption = "\n".join(lines)
    return caption[:1024]


def parse_tg_media_ref(url: str) -> tuple[int, int] | None:
    raw = str(url or "").strip()
    if not raw.startswith("tg:"):
        return None
    body = raw[3:]
    chat, sep, message = body.rpartition(":")
    if not sep:
        return None
    try:
        return int(chat), int(message)
    except ValueError:
        return None


def preview_image_urls(image_urls: list[str], count: int) -> list[str]:
    limit = max(1, min(10, int(count or 4)))
    seen: set[str] = set()
    picked: list[str] = []
    for url in image_urls:
        cleaned = str(url or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        picked.append(cleaned)
        if len(picked) >= limit:
            break
    return picked


class ChannelPublisher:
    def __init__(self, settings: MangaSettings, client_provider: Any, bot_api: Any | None = None):
        self.settings = settings
        self._client_provider = client_provider
        self._bot_api = bot_api

    @property
    def enabled(self) -> bool:
        return bool(self.settings.outbound_enabled and self.settings.outbound_channel.strip())

    @property
    def uses_bot(self) -> bool:
        return bool((self.settings.outbound_bot_token or "").strip())

    def _client(self) -> Any:
        provider = self._client_provider
        return provider() if callable(provider) else provider

    async def _ensure_outbound_peer(self, chat: str) -> None:
        """Numeric channel ids only resolve after the user client has seen the peer."""
        client = self._client()
        if client is None or not hasattr(client, "get_chat"):
            return
        target: Any = int(chat) if str(chat).lstrip("-").isdigit() else chat
        try:
            await client.get_chat(target)
        except Exception:
            logger.warning("Could not resolve outbound channel peer %s", chat, exc_info=True)

    async def publish_chapter(
        self,
        *,
        title: str,
        author: str | None,
        tags: list[str] | None,
        image_urls: list[str],
        local: PublishedChapter | None,
        video_urls: list[str] | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        if local is None:
            return False
        videos = [str(url).strip() for url in (video_urls or []) if str(url).strip()]
        if not getattr(self.settings, "outbound_forward_videos", True):
            videos = []
        # 预览图只在新章节发一次；后续补视频不再带一套相同的预览图。
        if not local.created and not videos:
            return False
        previews: list[str] = []
        if local.created:
            previews = preview_image_urls(
                [url for url in image_urls if not str(url).startswith("tg:")],
                self.settings.outbound_preview_count,
            )
        if not previews and not videos:
            logger.warning("Outbound channel publish skipped: no preview images or videos")
            return False

        origin = resolve_site_origin(
            self.settings.site_publish_url,
            self.settings.outbound_site_base,
        )
        read_url = build_reader_url(
            site_origin=origin,
            # Local catalog IDs are not guaranteed to match the remote reader.
            manga_id=local.site_manga_id or 0,
            number=local.number,
        )
        use_bot = self.uses_bot
        caption = build_outbound_caption(
            title=title,
            author=author,
            tags=tags,
            read_url=read_url,
            button_text=self.settings.outbound_button_text,
            include_link=True,
        )
        chat = self.settings.outbound_channel.strip()
        if str(chat).lstrip("-").isdigit():
            chat = str(int(chat))
        if not use_bot or any(parse_tg_media_ref(video) is not None for video in videos):
            await self._ensure_outbound_peer(chat)
        via = "bot" if use_bot else "account"
        if previews:
            if use_bot:
                await self._send_via_bot(chat, previews, caption, read_url)
            else:
                client = self._client()
                if client is None:
                    logger.warning("Outbound channel publish skipped: Telegram client is not ready")
                    return False
                await self._send_album(client, chat, previews, caption)
        for index, video in enumerate(videos):
            video_caption = caption if not previews and index == 0 else ""
            await self._send_video(chat, video, video_caption)
        logger.info(
            "Outbound channel post sent via=%s chat=%s manga=%s chapter=%s photos=%d videos=%d",
            via,
            chat,
            local.manga_id,
            local.number,
            len(previews),
            len(videos),
        )
        return True

    async def _send_video(self, chat: str, video: str, caption: str) -> None:
        dest: Any = int(chat) if str(chat).lstrip("-").isdigit() else chat
        ref = parse_tg_media_ref(video)
        client = self._client()
        if ref is not None:
            if client is None:
                raise RuntimeError("Telegram user client is required to copy source videos")
            from_chat, message_id = ref
            if hasattr(client, "copy_message"):
                await client.copy_message(dest, from_chat, message_id)
            else:
                await client.forward_messages(dest, from_chat, message_id)
            return
        if self.uses_bot:
            api = self._bot_api or TelegramBotApi(self.settings.outbound_bot_token)
            payload: dict[str, Any] = {"chat_id": dest, "video": video}
            if caption:
                payload["caption"] = caption
                payload["parse_mode"] = "HTML"
            await api.call("sendVideo", payload)
            return
        if client is None:
            raise RuntimeError("Telegram client is not ready for video send")
        kwargs: dict[str, Any] = {}
        if caption:
            kwargs["caption"] = caption
            kwargs["parse_mode"] = _html_parse_mode()
        await client.send_video(dest, video, **kwargs)

    async def _send_via_bot(
        self,
        chat: str,
        urls: list[str],
        caption: str,
        read_url: str,
    ) -> None:
        api = self._bot_api or TelegramBotApi(self.settings.outbound_bot_token)
        remaining = list(urls)
        # Telegram 相册不能挂 inline 按钮，文案里的「点击阅读」必须和预览图在同一条帖子。
        while len(remaining) > 1:
            media = []
            for index, url in enumerate(remaining):
                item: dict[str, Any] = {"type": "photo", "media": url}
                if index == 0:
                    item["caption"] = caption
                    item["parse_mode"] = "HTML"
                media.append(item)
            try:
                await api.call("sendMediaGroup", {"chat_id": chat, "media": media})
                return
            except RuntimeError as exc:
                failed_index = _webpage_curl_failed_index(exc, len(remaining))
                if failed_index is None:
                    raise
                logger.warning(
                    "Telegram could not fetch outbound preview #%d; retrying with %d photos",
                    failed_index + 1,
                    len(remaining) - 1,
                )
                remaining.pop(failed_index)

        payload: dict[str, Any] = {
            "chat_id": chat,
            "photo": remaining[0],
            "caption": caption,
            "parse_mode": "HTML",
        }
        if read_url:
            payload["reply_markup"] = build_read_button(
                self.settings.outbound_button_text,
                read_url,
            )
        await api.call("sendPhoto", payload)

    async def _send_album(self, client: Any, chat: str, urls: list[str], caption: str) -> None:
        parse_mode = _html_parse_mode()
        if len(urls) == 1:
            await client.send_photo(
                chat,
                urls[0],
                caption=caption,
                parse_mode=parse_mode,
            )
            return
        media = []
        for index, url in enumerate(urls):
            kwargs: dict[str, Any] = {}
            if index == 0:
                kwargs["caption"] = caption
                if parse_mode is not None:
                    kwargs["parse_mode"] = parse_mode
            media.append(_input_media_photo(url, **kwargs))
        await client.send_media_group(chat, media)


def _html_parse_mode() -> Any:
    try:
        from pyrogram.enums import ParseMode

        return ParseMode.HTML
    except Exception:
        return "html"


def _webpage_curl_failed_index(exc: BaseException, media_count: int) -> int | None:
    message = str(exc)
    if "WEBPAGE_CURL_FAILED" not in message.upper():
        return None
    matched = _FAILED_MEDIA_INDEX.search(message)
    if matched is None:
        return None
    index = int(matched.group(1)) - 1
    return index if 0 <= index < media_count else None


def _input_media_photo(url: str, **kwargs: Any) -> Any:
    try:
        from pyrogram.types import InputMediaPhoto

        return InputMediaPhoto(url, **kwargs)
    except Exception:
        return {"media": url, **kwargs}


class TelegramBotApi:
    """Minimal Bot API client. Token is never included in raised messages."""

    def __init__(self, token: str, http: Any | None = None):
        self._token = (token or "").strip()
        self._http = http

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        if not self._token:
            raise RuntimeError("Telegram bot token is empty")
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        if self._http is not None:
            data = await self._http(method, payload)
        else:
            import httpx

            async with httpx.AsyncClient(
                **httpx_async_client_kwargs(
                    timeout=httpx.Timeout(45.0, connect=15.0)
                )
            ) as client:
                try:
                    response = await client.post(url, json=payload)
                    data = response.json()
                except Exception:
                    raise RuntimeError(
                        f"Telegram bot {method} request failed"
                    ) from None
        if not isinstance(data, dict) or not data.get("ok"):
            description = ""
            if isinstance(data, dict):
                description = str(data.get("description") or "")[:200]
            raise RuntimeError(f"Telegram bot {method} failed{': ' + description if description else ''}")
        return data.get("result")
