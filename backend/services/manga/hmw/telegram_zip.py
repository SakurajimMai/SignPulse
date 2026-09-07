from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from backend.core.config import get_settings as get_panel_settings
from backend.services.config import get_config_service
from backend.services.manga.config import MangaSettings
from backend.services.telegram.credentials import resolve_telegram_api_credentials
from backend.utils.account_locks import get_account_lock
from backend.utils.names import validate_storage_name
from backend.utils.proxy import build_proxy_dict
from backend.utils.tg_session import (
    get_session_mode,
    load_account_session_string,
    resolve_effective_proxy,
)
from tg_signer.core.client import _CLIENT_INSTANCES, get_client

from .paths import hmw_dirs
from .service import relative_to_hmw

logger = logging.getLogger("backend.manga.hmw.telegram")

ARCHIVE_EXTS = {".zip", ".7z"}
ARCHIVE_MIMES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-7z-compressed",
    "application/7z",
}


class TelegramZipError(RuntimeError):
    pass


def _resolve_api_credentials(settings: MangaSettings) -> tuple[int | None, str | None]:
    if settings.telegram_api_id and settings.telegram_api_hash:
        return settings.telegram_api_id, settings.telegram_api_hash
    try:
        tg_config = get_config_service().get_telegram_config()
        return resolve_telegram_api_credentials(
            tg_config,
            env_api_id=os.getenv("TG_API_ID") or os.getenv("TELEGRAM_API_ID"),
            env_api_hash=os.getenv("TG_API_HASH") or os.getenv("TELEGRAM_API_HASH"),
        )
    except ValueError:
        return settings.telegram_api_id, settings.telegram_api_hash or None


def _document_name(message) -> str:
    doc = getattr(message, "document", None)
    if doc is None:
        return ""
    name = str(getattr(doc, "file_name", None) or "").strip()
    if name:
        return name
    for attr in getattr(doc, "attributes", None) or []:
        candidate = str(getattr(attr, "file_name", None) or "").strip()
        if candidate:
            return candidate
    return ""


def _is_archive_message(message) -> bool:
    doc = getattr(message, "document", None)
    if doc is None:
        return False
    name = _document_name(message).casefold()
    if any(name.endswith(ext) for ext in ARCHIVE_EXTS):
        return True
    mime = str(getattr(doc, "mime_type", "") or "").casefold()
    return mime in ARCHIVE_MIMES


def _parse_chat(chat: str) -> str | int:
    raw = str(chat or "").strip()
    if not raw:
        raise TelegramZipError("聊天不能为空")
    if raw.startswith("-") and raw[1:].isdigit():
        return int(raw)
    if raw.isdigit():
        return int(raw)
    return raw.lstrip("@")


async def _borrow_client(settings: MangaSettings, account: str):
    account = validate_storage_name(account, field_name="account_name")
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
            raise TelegramZipError(f"账号 {account} 没有可用 session，请到 /accounts 重新登录")
    proxy_value = resolve_effective_proxy(account)
    if not proxy_value:
        proxy_value = get_config_service().get_global_proxy()
    proxy = build_proxy_dict(proxy_value) if proxy_value else None
    api_id, api_hash = _resolve_api_credentials(settings)
    lock = get_account_lock(account)
    await lock.acquire()
    try:
        client_key = str(session_dir.joinpath(account).resolve())
        existing = _CLIENT_INSTANCES.get(client_key)
        client = get_client(
            account,
            proxy=proxy,
            workdir=session_dir,
            session_string=session_string,
            in_memory=in_memory,
            api_id=api_id,
            api_hash=api_hash,
            no_updates=existing is None,
        )
        await client.__aenter__()
        return client, lock, True
    except Exception:
        lock.release()
        raise


async def _release_client(client, lock, entered: bool) -> None:
    try:
        if entered and client is not None:
            await client.__aexit__(None, None, None)
    finally:
        lock.release()


def _serialize_doc(message) -> dict[str, Any]:
    doc = getattr(message, "document", None)
    date = getattr(message, "date", None)
    return {
        "message_id": getattr(message, "id", None),
        "file_name": _document_name(message),
        "size": getattr(doc, "file_size", None) if doc is not None else None,
        "mime_type": getattr(doc, "mime_type", None) if doc is not None else None,
        "date": date.isoformat() if date is not None else None,
        "caption": (getattr(message, "caption", None) or "")[:200],
    }


async def list_archive_docs(
    settings: MangaSettings,
    *,
    account: str,
    chat: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    chat_ref = _parse_chat(chat)
    limit = max(1, min(int(limit or 50), 80))
    client, lock, entered = await _borrow_client(settings, account)
    items: list[dict[str, Any]] = []
    try:
        async for message in client.get_chat_history(chat_ref, limit=limit * 3):
            if _is_archive_message(message):
                items.append(_serialize_doc(message))
            if len(items) >= limit:
                break
    except TelegramZipError:
        raise
    except Exception as exc:
        raise TelegramZipError(f"读取频道文档失败：{exc}") from exc
    finally:
        await _release_client(client, lock, entered)
    return items


async def download_archive_doc(
    settings: MangaSettings,
    *,
    account: str,
    chat: str,
    message_id: int,
) -> dict[str, Any]:
    chat_ref = _parse_chat(chat)
    inbox = hmw_dirs(settings).inbox
    client, lock, entered = await _borrow_client(settings, account)
    try:
        message = await client.get_messages(chat_ref, message_id)
        if isinstance(message, list):
            message = message[0] if message else None
        if message is None or not _is_archive_message(message):
            raise TelegramZipError("该消息不是 zip / 7z 文档")
        filename = _document_name(message) or f"{message_id}.zip"
        safe_name = Path(filename).name
        dest = inbox / safe_name
        if dest.exists():
            dest = inbox / f"{message_id}_{safe_name}"
        saved = await client.download_media(message, file_name=str(dest))
        path = Path(str(saved)) if saved else dest
        if not path.exists() or path.stat().st_size == 0:
            raise TelegramZipError("下载结果为空")
        return {
            "name": path.name,
            "path": relative_to_hmw(settings, path),
            "kind": "archive",
            "location": "inbox",
            "size": path.stat().st_size,
        }
    except TelegramZipError:
        raise
    except Exception as exc:
        raise TelegramZipError(f"下载失败：{exc}") from exc
    finally:
        await _release_client(client, lock, entered)
