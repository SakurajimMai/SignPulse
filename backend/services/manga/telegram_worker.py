from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.core.config import get_settings as get_panel_settings
from backend.services.alerts import schedule_alert
from backend.services.config import get_config_service
from backend.services.telegram.credentials import resolve_telegram_api_credentials
from backend.utils.account_locks import get_account_lock
from backend.utils.proxy import build_proxy_dict
from backend.utils.tg_session import (
    get_session_mode,
    load_account_session_string,
    resolve_effective_proxy,
)
from tg_signer.compat import EditedMessageHandler, MessageHandler, filters
from tg_signer.core.client import _CLIENT_INSTANCES, close_client_by_name, get_client

from .assembler import (
    REPLY_IDLE_FLOOR_SECONDS,
    ChapterAssembler,
    PendingChapter,
    PendingPage,
    bucket_idle_seconds,
    collapse_reply_root,
)
from .channel_publish import ChannelPublisher, can_publish_outbound, plan_outbound_post
from .config import MangaSettings
from .db import Chapter, Page, get_session_factory
from .filters import (
    caption_page_hint,
    document_filename,
    grouped_id,
    is_image_message,
    is_video_message,
    merge_reader_title,
    parse_caption,
    parse_filter_keywords,
    reply_to_id,
    should_accept_outbound_video,
    should_skip_user_comment,
    video_duration_seconds,
)
from .imgbed import ImgBedClient
from .peers import same_telegram_peer, telegram_raw_peer_id
from .publisher import (
    PublishedChapter,
    StoredOutbound,
    _find_chapter_by_source_key,
    list_chapter_image_urls,
    list_chapter_videos,
    list_pending_reply_outbound,
    load_stored_outbound,
    mark_outbound_failed,
    mark_outbound_sent,
    mark_site_publish_succeeded,
    publish_chapter,
    read_outbound_state,
    update_published_titles,
)
from .site_publish import SitePublisher
from .sources import SourceBinding
from .telegraph import (
    TelegraphClient,
    chapter_images_usable,
    decode_telegraph_page_message_id,
    encode_telegraph_page_message_id,
    inspect_image_file,
    is_telegraph_url,
    reader_source_identity,
    resolve_reader_urls_from_message,
)

TELEGRAPH_CATCHUP_LIMIT = 200
TELEGRAPH_CATCHUP_INTERVAL_SECONDS = 300.0
# 只续传最近中断的章节，避免启动回扫把旧 Telegraph 半成品全部重拉一遍。
RESUME_MAX_AGE_SECONDS = 6 * 3600
# 讨论帖 checkpoint 后空桶若没发出 is_final，启动/定时补发运营频道。
STALE_OUTBOUND_INTERVAL_SECONDS = 120.0

logger = logging.getLogger(__name__)


@dataclass
class ResolvedBinding:
    channel_id: int | None
    channel_title: str | None
    discussion_id: int | None
    discussion_title: str | None
    ingest_mode: str = "auto"
    configured_discussion: str = ""
    forward_videos: bool = True
    ingest_enabled: bool = True

    def source_id(self, fallback: int) -> int:
        # Keep the Bot API marked id (-100...) so existing source_key rows match.
        return self.channel_id if self.channel_id is not None else fallback

    def source_title(self, fallback: str | None) -> str | None:
        return self.channel_title or fallback

    def is_telegraph(self) -> bool:
        return SourceBinding(
            channel=str(self.channel_id or ""),
            discussion=self.configured_discussion,
            ingest_mode=self.ingest_mode,
        ).is_telegraph()

    def wants_history_backfill(self) -> bool:
        return SourceBinding(
            channel=str(self.channel_id or ""),
            discussion=self.configured_discussion,
            ingest_mode=self.ingest_mode,
        ).wants_history_backfill()


