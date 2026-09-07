from __future__ import annotations

import json
import logging
import re
from typing import Any

import json_repair

from tg_signer.ai_tools import DEFAULT_MODEL, get_openai_client

logger = logging.getLogger("backend.ai")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def ai_available() -> bool:
    config = _load_ai_config()
    return bool(config and config.get("api_key"))


def _load_ai_config() -> dict[str, Any] | None:
    try:
        from backend.services.config import get_config_service

        config = get_config_service().get_ai_config()
    except Exception:
        logger.exception("读取系统 AI 配置失败")
        return None
    if not isinstance(config, dict):
        return None
    if config.get("api_key_decrypt_failed") or not str(config.get("api_key") or "").strip():
        return None
    return config


def parse_json_object(text: str) -> dict[str, Any]:
    raw = _FENCE.sub("", str(text or "").strip()).strip()
    if not raw:
        raise ValueError("AI 返回空内容")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json_repair.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("AI 返回的不是 JSON 对象")
    return data


def _content_text(response: Any) -> str:
    choice = (getattr(response, "choices", None) or [None])[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return str(content or "")


async def complete_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.1,
) -> dict[str, Any] | None:
    """调用系统 AI 配置，要求返回 JSON 对象。失败返回 None，不抛给业务。"""
    config = _load_ai_config()
    if not config:
        return None
    client = get_openai_client(
        api_key=str(config.get("api_key") or ""),
        base_url=str(config.get("base_url") or "") or None,
    )
    if client is None:
        logger.warning("无法创建 OpenAI 客户端")
        return None
    model = str(config.get("model") or "").strip() or DEFAULT_MODEL
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    try:
        response = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "response_format" in str(exc).casefold() or "json_object" in str(exc).casefold():
            kwargs.pop("response_format", None)
            try:
                response = await client.chat.completions.create(**kwargs)
            except Exception:
                logger.exception("流水线 AI 调用失败 model=%s", model)
                return None
        else:
            logger.exception("流水线 AI 调用失败 model=%s", model)
            return None
    try:
        return parse_json_object(_content_text(response))
    except Exception:
        logger.warning("流水线 AI 返回无法解析的 JSON")
        return None
