"""Telegram API 凭据解析（api_id / api_hash 统一入口）。

解析优先级：调用方传入的 env_* > 环境变量别名 > 配置服务 telegram 配置。
缺失或无效时抛 ValueError，由调用方决定如何降级或报错。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

ENV_API_ID_NAMES = ("TG_API_ID", "TELEGRAM_API_ID", "MANGA_TELEGRAM_API_ID")
ENV_API_HASH_NAMES = ("TG_API_HASH", "TELEGRAM_API_HASH", "MANGA_TELEGRAM_API_HASH")


def read_env_telegram_api() -> tuple[Optional[str], Optional[str]]:
    """读取环境变量里的 Telegram API 凭据；兼容 TG_* / TELEGRAM_* / MANGA_* 别名。"""
    api_id = None
    api_hash = None
    for name in ENV_API_ID_NAMES:
        raw = str(os.getenv(name) or "").strip()
        if raw:
            api_id = raw
            break
    for name in ENV_API_HASH_NAMES:
        raw = str(os.getenv(name) or "").strip()
        if raw:
            api_hash = raw
            break
    return api_id, api_hash


def resolve_telegram_api_credentials(
    tg_config: Dict[str, Any],
    *,
    env_api_id: Optional[str] = None,
    env_api_hash: Optional[str] = None,
) -> tuple[int, str]:
    """解析 api_id / api_hash；无效时抛 ValueError。"""
    raw_id = env_api_id or tg_config.get("api_id")
    raw_hash = env_api_hash or tg_config.get("api_hash")
    try:
        api_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        api_id = None
    if isinstance(raw_hash, str):
        raw_hash = raw_hash.strip()
    if not api_id or not raw_hash:
        raise ValueError("未配置 Telegram API ID 或 API Hash")
    return api_id, str(raw_hash)