def cleanup_temp_dir(temp_dir: Path) -> tuple[int, int]:
    """Remove files left by interrupted downloads from the worker temp area."""
    if not temp_dir.exists():
        return 0, 0

    removed_files = 0
    removed_bytes = 0
    for path in temp_dir.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        try:
            if path.is_file():
                removed_bytes += path.stat().st_size
            path.unlink()
            removed_files += 1
        except OSError:
            logger.warning("Could not remove stale temp file %s", path, exc_info=True)

    for path in sorted(temp_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_dir():
            continue
        try:
            path.rmdir()
        except OSError:
            # A directory can be recreated or populated while the worker starts.
            pass
    return removed_files, removed_bytes


def _chat_type_name(entity: object) -> str:
    raw = getattr(entity, "type", None)
    if raw is None:
        return ""
    return str(getattr(raw, "name", raw)).lower()


def _is_discussion_entity(entity: object) -> bool:
    name = _chat_type_name(entity)
    return "supergroup" in name or name.endswith("group")


def _is_broadcast_entity(entity: object) -> bool:
    return "channel" in _chat_type_name(entity)


def _chat_id_from_source_key(source_key: str, source_chat_id: str | None = None) -> int:
    for raw in (source_chat_id, str(source_key or "").split(":", 1)[0]):
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return 0


class TelegramMangaWorker:
    def __init__(self, settings: MangaSettings):
        self.settings = settings
        self.client: Any | None = None
        self.imgbed = ImgBedClient(settings)
        self.site = SitePublisher(settings)
        self.telegraph = TelegraphClient()
        self.channel = ChannelPublisher(settings, lambda: self.client)
        self.assembler = ChapterAssembler(settings, self._on_chapter_ready)
        self._resolved_chats: list[Any] = []
        self._resolved_chat_ids: list[int] = []
        self._bindings_by_chat: dict[int, ResolvedBinding] = {}
        self._discussion_only = False
        self._reply_caption_cache: dict[tuple[int, int], str | None] = {}
        self._handler_refs: list[Any] = []
        self._owns_client = False
        self._account_name = ""
        self._session_dir: Path | None = None
        self._stopped = asyncio.Event()
        self._closing = False
        self._backfill_task: asyncio.Task | None = None
        self._telegraph_catchup_task: asyncio.Task | None = None
        self._stale_outbound_task: asyncio.Task | None = None
        self._reply_parent: dict[tuple[int, int], int] = {}
        self._page_to_root: dict[int, int] = {}
        self._known_roots: set[int] = set()
        self._outbound_locks: dict[str, asyncio.Lock] = {}
        self._telegraph_ingest_locks: dict[str, asyncio.Lock] = {}
        self._telegraph_catchup_lock = asyncio.Lock()

    async def start(self) -> None:
        self.settings.ensure_dirs()
        removed_files, removed_bytes = cleanup_temp_dir(Path(self.settings.temp_dir))
        if removed_files:
            logger.info(
                "Removed %d stale temp files (%d bytes)",
                removed_files,
                removed_bytes,
            )
        await self._connect_account()
        await self._load_thread_index()

        bindings = self.settings.binding_list
        if not bindings:
            raise RuntimeError(
                "至少配置一组「采集频道 + 讨论群」；拒绝监听全部 Telegram 对话"
            )
        if not any(item.ingest_enabled for item in bindings):
            raise RuntimeError("所有采集频道都已勾选「跳过采集」")

        for binding in bindings:
            if not binding.ingest_enabled:
                logger.info(
                    "Skip ingest for channel=%s discussion=%s",
                    binding.channel or "-",
                    binding.discussion or "-",
                )
                continue
            resolved = await self._resolve_binding(binding)
            if resolved is None:
                continue
            for chat_id in (resolved.channel_id, resolved.discussion_id):
                self._index_binding(chat_id, resolved)
            listen_ids: list[int] = []
            if resolved.is_telegraph():
                if resolved.channel_id is not None:
                    listen_ids.append(resolved.channel_id)
            else:
                if resolved.discussion_id is not None:
                    listen_ids.append(resolved.discussion_id)
                if resolved.channel_id is not None:
                    # discussion_only 仍监听频道：只收视频转发到运营频道，图片继续走讨论区
                    listen_ids.append(resolved.channel_id)
            for chat_id in listen_ids:
                entity = await self._resolve_entity(str(chat_id))
                if entity is None:
                    continue
                entity_id = getattr(entity, "id", None)
                if entity_id is None:
                    continue
                if all(
                    getattr(existing, "id", None) != entity_id
                    for existing in self._resolved_chats
                ):
                    self._resolved_chats.append(entity)
                    self._resolved_chat_ids.append(int(entity_id))
                logger.info(
                    "Watching %s (%s) as %s",
                    getattr(entity, "title", chat_id),
                    entity_id,
                    "telegraph-channel"
                    if resolved.is_telegraph()
                    else "discussion"
                    if same_telegram_peer(entity_id, resolved.discussion_id)
                    else "channel",
                )

        if not self._resolved_chats:
            raise RuntimeError("配置的采集频道 / 讨论群都无法解析")

        self._discussion_only = self.settings.discussion_only and any(
            binding.discussion_id is not None
            for binding in self._bindings_by_chat.values()
        )

        assert self.client is not None
        chat_filter = filters.chat(self._resolved_chat_ids)
        self._handler_refs.append(
            self.client.add_handler(MessageHandler(self._on_message, chat_filter))
        )
        self._handler_refs.append(
            self.client.add_handler(EditedMessageHandler(self._on_message, chat_filter))
        )
        self.assembler.start()
        if self.settings.history_backfill_limit > 0:
            self._backfill_task = asyncio.create_task(
                self._backfill_history(),
                name="manga-history-backfill",
            )
        self._telegraph_catchup_task = asyncio.create_task(
            self._telegraph_catchup_loop(),
            name="manga-telegraph-catchup",
        )
        self._stale_outbound_task = asyncio.create_task(
            self._stale_outbound_loop(),
            name="manga-stale-outbound",
        )
        logger.info(
            "Telegram manga worker started (account=%s chats=%d idle=%ss "
            "discussion_only=%s ignore_user_comments=%s backfill=%d)",
            self._account_name,
            len(self._resolved_chats),
            self.settings.chapter_idle_seconds,
            self._discussion_only,
            self.settings.ignore_user_comments,
            self.settings.history_backfill_limit,
        )

    async def wait(self) -> None:
        await self._stopped.wait()

    async def run_until_disconnected(self) -> None:
        try:
            await self.start()
            await self.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stopped.set()
        for attr in ("_backfill_task", "_telegraph_catchup_task", "_stale_outbound_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.assembler.stop()
        await self.imgbed.aclose()
        await self.site.aclose()
        await self.telegraph.aclose()
        if self.client is not None:
            for handler_ref in self._handler_refs:
                try:
                    self.client.remove_handler(*handler_ref)
                except Exception:
                    logger.debug("Could not remove manga message handler", exc_info=True)
        self._handler_refs = []
        if self._owns_client and self.client is not None:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception:
                logger.debug("Could not close manga Telegram client", exc_info=True)
            self._owns_client = False

    async def _connect_account(self) -> None:
        account = (self.settings.telegram_account_name or "").strip()
        if not account:
            raise RuntimeError(
                "请在漫画设置中选择 /accounts 里已登录的采集账号"
            )
        panel = get_panel_settings()
        session_dir = panel.resolve_session_dir()
        session_mode = get_session_mode()
        session_string = load_account_session_string(
            account, session_dir=session_dir, session_mode=session_mode
        )
        in_memory = False
        if session_mode == "string":
            in_memory = bool(session_string)
            if not session_string:
                raise RuntimeError(
                    f"账号 {account} 没有可用 session_string，请到 /accounts 重新登录"
                )

        proxy_value = resolve_effective_proxy(account)
        if not proxy_value:
            proxy_value = get_config_service().get_global_proxy()
        proxy = build_proxy_dict(proxy_value) if proxy_value else None
        api_id, api_hash = self._resolve_api_credentials()

        lock = get_account_lock(account)
        async with lock:
            client_key = str(session_dir.joinpath(account).resolve())
            existing = _CLIENT_INSTANCES.get(client_key)
            if (
                existing is not None
                and getattr(existing, "_tg_signpulse_no_updates", None) is True
            ):
                logger.info(
                    "Recreating manga ingest client for %s with updates enabled",
                    account,
                )
                await close_client_by_name(account, workdir=session_dir)

            client = get_client(
                account,
                proxy=proxy,
                workdir=session_dir,
                session_string=session_string,
                in_memory=in_memory,
                api_id=api_id,
                api_hash=api_hash,
                no_updates=False,
            )
            await client.__aenter__()
            self.client = client
            self._owns_client = True
            self._account_name = account
            self._session_dir = session_dir

        me = await client.get_me()
        if me is None:
            raise RuntimeError(
                f"账号 {account} 未登录或会话已失效，请到 /accounts 重新登录"
            )
        logger.info(
            "Manga ingest using account %s as %s (id=%s)",
            account,
            getattr(me, "username", None) or getattr(me, "first_name", None) or me.id,
            me.id,
        )

    @staticmethod
    def _root_from_source_key(source_key: str | None) -> int | None:
        if not source_key or ":r:" not in source_key:
            return None
        try:
            return int(str(source_key).rsplit(":r:", 1)[-1])
        except (TypeError, ValueError):
            return None

    async def _load_thread_index(self) -> None:
        factory = get_session_factory()
        async with factory() as session:
            chapter_keys = (
                await session.execute(select(Chapter.source_key))
            ).all()
            page_rows = (
                await session.execute(
                    select(Page.tg_message_id, Chapter.source_key)
                    .join(Chapter, Page.chapter_id == Chapter.id)
                    .where(Page.tg_message_id.is_not(None))
                )
            ).all()
        for (key,) in chapter_keys:
            root = self._root_from_source_key(key)
            if root is not None:
                self._known_roots.add(root)
        for mid, key in page_rows:
            root = self._root_from_source_key(key)
            if mid is None or root is None:
                continue
            self._page_to_root[int(mid)] = root
        logger.info(
            "Manga thread index roots=%d pages=%d",
            len(self._known_roots),
            len(self._page_to_root),
        )

    def _remember_reply(self, chat_id: int, message_id: int, reply_id: int | None) -> None:
        if reply_id is None:
            return
        self._reply_parent[(int(chat_id), int(message_id))] = int(reply_id)

    def _resolve_thread_root(self, chat_id: int, reply_id: int | None) -> int | None:
        if reply_id is None:
            return None
        parent_of = {
            mid: parent
            for (cid, mid), parent in self._reply_parent.items()
            if cid == int(chat_id)
        }
        return collapse_reply_root(
            int(reply_id),
            parent_of=parent_of,
            known_roots=self._known_roots,
            page_to_root=self._page_to_root,
        )

    def _index_published_thread(self, source_key: str, message_ids: list[int]) -> None:
        root = self._root_from_source_key(source_key)
        if root is None:
            return
        self._known_roots.add(root)
        for mid in message_ids:
            self._page_to_root[int(mid)] = root

    def _resolve_api_credentials(self) -> tuple[int | None, str | None]:
        if self.settings.telegram_api_id and self.settings.telegram_api_hash:
            return self.settings.telegram_api_id, self.settings.telegram_api_hash
        try:
            tg_config = get_config_service().get_telegram_config()
            return resolve_telegram_api_credentials(
                tg_config,
                env_api_id=(
                    os.getenv("TG_API_ID")
                    or os.getenv("MANGA_TELEGRAM_API_ID")
                    or os.getenv("TELEGRAM_API_ID")
                ),
                env_api_hash=(
                    os.getenv("TG_API_HASH")
                    or os.getenv("MANGA_TELEGRAM_API_HASH")
                    or os.getenv("TELEGRAM_API_HASH")
                ),
            )
        except ValueError:
            return self.settings.telegram_api_id, self.settings.telegram_api_hash or None

    def _index_binding(self, chat_id: int | None, resolved: ResolvedBinding) -> None:
        raw = telegram_raw_peer_id(chat_id)
        if raw is None:
            return
        self._bindings_by_chat[raw] = resolved
        self._bindings_by_chat[-int(f"100{raw}")] = resolved
        if chat_id is not None:
            self._bindings_by_chat[int(chat_id)] = resolved

    def _binding_for(self, chat_id: int | None) -> ResolvedBinding | None:
        if chat_id is None:
            return None
        found = self._bindings_by_chat.get(int(chat_id))
        if found is not None:
            return found
        raw = telegram_raw_peer_id(chat_id)
        return self._bindings_by_chat.get(raw) if raw is not None else None

    @staticmethod
    def _parse_chat_ref(ref: str):
        ref = ref.strip()
        if ref.startswith("https://t.me/") or ref.startswith("http://t.me/"):
            return ref
        if ref.startswith("@"):
            return ref
        try:
            return int(ref)
        except ValueError:
            return ref

    @staticmethod
    def _entity_id(entity: object) -> int | None:
        raw = getattr(entity, "id", None)
        return int(raw) if raw is not None else None

    async def _resolve_linked_discussion(self, entity: object) -> object | None:
        linked = getattr(entity, "linked_chat", None)
        if linked is not None and getattr(linked, "id", None) is not None:
            return linked
        entity_id = self._entity_id(entity)
        if entity_id is None or self.client is None:
            return None
        try:
            full = await self.client.get_chat(entity_id)
            linked = getattr(full, "linked_chat", None)
            if linked is not None and getattr(linked, "id", None) is not None:
                return linked
        except Exception:
            logger.debug(
                "Could not resolve linked discussion for %s",
                entity_id,
                exc_info=True,
            )
        return None

    async def _resolve_entity(self, ref: str):
        if not ref or self.client is None:
            return None
        try:
            return await self.client.get_chat(self._parse_chat_ref(ref))
        except Exception:
            logger.exception("Failed to resolve chat: %s", ref)
            return None

    async def _resolve_binding(self, binding: SourceBinding) -> ResolvedBinding | None:
        channel = await self._resolve_entity(binding.channel)
        discussion = await self._resolve_entity(binding.discussion)
        telegraph_mode = binding.is_telegraph()
        if not telegraph_mode:
            if channel is not None and _is_discussion_entity(channel) and discussion is None:
                discussion = channel
                channel = await self._resolve_linked_discussion(discussion)
            if discussion is None and channel is not None:
                discussion = await self._resolve_linked_discussion(channel)
        if channel is None and discussion is None:
            return None
        resolved = ResolvedBinding(
            channel_id=self._entity_id(channel),
            channel_title=getattr(channel, "title", None) if channel is not None else None,
            discussion_id=None if telegraph_mode else self._entity_id(discussion),
            discussion_title=(
                None
                if telegraph_mode
                else (getattr(discussion, "title", None) if discussion is not None else None)
            ),
            ingest_mode=binding.resolved_ingest_mode(),
            configured_discussion=binding.discussion,
            ingest_enabled=binding.ingest_enabled,
            forward_videos=binding.forward_videos,
        )
        logger.info(
            "Bound source channel=%s (%s) discussion=%s (%s) mode=%s",
            resolved.channel_title or binding.channel or "-",
            resolved.channel_id,
            resolved.discussion_title or binding.discussion or "-",
            resolved.discussion_id,
            resolved.ingest_mode,
        )
        return resolved

    async def _on_message(self, client: Any, message: Any) -> None:
        del client
        await self._ingest_message(message)

    async def _ingest_message(self, message: Any) -> None:
        if getattr(message, "outgoing", False):
            return
        chat = getattr(message, "chat", None)
        chat_id = int(getattr(chat, "id", 0) or getattr(message, "chat_id", 0) or 0)
        if chat_id and same_telegram_peer(chat_id, self.settings.outbound_channel):
            return
        binding = self._binding_for(chat_id)
        if binding and binding.is_telegraph():
            await self._ingest_telegraph_post(message, chat_id, binding)
            return
        page = await self._materialize_page(message)
        if page is None:
            return
        chat = getattr(message, "chat", None)
        chat_id = int(getattr(chat, "id", 0) or 0)
        chat_title = getattr(chat, "title", None)
        binding = self._binding_for(chat_id)
        if binding:
            chat_title = binding.source_title(chat_title)
        await self.assembler.add_page(chat_id, chat_title, page)
        logger.info(
            "Buffered page chat=%s msg=%s group=%s reply=%s",
            chat_id,
            page.message_id,
            page.grouped_id,
            page.reply_to_msg_id,
        )

    @staticmethod
    def _telegraph_source_key(chat_id: int, page_url: str) -> str:
        identity = reader_source_identity(page_url) or page_url
        return f"{chat_id}:tgph:{identity}"

    async def _source_key_state(self, source_key: str) -> tuple[int, datetime | None]:
        factory = get_session_factory()
        async with factory() as session:
            chapter = await _find_chapter_by_source_key(session, source_key)
            if chapter is None:
                return 0, None
            updated = getattr(chapter, "updated_at", None)
            if updated is not None and getattr(updated, "tzinfo", None) is None:
                updated = updated.replace(tzinfo=timezone.utc)
            return int(chapter.page_count or 0), updated

    async def _source_key_page_count(self, source_key: str) -> int:
        count, _updated = await self._source_key_state(source_key)
        return count

    async def _source_key_has_pages(self, source_key: str) -> bool:
        return await self._source_key_page_count(source_key) > 0

    async def _refresh_reader_titles(self, source_key: str, title: str | None) -> None:
        if not title:
            return
        factory = get_session_factory()
        async with factory() as session:
            await update_published_titles(session, source_key, title=title)

    async def _ingest_telegraph_post(
        self,
        message: Any,
        chat_id: int,
        binding: ResolvedBinding,
        *,
        skip_existing: bool = True,
    ) -> None:
        urls, reason = resolve_reader_urls_from_message(message)
        if not urls:
            logger.info(
                "Skip manga post without 在线阅读 chat=%s msg=%s reason=%s",
                chat_id,
                getattr(message, "id", None),
                reason,
            )
            return
        if should_skip_user_comment(
            message,
            ignore_user_comments=self.settings.ignore_user_comments,
            allowed_sender_ids=self.settings.allowed_sender_id_set,
        ):
            return

        page_url = urls[0]
        source_key = self._telegraph_source_key(chat_id, page_url)
        lock = self._telegraph_ingest_locks.setdefault(source_key, asyncio.Lock())
        await lock.acquire()
        try:
            await self._ingest_telegraph_locked(
                message,
                chat_id,
                binding,
                page_url=page_url,
                source_key=source_key,
                skip_existing=skip_existing,
            )
        finally:
            lock.release()

    async def _ingest_telegraph_locked(
        self,
        message: Any,
        chat_id: int,
        binding: ResolvedBinding,
        *,
        page_url: str,
        source_key: str,
        skip_existing: bool,
    ) -> None:
        caption = (
            getattr(message, "caption", None)
            or getattr(message, "text", None)
            or getattr(message, "message", None)
            or getattr(message, "raw_text", None)
        )
        parsed = parse_caption(caption)
        existing_pages, updated_at = await self._source_key_state(source_key)
        if skip_existing and existing_pages:
            # 旧 telegra.ph 半成品不在启动回扫里重拉；漫画本子在线阅读页仍可续传。
            if is_telegraph_url(page_url):
                logger.debug(
                    "Skip existing telegraph page %s pages=%d",
                    source_key,
                    existing_pages,
                )
                return
            if updated_at is not None:
                age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                if age > RESUME_MAX_AGE_SECONDS:
                    logger.debug(
                        "Skip stale telegraph chapter %s pages=%d age=%.0fs",
                        source_key,
                        existing_pages,
                        age,
                    )
                    return
        reader = await self.telegraph.fetch_reader_page(
            page_url,
            limit=self.settings.chapter_max_pages,
            page_hint=caption_page_hint(str(caption) if caption else None),
        )
        image_urls = reader.image_urls
        merged_title = merge_reader_title(parsed.title, reader.html_title, str(caption) if caption else None)
        if merged_title:
            parsed.title = merged_title
        if not image_urls:
            logger.warning(
                "Telegraph page has no images chat=%s msg=%s url=%s",
                chat_id,
                getattr(message, "id", None),
                page_url,
            )
            return
        if skip_existing and existing_pages:
            if existing_pages >= len(image_urls):
                await self._refresh_reader_titles(source_key, parsed.title)
                logger.debug(
                    "Skip existing telegraph chapter %s pages=%d",
                    source_key,
                    existing_pages,
                )
                return
            next_url = image_urls[existing_pages]
            if not await self.telegraph.url_exists(next_url, referer=page_url):
                await self._refresh_reader_titles(source_key, parsed.title)
                logger.debug(
                    "Skip telegraph chapter %s have=%d next missing",
                    source_key,
                    existing_pages,
                )
                return
            logger.info(
                "Resume telegraph chapter %s have=%d listed=%d",
                source_key,
                existing_pages,
                len(image_urls),
            )

        message_id = int(getattr(message, "id", 0) or 0)
        date = getattr(message, "date", None) or datetime.now(timezone.utc)
        if getattr(date, "tzinfo", None) is None:
            date = date.replace(tzinfo=timezone.utc)
        sender = getattr(message, "from_user", None) or getattr(message, "sender_chat", None)
        sender_id = getattr(sender, "id", None) if sender is not None else None
        chat_title = binding.source_title(getattr(getattr(message, "chat", None), "title", None))
        temp_dir = Path(self.settings.temp_dir) / "telegraph" / str(chat_id) / str(message_id or "page")
        temp_dir.mkdir(parents=True, exist_ok=True)

        pages: list[PendingPage] = []
        inspected = []
        listed = len(image_urls)
        consecutive_miss = 0
        start_index = existing_pages + 1 if existing_pages else 1
        for index, image_url in enumerate(image_urls, start=1):
            if index < start_index:
                continue
            suffix = Path(image_url.split("?", 1)[0]).suffix or ".jpg"
            filename = f"{index:03d}{suffix.lower()}"
            dest = temp_dir / filename
            try:
                await self.telegraph.download_reader_file(image_url, dest, referer=page_url)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                consecutive_miss += 1
                if status == 404:
                    logger.info(
                        "Reader image missing chat=%s msg=%s url=%s",
                        chat_id,
                        message_id,
                        image_url,
                    )
                else:
                    logger.exception(
                        "Telegraph image download failed chat=%s msg=%s url=%s",
                        chat_id,
                        message_id,
                        image_url,
                    )
                if consecutive_miss >= 3:
                    if pages:
                        listed = len(pages)
                    logger.info(
                        "Stop reader gallery after %d consecutive misses chat=%s pages=%d url=%s",
                        consecutive_miss,
                        chat_id,
                        len(pages),
                        page_url,
                    )
                    break
                continue
            consecutive_miss = 0
            if not dest.exists() or dest.stat().st_size == 0:
                logger.warning("Empty telegraph image %s", image_url)
                continue
            info = inspect_image_file(dest)
            inspected.append(info)
            if not info.valid:
                logger.info(
                    "Reject telegraph image chat=%s msg=%s url=%s reason=%s %dx%d %dB",
                    chat_id,
                    message_id,
                    image_url,
                    info.reason,
                    info.width,
                    info.height,
                    info.size,
                )
                dest.unlink(missing_ok=True)
            else:
                pages.append(
                    PendingPage(
                        message_id=encode_telegraph_page_message_id(message_id, index),
                        local_path=dest,
                        filename=filename,
                        caption=str(caption) if caption else None,
                        grouped_id=None,
                        date=date,
                        sender_id=sender_id,
                        reply_to_msg_id=None,
                        metadata_caption=str(caption) if caption else None,
                    )
                )
            if len(inspected) >= 3 and all(not item.valid for item in inspected):
                logger.warning(
                    "Abort telegraph ingest after %d unusable images chat=%s url=%s",
                    len(inspected),
                    chat_id,
                    page_url,
                )
                break

        if existing_pages:
            usable, reason = (bool(pages), "resume" if pages else "no new pages")
        else:
            usable, reason = chapter_images_usable(inspected, expected=listed)
        if not usable:
            logger.warning(
                "Skip telegraph chapter chat=%s msg=%s title=%s reason=%s listed=%d valid=%d url=%s",
                chat_id,
                message_id,
                parsed.title,
                reason,
                len(image_urls),
                sum(1 for item in inspected if item.valid),
                page_url,
            )
            for page in pages:
                try:
                    page.local_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return

        if not pages:
            return

        chapter = PendingChapter(
            bucket_key=source_key,
            chat_id=chat_id,
            chat_title=chat_title,
            pages=pages,
            title_hint=parsed.title,
            author_hint=parsed.author,
            tags=list(parsed.tags),
            source_key=source_key,
        )
        logger.info(
            "Telegraph chapter chat=%s msg=%s title=%s pages=%d url=%s",
            chat_id,
            message_id,
            parsed.title,
            len(pages),
            page_url,
        )
        await self._on_chapter_ready(chapter)

    def _should_accept(self, message: Any, chat_id: int, chat: Any) -> bool:
        is_video = is_video_message(message)
        is_image = is_image_message(
            message,
            accept_image_documents=self.settings.accept_image_documents,
        )
        if not is_image and not is_video:
            return False

        binding = self._binding_for(chat_id)
        is_discussion_chat = bool(
            binding and same_telegram_peer(binding.discussion_id, chat_id)
        ) or _is_discussion_entity(chat)
        is_source_channel = bool(
            binding
            and binding.channel_id is not None
            and same_telegram_peer(binding.channel_id, chat_id)
            and not same_telegram_peer(binding.discussion_id, chat_id)
        )
        if is_video and binding is not None and not binding.forward_videos:
            return False
        if is_video and not self._video_passes_filter(message, binding, chat_id=chat_id):
            return False
        if self._discussion_only:
            if is_source_channel:
                # 主站只要讨论区漫画图；采集频道里的 mp4 仍要转发到运营频道
                if not is_video:
                    return False
            elif is_discussion_chat:
                # 讨论区视频必须回帖到标题卡，避免把群里广告片当成新漫画
                if reply_to_id(message) is None:
                    return False
            else:
                return False

        if should_skip_user_comment(
            message,
            ignore_user_comments=self.settings.ignore_user_comments,
            allowed_sender_ids=self.settings.allowed_sender_id_set,
        ):
            return False
        return True

    def _video_passes_filter(
        self,
        message: Any,
        binding: ResolvedBinding | None,
        *,
        chat_id: int | None = None,
        extra_text: str | None = None,
    ) -> bool:
        blob_parts = [
            extra_text,
            getattr(message, "caption", None),
            getattr(message, "text", None),
            getattr(message, "message", None),
            document_filename(message),
        ]
        reply_id = reply_to_id(message)
        if chat_id and reply_id:
            cached = self._reply_caption_cache.get((int(chat_id), int(reply_id)))
            if cached:
                blob_parts.append(cached)
        text = " ".join(str(part) for part in blob_parts if part)
        allow = parse_filter_keywords(getattr(self.settings, "outbound_video_allow_keywords", ""))
        # 讨论区视频往往没配文，标题在父帖；没文本时先不因白名单拦截，后面拿到标题再判
        apply_allow = bool(text.strip()) or extra_text is not None
        return should_accept_outbound_video(
            text=text,
            duration=video_duration_seconds(message),
            enabled=bool(getattr(self.settings, "outbound_forward_videos", True)),
            binding_enabled=True if binding is None else bool(binding.forward_videos),
            block_keywords=parse_filter_keywords(
                getattr(self.settings, "outbound_video_block_keywords", "")
            ),
            allow_keywords=allow if apply_allow else [],
            min_seconds=int(getattr(self.settings, "outbound_video_min_seconds", 0) or 0),
            max_seconds=int(getattr(self.settings, "outbound_video_max_seconds", 0) or 0),
        )

    async def _materialize_page(
        self,
        message: Any,
        *,
        chat_id: int | None = None,
        metadata_caption: str | None = None,
        skip_known: set[int] | None = None,
    ) -> PendingPage | None:
        chat = getattr(message, "chat", None)
        resolved_chat_id = int(
            chat_id
            or getattr(chat, "id", 0)
            or getattr(message, "chat_id", 0)
            or 0
        )
        chat_id = resolved_chat_id
        if not chat_id or not self._should_accept(message, chat_id, chat):
            return None
        message_id = int(getattr(message, "id", 0) or 0)
        raw_reply_id = reply_to_id(message)
        self._remember_reply(chat_id, message_id, raw_reply_id)
        if skip_known is not None and message_id in skip_known:
            return None
        if self.client is None:
            return None

        caption = metadata_caption
        root_id = self._resolve_thread_root(chat_id, raw_reply_id)
        if caption is None and self._discussion_only and root_id is not None:
            caption = await self._get_reply_caption(chat_id, message, reply_id=root_id)

        filename = document_filename(message)
        if is_video_message(message):
            if not self._video_passes_filter(
                message,
                self._binding_for(chat_id),
                chat_id=chat_id,
                extra_text=caption,
            ):
                logger.info("Skip video by outbound filter chat=%s msg=%s", chat_id, message_id)
                return None
            date = getattr(message, "date", None) or datetime.now(timezone.utc)
            if getattr(date, "tzinfo", None) is None:
                date = date.replace(tzinfo=timezone.utc)
            sender = getattr(message, "from_user", None) or getattr(message, "sender_chat", None)
            # 大视频只记 tg:chat:msg，转发时 copy_message，不落盘也不上传图床
            return PendingPage(
                message_id=message_id,
                local_path=Path(os.devnull),
                filename=filename or f"{message_id}.mp4",
                caption=getattr(message, "caption", None)
                or getattr(message, "text", None)
                or getattr(message, "message", None),
                grouped_id=grouped_id(message),
                date=date,
                sender_id=getattr(sender, "id", None) if sender is not None else None,
                reply_to_msg_id=root_id,
                metadata_caption=caption,
                kind="video",
            )

        temp_dir = Path(self.settings.temp_dir) / str(chat_id)
        temp_dir.mkdir(parents=True, exist_ok=True)
        if not filename:
            ext = ".jpg"
            doc = getattr(message, "document", None)
            mime = getattr(doc, "mime_type", None) if doc is not None else None
            if mime:
                ext = mimetypes.guess_extension(mime) or ".bin"
            filename = f"{message_id}{ext}"

        local_path = temp_dir / f"{message_id}_{filename}"
        try:
            saved = await self.client.download_media(message, file_name=str(local_path))
        except Exception:
            logger.exception("Download failed chat=%s msg=%s", chat_id, message_id)
            return None
        if saved and not local_path.exists():
            try:
                Path(str(saved)).replace(local_path)
            except OSError:
                local_path = Path(str(saved))

        if not local_path.exists() or local_path.stat().st_size == 0:
            logger.warning("Empty download chat=%s msg=%s", chat_id, message_id)
            return None

        date = getattr(message, "date", None) or datetime.now(timezone.utc)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        sender = getattr(message, "from_user", None) or getattr(message, "sender_chat", None)
        return PendingPage(
            message_id=message_id,
            local_path=local_path,
            filename=filename,
            caption=getattr(message, "caption", None)
            or getattr(message, "text", None)
            or getattr(message, "message", None),
            grouped_id=grouped_id(message),
            date=date,
            sender_id=getattr(sender, "id", None) if sender is not None else None,
            reply_to_msg_id=root_id,
            metadata_caption=caption,
            kind="video" if is_video_message(message) else "image",
        )

    async def _load_known_message_ids(self) -> set[int]:
        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    select(Page.tg_message_id).where(Page.tg_message_id.is_not(None))
                )
            ).all()
        return {int(row[0]) for row in rows if row[0] is not None}

    async def _backfill_history(self) -> None:
        if self.client is None:
            return
        try:
            known = await self._load_known_message_ids()
        except Exception:
            logger.exception("Could not load known manga message ids")
            known = set()

        limit = self.settings.history_backfill_limit
        discussions: list[tuple[int, str | None]] = []
        telegraph_channels: list[tuple[int, str | None]] = []
        seen: set[int] = set()
        seen_telegraph: set[int] = set()
        for resolved in self._bindings_by_chat.values():
            if resolved.is_telegraph():
                chat_id = resolved.channel_id
                raw = telegram_raw_peer_id(chat_id)
                if raw is None or raw in seen_telegraph:
                    continue
                seen_telegraph.add(raw)
                telegraph_channels.append(
                    (int(chat_id) if chat_id is not None else raw, resolved.channel_title)
                )
                continue
            chat_id = resolved.discussion_id or (
                None if self._discussion_only else resolved.channel_id
            )
            raw = telegram_raw_peer_id(chat_id)
            if raw is None or raw in seen:
                continue
            seen.add(raw)
            discussions.append(
                (
                    int(chat_id) if chat_id is not None else raw,
                    resolved.discussion_title or resolved.channel_title,
                )
            )

        await self._catch_up_telegraph_channels(telegraph_channels, full_window=True)

        for chat_id, title in discussions:
            try:
                await self._backfill_chat(chat_id, title, known, limit)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("History backfill failed for chat=%s", chat_id)

    async def _telegraph_source_key_prefixes(self, chat_id: int) -> list[str]:
        prefixes = [f"{int(chat_id)}:tgph:"]
        raw = telegram_raw_peer_id(chat_id)
        if raw is not None:
            marked = -int(f"100{raw}")
            for value in (raw, marked):
                prefix = f"{value}:tgph:"
                if prefix not in prefixes:
                    prefixes.append(prefix)
        return prefixes

    async def _last_telegraph_source_message_id(self, chat_id: int) -> int | None:
        prefixes = await self._telegraph_source_key_prefixes(chat_id)
        factory = get_session_factory()
        ids: list[int] = []
        async with factory() as session:
            for prefix in prefixes:
                rows = (
                    await session.execute(
                        select(Page.tg_message_id)
                        .join(Chapter, Page.chapter_id == Chapter.id)
                        .where(
                            Chapter.source_key.startswith(prefix),
                            Page.tg_message_id.is_not(None),
                        )
                    )
                ).all()
                for (stored,) in rows:
                    decoded = decode_telegraph_page_message_id(stored)
                    if decoded:
                        ids.append(decoded)
        return max(ids) if ids else None

    async def _telegraph_catchup_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=TELEGRAPH_CATCHUP_INTERVAL_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            if self._closing:
                return
            try:
                await self._catch_up_telegraph_channels()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic telegraph catch-up failed")

    async def _iter_telegraph_channels(self) -> list[tuple[int, str | None]]:
        seen: set[int] = set()
        channels: list[tuple[int, str | None]] = []
        for resolved in self._bindings_by_chat.values():
            if not resolved.is_telegraph():
                continue
            chat_id = resolved.channel_id
            raw = telegram_raw_peer_id(chat_id)
            if raw is None or raw in seen:
                continue
            seen.add(raw)
            channels.append(
                (int(chat_id) if chat_id is not None else raw, resolved.channel_title)
            )
        return channels

    async def _catch_up_telegraph_channels(
        self,
        channels: list[tuple[int, str | None]] | None = None,
        *,
        full_window: bool = False,
    ) -> None:
        async with self._telegraph_catchup_lock:
            await self._catch_up_telegraph_channels_locked(
                channels, full_window=full_window
            )

    async def _catch_up_telegraph_channels_locked(
        self,
        channels: list[tuple[int, str | None]] | None = None,
        *,
        full_window: bool = False,
    ) -> None:
        items = channels if channels is not None else await self._iter_telegraph_channels()
        for chat_id, title in items:
            try:
                last_id = await self._last_telegraph_source_message_id(chat_id)
                if last_id is None or full_window:
                    logger.info(
                        "%s telegraph channel %s (%s) last %d posts",
                        "Bootstrap" if last_id is None else "Repair",
                        title or chat_id,
                        chat_id,
                        TELEGRAPH_CATCHUP_LIMIT,
                    )
                    await self._backfill_telegraph_chat(
                        chat_id,
                        title,
                        TELEGRAPH_CATCHUP_LIMIT,
                        min_id=0,
                    )
                    continue
                await self._backfill_telegraph_chat(
                    chat_id,
                    title,
                    TELEGRAPH_CATCHUP_LIMIT,
                    min_id=last_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegraph catch-up failed for chat=%s", chat_id)

    async def _backfill_telegraph_chat(
        self,
        chat_id: int,
        title: str | None,
        limit: int,
        *,
        min_id: int | None = None,
    ) -> None:
        if self.client is None or limit <= 0:
            return
        logger.info(
            "Catching up telegraph channel %s (%s) after msg=%s limit=%d",
            title or chat_id,
            chat_id,
            min_id,
            limit,
        )
        ingested = 0
        scanned = 0
        messages: list[Any] = []
        async for message in self.client.get_chat_history(chat_id, limit=limit):
            scanned += 1
            mid = int(getattr(message, "id", 0) or 0)
            if min_id is not None and min_id > 0 and mid <= min_id:
                break
            messages.append(message)
        messages.reverse()
        for message in messages:
            try:
                binding = self._binding_for(chat_id)
                if binding is None:
                    continue
                urls, _reason = resolve_reader_urls_from_message(message)
                if not urls:
                    continue
                source_key = self._telegraph_source_key(chat_id, urls[0])
                existed = await self._source_key_has_pages(source_key)
                await self._ingest_telegraph_post(message, chat_id, binding)
                if not existed:
                    ingested += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Telegraph catch-up message failed chat=%s msg=%s",
                    chat_id,
                    getattr(message, "id", None),
                )
        logger.info(
            "Telegraph catch-up %s scanned=%d new_chapters=%d after=%s",
            chat_id,
            scanned,
            ingested,
            min_id,
        )

    async def _backfill_chat(
        self,
        chat_id: int,
        title: str | None,
        known: set[int],
        limit: int,
    ) -> None:
        if self.client is None or limit <= 0:
            return
        logger.info(
            "Backfilling discussion %s (%s) last %d messages",
            title or chat_id,
            chat_id,
            limit,
        )
        messages: list[Any] = []
        async for message in self.client.get_chat_history(chat_id, limit=limit):
            messages.append(message)
        messages.reverse()

        for message in messages:
            mid = int(getattr(message, "id", 0) or 0)
            self._remember_reply(chat_id, mid, reply_to_id(message))

        groups: dict[str, list[Any]] = defaultdict(list)
        for message in messages:
            chat = getattr(message, "chat", None) or type(
                "Chat", (), {"id": chat_id, "title": title}
            )()
            if not self._should_accept(message, chat_id, chat):
                continue
            message_id = int(getattr(message, "id", 0) or 0)
            if message_id in known:
                continue
            root = self._resolve_thread_root(chat_id, reply_to_id(message))
            album = grouped_id(message)
            if root is not None:
                key = f"r:{root}"
            elif album is not None:
                key = f"g:{album}"
            else:
                key = f"m:{message_id}"
            groups[key].append(message)

        logger.info(
            "Backfill %s grouped %d unfinished threads from %d messages",
            chat_id,
            len(groups),
            len(messages),
        )
        binding = self._binding_for(chat_id)
        chat_title = binding.source_title(title) if binding else title
        for key, group in groups.items():
            pages: list[PendingPage] = []
            metadata = None
            first = group[0]
            root = self._resolve_thread_root(chat_id, reply_to_id(first))
            if root is not None:
                metadata = await self._get_reply_caption(chat_id, first, reply_id=root)
            for message in group:
                page = await self._materialize_page(
                    message,
                    chat_id=chat_id,
                    metadata_caption=metadata,
                    skip_known=known,
                )
                if page is None:
                    continue
                pages.append(page)
                known.add(page.message_id)
            if not pages:
                continue
            chapter = PendingChapter(
                bucket_key=f"{chat_id}:{key}",
                chat_id=chat_id,
                chat_title=chat_title,
                pages=pages,
            )
            for page in pages:
                for caption in (page.metadata_caption, page.caption):
                    if not caption:
                        continue
                    parsed = parse_caption(caption)
                    if parsed.title and not chapter.title_hint:
                        chapter.title_hint = parsed.title
                    if parsed.author and not chapter.author_hint:
                        chapter.author_hint = parsed.author
                    for tag in parsed.tags:
                        if tag not in chapter.tags:
                            chapter.tags.append(tag)
            try:
                await self._on_chapter_ready(chapter)
            except Exception:
                logger.exception("Backfill publish failed for %s", chapter.bucket_key)

    async def _on_chapter_ready(self, chapter: PendingChapter) -> None:
        captions: list[str] = []
        batch_urls: list[str] = []
        batch_ids: list[int] = []
        batch_kinds: list[str] = []
        published = None
        site_urls: list[str] = []
        video_urls: list[str] = []
        new_video_urls: list[str] = []
        flush_every = 10

        binding = self._binding_for(chapter.chat_id)
        source_chat_id = str(chapter.chat_id)
        source_chat_title = (
            binding.source_title(chapter.chat_title) if binding else chapter.chat_title
        )
        title_hint = chapter.title_hint
        if not title_hint:
            for page in chapter.pages:
                for cap in (page.metadata_caption, page.caption):
                    if not cap:
                        continue
                    title_hint = parse_caption(cap).title
                    if title_hint:
                        break
                if title_hint:
                    break
        caption_title = title_hint
        if not title_hint:
            title_hint = source_chat_title or chapter.chat_title or f"chat-{chapter.chat_id}"
        source_key = self._chapter_source_key(chapter, source_chat_id)
        if not source_key:
            logger.warning("Skip chapter without source_key chat=%s", chapter.chat_id)
            return

        new_chapter = False
        site_published: bool | None = None
        site_publish_error: str | None = None
        is_final = bool(getattr(chapter, "is_final", True))

        if not chapter.pages:
            if not is_final:
                logger.info("Skip empty non-final checkpoint source_key=%s", source_key)
                return
            stored = await self._load_stored_outbound(source_key)
            if stored is None:
                logger.info("Final flush has no stored chapter source_key=%s", source_key)
                return
            published = stored.published
            site_urls = list(stored.image_urls)
            video_urls = list(stored.video_urls)
            if stored.title:
                if not title_hint or title_hint.startswith("chat-"):
                    title_hint = stored.title
                caption_title = caption_title or stored.title
            if stored.author and not chapter.author_hint:
                chapter.author_hint = stored.author
            if stored.tags and not chapter.tags:
                chapter.tags = list(stored.tags)
            if stored.source_chat_id:
                source_chat_id = stored.source_chat_id
            if stored.source_chat_title:
                source_chat_title = stored.source_chat_title

        async def publish_to_site() -> bool:
            nonlocal site_publish_error, site_published
            if not self.site.enabled or not published or not site_urls:
                return True
            if published.site_published_page_count >= len(site_urls):
                site_published = True
                return True
            try:
                body = await self.site.publish_chapter(
                    title=title_hint,
                    chapter_title=caption_title,
                    source_key=source_key,
                    source_chat_id=source_chat_id,
                    source_chat_title=source_chat_title,
                    author=chapter.author_hint,
                    tags=chapter.tags,
                    image_urls=site_urls,
                    local=published,
                )
                site_published = body is not None
                site_publish_error = None
                if site_published:
                    factory = get_session_factory()
                    async with factory() as session:
                        site_manga_id, site_chapter_id = (
                            await mark_site_publish_succeeded(
                                session,
                                published.chapter_id,
                                page_count=len(site_urls),
                                response=body,
                            )
                        )
                    published.site_published_page_count = max(
                        published.site_published_page_count,
                        len(site_urls),
                    )
                    published.site_manga_id = site_manga_id
                    published.site_chapter_id = site_chapter_id
                if not site_published:
                    logger.warning(
                        "Site publish returned empty body; hold outbound Telegram slug=%s",
                        published.slug if published else "?",
                    )
            except Exception as exc:
                site_published = False
                site_publish_error = str(exc).strip() or exc.__class__.__name__
                logger.exception("Failed to push chapter batch to AnimeStream site")
                schedule_alert(
                    "manga_site_fail",
                    title=f"漫画站点发布失败：{title_hint or source_key}",
                    detail=str(exc),
                    fingerprint=str(source_key or title_hint or "site"),
                )
            else:
                if site_published is False:
                    site_publish_error = site_publish_error or "站点返回空响应"
                    schedule_alert(
                        "manga_site_fail",
                        title=f"漫画站点发布失败：{title_hint or source_key}",
                        detail="站点返回空响应",
                        fingerprint=str(source_key or title_hint or "site"),
                    )
            return site_published is True

        async def flush_batch() -> None:
            nonlocal published, site_urls, video_urls, new_video_urls, batch_urls, batch_ids, batch_kinds, new_chapter
            if not batch_urls:
                return
            factory = get_session_factory()
            async with factory() as session:
                published = await publish_chapter(
                    session,
                    title=title_hint,
                    chapter_title=caption_title,
                    source_key=source_key,
                    source_chat_id=source_chat_id,
                    source_chat_title=source_chat_title,
                    author=chapter.author_hint,
                    tags=chapter.tags,
                    image_urls=list(batch_urls),
                    message_ids=list(batch_ids),
                    media_kinds=list(batch_kinds),
                )
                if published:
                    if published.created:
                        new_chapter = True
                    if published.new_video_urls:
                        new_video_urls.extend(published.new_video_urls)
                    self._index_published_thread(source_key, list(batch_ids))
                    merged = await list_chapter_image_urls(session, published.chapter_id)
                    if merged:
                        site_urls = merged
                    videos = await list_chapter_videos(session, published.chapter_id)
                    if videos:
                        video_urls = videos
                    logger.info(
                        "Local catalog: /read/%s/%s pages=%d videos=%d",
                        published.slug,
                        published.number,
                        published.page_count,
                        len(video_urls),
                    )
            batch_urls = []
            batch_ids = []
            batch_kinds = []
            if self.site.enabled and published and site_urls:
                await publish_to_site()

        for page in chapter.pages:
            try:
                if page.kind == "video":
                    url = f"tg:{chapter.chat_id}:{page.message_id}"
                else:
                    url = await self._upload_with_retry(page)
                batch_urls.append(url)
                batch_ids.append(page.message_id)
                batch_kinds.append(page.kind if page.kind == "video" else "image")
                for caption in (page.metadata_caption, page.caption):
                    if caption:
                        captions.append(caption)
            except Exception as exc:
                logger.exception("ImgBed upload failed for %s", page.local_path)
                schedule_alert(
                    "manga_imgbed_upload_fail",
                    title=f"漫画图床上传失败：{title_hint or source_key}",
                    detail=f"{page.local_path}\n{exc}",
                    fingerprint=str(source_key or "imgbed"),
                )
                break
            finally:
                try:
                    if page.kind != "video":
                        page.local_path.unlink(missing_ok=True)
                except Exception:
                    pass
            if len(batch_urls) >= flush_every:
                await flush_batch()

        for page in chapter.pages:
            if page.kind == "video":
                continue
            try:
                page.local_path.unlink(missing_ok=True)
            except Exception:
                pass

        if batch_urls:
            await flush_batch()

        if published and self.site.enabled and site_urls:
            await publish_to_site()

        if not is_final:
            logger.info(
                "Hold outbound until discussion thread closes source_key=%s pages=%d videos=%d",
                source_key,
                len(site_urls),
                len(new_video_urls),
            )
            return
        if not published:
            return

        forward_videos = bool(getattr(self.settings, "outbound_forward_videos", True))
        pending_videos = video_urls if forward_videos else []
        send_album, outbound_videos = plan_outbound_post(
            is_final=True,
            album_sent=bool(published.album_sent),
            new_video_urls=pending_videos,
            sent_video_urls=published.sent_video_urls,
        )
        if not send_album and not outbound_videos and (new_chapter or pending_videos):
            logger.info(
                "Skip duplicate outbound slug=%s chapter=%s album_sent=%s videos=%d",
                published.slug,
                published.number,
                bool(published.album_sent),
                len(pending_videos),
            )
        if self.site.enabled and site_urls and site_published is False:
            factory = get_session_factory()
            async with factory() as session:
                retry = await mark_outbound_failed(
                    session,
                    published.chapter_id,
                    site_publish_error or "Site publish did not confirm success",
                )
            logger.warning(
                "Deferred delivery after site failure source_key=%s attempt=%d "
                "next_retry_at=%s exhausted=%s",
                source_key,
                retry.attempts,
                retry.retry_at,
                retry.exhausted,
            )
            return
        if can_publish_outbound(
            channel_enabled=self.channel.enabled,
            send_album=send_album,
            video_urls=outbound_videos,
            site_enabled=self.site.enabled,
            has_site_images=bool(site_urls),
            site_published=site_published,
        ):
            lock = self._outbound_locks.setdefault(source_key, asyncio.Lock())
            async with lock:
                try:
                    factory = get_session_factory()
                    async with factory() as session:
                        current = await _find_chapter_by_source_key(session, source_key)
                        if current is not None:
                            album_sent, sent_videos = read_outbound_state(current)
                            send_album, outbound_videos = plan_outbound_post(
                                is_final=True,
                                album_sent=album_sent,
                                new_video_urls=pending_videos,
                                sent_video_urls=sent_videos,
                            )
                    if not send_album and not outbound_videos:
                        logger.info(
                            "Skip duplicate outbound after lock slug=%s chapter=%s",
                            published.slug,
                            published.number,
                        )
                        return
                    posted = await self.channel.publish_chapter(
                        title=title_hint,
                        author=chapter.author_hint,
                        tags=chapter.tags,
                        image_urls=site_urls if send_album else [],
                        video_urls=outbound_videos,
                        local=PublishedChapter(
                            manga_id=published.manga_id,
                            chapter_id=published.chapter_id,
                            slug=published.slug,
                            number=published.number,
                            page_count=published.page_count,
                            created=send_album,
                            album_sent=bool(published.album_sent),
                            sent_video_urls=list(published.sent_video_urls or []),
                            outbound_attempts=published.outbound_attempts,
                            site_published_page_count=published.site_published_page_count,
                            site_manga_id=published.site_manga_id,
                            site_chapter_id=published.site_chapter_id,
                        ),
                    )
                    if not posted:
                        raise RuntimeError("Telegram outbound channel returned no message")
                    factory = get_session_factory()
                    async with factory() as session:
                        await mark_outbound_sent(
                            session,
                            published.chapter_id,
                            album=send_album,
                            video_urls=outbound_videos,
                        )
                except Exception as exc:
                    factory = get_session_factory()
                    async with factory() as session:
                        retry = await mark_outbound_failed(
                            session,
                            published.chapter_id,
                            exc,
                        )
                    if retry.attempts == 1 or retry.exhausted:
                        logger.exception(
                            "Failed to post chapter to outbound Telegram channel "
                            "attempt=%d exhausted=%s",
                            retry.attempts,
                            retry.exhausted,
                        )
                    else:
                        logger.warning(
                            "Outbound Telegram retry failed source_key=%s attempt=%d "
                            "next_retry_at=%s error=%s",
                            source_key,
                            retry.attempts,
                            retry.retry_at,
                            exc,
                        )
                    if retry.attempts == 1 or retry.exhausted:
                        retry_detail = (
                            "已停止自动重试"
                            if retry.exhausted
                            else f"下次重试：{retry.retry_at}"
                        )
                        schedule_alert(
                            "manga_channel_publish_fail",
                            title=f"漫画频道补发失败：{title_hint or source_key}",
                            detail=f"{exc}\n{retry_detail}",
                            fingerprint=str(source_key or "outbound"),
                        )
        elif published and self.channel.enabled and (send_album or outbound_videos):
            logger.warning(
                "Skip outbound Telegram until site publish succeeds slug=%s pages=%d",
                published.slug,
                len(site_urls),
            )

    def _chapter_source_key(self, chapter: PendingChapter, source_chat_id: str) -> str | None:
        if chapter.source_key:
            return chapter.source_key
        if chapter.pages and chapter.pages[0].reply_to_msg_id is not None:
            return f"{source_chat_id}:r:{chapter.pages[0].reply_to_msg_id}"
        if chapter.pages and chapter.pages[0].grouped_id is not None:
            return f"{source_chat_id}:g:{chapter.pages[0].grouped_id}"
        if chapter.pages:
            return f"{source_chat_id}:m:{chapter.pages[0].message_id}"
        bucket_key = chapter.bucket_key or ""
        if ":r:" in bucket_key or ":g:" in bucket_key:
            return bucket_key
        return None

    async def _load_stored_outbound(self, source_key: str) -> StoredOutbound | None:
        factory = get_session_factory()
        async with factory() as session:
            return await load_stored_outbound(session, source_key)

    async def _stale_outbound_loop(self) -> None:
        await asyncio.sleep(8)
        while not self._closing:
            try:
                await self._flush_stale_outbound()
            except Exception:
                logger.exception("Stale outbound sweep failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=STALE_OUTBOUND_INTERVAL_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def _flush_stale_outbound(self) -> None:
        if not self.channel.enabled and not self.site.enabled:
            return
        idle = bucket_idle_seconds(
            "0:r:0",
            float(self.settings.chapter_idle_seconds),
            float(getattr(self.settings, "chapter_reply_idle_seconds", REPLY_IDLE_FLOOR_SECONDS)),
        )
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=idle)
        factory = get_session_factory()
        async with factory() as session:
            pending = await list_pending_reply_outbound(
                session,
                idle_before=cutoff,
                include_outbound=self.channel.enabled,
                include_site=self.site.enabled,
            )
        if not pending:
            return
        live = await self.assembler.live_source_keys()
        for stored in pending:
            if stored.source_key in live:
                logger.info(
                    "Skip stale outbound still ingesting source_key=%s title=%s",
                    stored.source_key,
                    stored.title,
                )
                continue
            chat_id = _chat_id_from_source_key(stored.source_key, stored.source_chat_id)
            logger.info(
                "Retry delivery for closed Telegram source source_key=%s title=%s pages=%d",
                stored.source_key,
                stored.title,
                stored.published.page_count,
            )
            await self._on_chapter_ready(
                PendingChapter(
                    bucket_key=stored.source_key,
                    chat_id=chat_id,
                    chat_title=stored.source_chat_title,
                    pages=[],
                    title_hint=stored.title,
                    author_hint=stored.author,
                    tags=list(stored.tags),
                    source_key=stored.source_key,
                    is_final=True,
                )
            )

    async def _upload_with_retry(self, page: PendingPage) -> str:
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                return await self.imgbed.upload_file(page.local_path, filename=page.filename)
            except Exception:
                if attempt == attempts:
                    raise
                delay = max(1.0, self.settings.cfbed_retry_delay_seconds) * (
                    2 ** (attempt - 1)
                )
                logger.warning(
                    "ImgBed upload retry %d/%d for %s in %ss",
                    attempt,
                    attempts - 1,
                    page.filename,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable upload retry state")

    async def _get_reply_caption(
        self, chat_id: int, message: Any, *, reply_id: int | None = None
    ) -> str | None:
        reply_id = reply_id if reply_id is not None else reply_to_id(message)
        if reply_id is None:
            return None
        key = (chat_id, reply_id)
        if key in self._reply_caption_cache:
            return self._reply_caption_cache[key]

        caption = None
        try:
            root = getattr(message, "reply_to_message", None)
            if root is None and self.client is not None:
                fetched = await self.client.get_messages(chat_id, reply_id)
                if isinstance(fetched, list):
                    root = fetched[0] if fetched else None
                else:
                    root = fetched
            if root:
                caption = (
                    getattr(root, "caption", None)
                    or getattr(root, "text", None)
                    or getattr(root, "message", None)
                    or getattr(root, "raw_text", None)
                )
        except Exception:
            logger.debug(
                "Could not resolve discussion root chat=%s msg=%s",
                chat_id,
                reply_id,
                exc_info=True,
            )
        self._reply_caption_cache[key] = caption
        return caption
