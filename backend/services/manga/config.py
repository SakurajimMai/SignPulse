from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backend.core.config import _merged_env
from backend.core.config import get_settings as get_panel_settings
from backend.services.manga.sources import (
    SourceBinding,
    binding_chat_refs,
    parse_source_bindings,
    serialize_source_bindings,
)
from backend.utils.atomic_io import read_json_safe, write_json_atomic
from tg_signer.security import decrypt_secret, encrypt_secret

logger = logging.getLogger("backend.manga.config")

SECRET_FIELDS = {
    "telegram_api_hash",
    "cfbed_auth_code",
    "cfbed_api_token",
    "site_publish_secret",
    "outbound_bot_token",
    "ehentai_cookie",
    "hmw_publisher_token",
    "hmw_s3_access_key",
    "hmw_s3_secret_key",
}
MASKED_SECRET = "********"
# 单话页数上限。长篇 IF 线可达数百页；环境里常用 2000，API 必须放得下。
CHAPTER_MAX_PAGES_CAP = 10000


def _env(*names: str, default: str = "") -> str:
    env = _merged_env()
    for name in names:
        value = str(env.get(name, "") or "").strip()
        if value:
            return value
    return default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _base_dir() -> Path:
    return Path(
        _env(
            "MANGA_DATA_DIR",
            "APP_DATA_DIR",
            "DATA_DIR",
            default=str(get_panel_settings().resolve_base_dir()),
        )
    )


def config_path() -> Path:
    raw = _env("MANGA_CONFIG_FILE")
    return Path(raw).expanduser() if raw else _base_dir() / ".manga_config.json"


@dataclass(frozen=True)
class MangaSettings:
    """Configuration for the manga submodule.

    Ingest uses a SignPulse account from /accounts (Pyrogram / kurigram),
    plus a separate manga catalog database.
    """

    enabled: bool = False
    telegram_account_name: str = ""
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_session_file: str = "/data/telegram.session"
    tg_source_chats: str = ""
    source_bindings: tuple[dict[str, str], ...] = ()
    tg_allowed_sender_ids: str = ""
    discussion_only: bool = True
    ignore_user_comments: bool = True
    chapter_idle_seconds: float = 8.0
    chapter_reply_idle_seconds: float = 900.0
    chapter_max_pages: int = 200
    history_backfill_limit: int = 1500
    accept_image_documents: bool = True
    cfbed_upload_url: str = ""
    cfbed_auth_code: str = ""
    cfbed_api_token: str = ""
    cfbed_extra_query: str = ""
    cfbed_public_base: str = ""
    cfbed_file_field: str = "file"
    cfbed_retry_delay_seconds: float = 30.0
    site_publish_url: str = ""
    site_publish_secret: str = ""
    outbound_enabled: bool = False
    outbound_channel: str = ""
    outbound_preview_count: int = 4
    outbound_button_text: str = "点击阅读"
    outbound_site_base: str = ""
    outbound_bot_token: str = ""
    outbound_forward_videos: bool = True
    outbound_video_block_keywords: str = ""
    outbound_video_allow_keywords: str = ""
    outbound_video_min_seconds: int = 0
    outbound_video_max_seconds: int = 0
    ehentai_enabled: bool = False
    ehentai_cookie: str = ""
    ehentai_exhentai: bool = False
    ehentai_search: str = ""
    ehentai_cats: str = "704"
    ehentai_max_pages: int = 400
    ehentai_search_pages: int = 1
    ehentai_delay_seconds: float = 1.2
    ehentai_gallery_delay_seconds: float = 5.0
    ehentai_poll_seconds: float = 300.0
    ehentai_translation_url: str = ""
    ehentai_translation_auto: bool = True
    hmw_api_url: str = "https://www.hmw.app/api/publisher"
    hmw_publisher_token: str = ""
    hmw_s3_endpoint: str = ""
    hmw_s3_region: str = "us-west-004"
    hmw_s3_bucket: str = ""
    hmw_s3_access_key: str = ""
    hmw_s3_secret_key: str = ""
    hmw_s3_public_url: str = ""
    hmw_s3_prefix: str = "comics/zh"
    hmw_avif_quality: int = 50
    hmw_convert_workers: int = 1
    hmw_upload_workers: int = 6
    hmw_request_timeout: int = 180
    ai_enabled: bool = False
    ai_refine_prompt: str = ""
    database_url: str = ""
    data_dir: str = "/data"
    temp_dir: str = "/data/tmp"

    @classmethod
    def from_environment(cls) -> "MangaSettings":
        source_chats = _env("MANGA_SOURCE_CHATS", "TG_SOURCE_CHATS")
        source_bindings_raw = _env("MANGA_SOURCE_BINDINGS")
        telegram_api_id = _env("MANGA_TELEGRAM_API_ID", "TELEGRAM_API_ID")
        telegram_api_hash = _env("MANGA_TELEGRAM_API_HASH", "TELEGRAM_API_HASH")
        parsed_bindings = parse_source_bindings(source_bindings_raw, legacy_chats=source_chats)
        telegram_account_name = _env("MANGA_TELEGRAM_ACCOUNT", "MANGA_ACCOUNT_NAME")
        auto_enabled = bool(
            parsed_bindings
            and (telegram_account_name or (telegram_api_id and telegram_api_hash))
        )
        data_dir = _base_dir()
        database_url = _env("MANGA_DATABASE_URL", "DATABASE_URL")
        if not database_url:
            database_url = f"sqlite:///{data_dir / 'tg_manga.db'}"
        return cls(
            enabled=_bool("MANGA_ENABLED", auto_enabled),
            telegram_account_name=telegram_account_name,
            telegram_api_id=_int("MANGA_TELEGRAM_API_ID", 0) or _int("TELEGRAM_API_ID", 0) or None,
            telegram_api_hash=telegram_api_hash,
            telegram_session_file=_env(
                "MANGA_TELEGRAM_SESSION_FILE",
                "TELEGRAM_SESSION_FILE",
                default=str(data_dir / "telegram.session"),
            ),
            tg_source_chats=source_chats,
            source_bindings=tuple(serialize_source_bindings(parsed_bindings)),
            tg_allowed_sender_ids=_env("MANGA_ALLOWED_SENDER_IDS", "TG_ALLOWED_SENDER_IDS"),
            discussion_only=_bool("MANGA_DISCUSSION_ONLY", _bool("TG_DISCUSSION_ONLY", True)),
            ignore_user_comments=_bool("MANGA_IGNORE_USER_COMMENTS", _bool("IGNORE_USER_COMMENTS", True)),
            chapter_idle_seconds=max(_float("MANGA_CHAPTER_IDLE_SECONDS", _float("CHAPTER_IDLE_SECONDS", 8.0)), 1.0),
            chapter_reply_idle_seconds=max(
                _float("MANGA_CHAPTER_REPLY_IDLE_SECONDS", 900.0),
                60.0,
            ),
            chapter_max_pages=max(_int("MANGA_CHAPTER_MAX_PAGES", _int("CHAPTER_MAX_PAGES", 200)), 1),
            history_backfill_limit=max(_int("MANGA_HISTORY_BACKFILL_LIMIT", 1500), 0),
            accept_image_documents=_bool("MANGA_ACCEPT_IMAGE_DOCUMENTS", _bool("ACCEPT_IMAGE_DOCUMENTS", True)),
            cfbed_upload_url=_env("CFBED_UPLOAD_URL").rstrip("/"),
            cfbed_auth_code=_env("CFBED_AUTH_CODE"),
            cfbed_api_token=_env("CFBED_API_TOKEN"),
            cfbed_extra_query=_env("CFBED_EXTRA_QUERY"),
            cfbed_public_base=_env("CFBED_PUBLIC_BASE").rstrip("/"),
            cfbed_file_field=_env("CFBED_FILE_FIELD", default="file"),
            cfbed_retry_delay_seconds=max(_float("CFBED_RETRY_DELAY_SECONDS", 30.0), 1.0),
            site_publish_url=_env("SITE_PUBLISH_URL").rstrip("/"),
            site_publish_secret=_env("SITE_PUBLISH_SECRET"),
            outbound_enabled=_bool("MANGA_OUTBOUND_ENABLED", False),
            outbound_channel=_env("MANGA_OUTBOUND_CHANNEL"),
            outbound_preview_count=max(1, min(_int("MANGA_OUTBOUND_PREVIEW_COUNT", 4), 10)),
            outbound_button_text=_env("MANGA_OUTBOUND_BUTTON_TEXT", default="点击阅读") or "点击阅读",
            outbound_site_base=_env("MANGA_OUTBOUND_SITE_BASE").rstrip("/"),
            outbound_bot_token=_env("MANGA_OUTBOUND_BOT_TOKEN"),
            outbound_forward_videos=_bool("MANGA_OUTBOUND_FORWARD_VIDEOS", True),
            outbound_video_block_keywords=_env("MANGA_OUTBOUND_VIDEO_BLOCK_KEYWORDS"),
            outbound_video_allow_keywords=_env("MANGA_OUTBOUND_VIDEO_ALLOW_KEYWORDS"),
            outbound_video_min_seconds=max(_int("MANGA_OUTBOUND_VIDEO_MIN_SECONDS", 0), 0),
            outbound_video_max_seconds=max(_int("MANGA_OUTBOUND_VIDEO_MAX_SECONDS", 0), 0),
            ehentai_enabled=_bool("MANGA_EHENTAI_ENABLED", False),
            ehentai_cookie=_env("MANGA_EHENTAI_COOKIE"),
            ehentai_exhentai=_bool("MANGA_EHENTAI_EXHENTAI", False),
            ehentai_search=_env("MANGA_EHENTAI_SEARCH"),
            ehentai_cats=_env("MANGA_EHENTAI_CATS", default="704") or "704",
            ehentai_max_pages=max(1, min(_int("MANGA_EHENTAI_MAX_PAGES", 400), 2000)),
            ehentai_search_pages=max(1, min(_int("MANGA_EHENTAI_SEARCH_PAGES", 1), 10)),
            ehentai_delay_seconds=max(_float("MANGA_EHENTAI_DELAY_SECONDS", 1.2), 0.0),
            ehentai_gallery_delay_seconds=max(
                _float("MANGA_EHENTAI_GALLERY_DELAY_SECONDS", 5.0), 0.0
            ),
            ehentai_poll_seconds=max(_float("MANGA_EHENTAI_POLL_SECONDS", 300.0), 60.0),
            ehentai_translation_url=_env("MANGA_EHENTAI_TRANSLATION_URL"),
            ehentai_translation_auto=_bool("MANGA_EHENTAI_TRANSLATION_AUTO", True),
            hmw_api_url=_env("HMW_API_URL", default="https://www.hmw.app/api/publisher").rstrip("/")
            or "https://www.hmw.app/api/publisher",
            hmw_publisher_token=_env("HMW_PUBLISHER_TOKEN"),
            hmw_s3_endpoint=_env("HMW_S3_ENDPOINT", "S3_ENDPOINT").rstrip("/"),
            hmw_s3_region=_env("HMW_S3_REGION", "S3_REGION", default="us-west-004") or "us-west-004",
            hmw_s3_bucket=_env("HMW_S3_BUCKET", "S3_BUCKET"),
            hmw_s3_access_key=_env("HMW_S3_ACCESS_KEY", "S3_ACCESS_KEY"),
            hmw_s3_secret_key=_env("HMW_S3_SECRET_KEY", "S3_SECRET_KEY"),
            hmw_s3_public_url=_env("HMW_S3_PUBLIC_URL", "S3_PUBLIC_URL").rstrip("/"),
            hmw_s3_prefix=_env("HMW_S3_PREFIX", "S3_PREFIX", default="comics/zh").strip("/")
            or "comics/zh",
            hmw_avif_quality=max(1, min(_int("HMW_AVIF_QUALITY", 50), 100)),
            hmw_convert_workers=max(1, min(_int("HMW_CONVERT_WORKERS", 1), 4)),
            hmw_upload_workers=max(1, min(_int("HMW_UPLOAD_WORKERS", 6), 32)),
            hmw_request_timeout=max(_int("HMW_REQUEST_TIMEOUT", 180), 1),
            ai_enabled=_bool("MANGA_AI_ENABLED", False),
            ai_refine_prompt=_env("MANGA_AI_REFINE_PROMPT"),
            database_url=database_url,
            data_dir=str(data_dir),
            temp_dir=_env("MANGA_TEMP_DIR", "TEMP_DIR", default=str(data_dir / "tmp")),
        )

    @property
    def binding_list(self) -> list[SourceBinding]:
        return parse_source_bindings(list(self.source_bindings), legacy_chats=self.tg_source_chats)

    @property
    def source_chat_list(self) -> list[str]:
        refs = binding_chat_refs(self.binding_list)
        if refs:
            return refs
        return [item.strip() for item in self.tg_source_chats.split(",") if item.strip()]

    @property
    def allowed_sender_id_set(self) -> set[int]:
        values: set[int] = set()
        for item in self.tg_allowed_sender_ids.split(","):
            try:
                if item.strip():
                    values.add(int(item.strip()))
            except ValueError:
                continue
        return values

    def build_cfbed_upload_url(self) -> str:
        raw = self.cfbed_upload_url.strip()
        if not raw:
            raise ValueError("CFBED_UPLOAD_URL is required")
        if not raw.startswith(("http://", "https://")):
            raise ValueError("CFBED_UPLOAD_URL must start with http:// or https://")
        parsed = urlparse(raw)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if self.cfbed_auth_code and not any(key.lower() == "authcode" for key in query):
            query["authCode"] = self.cfbed_auth_code
        extra = self.cfbed_extra_query.strip().lstrip("?&")
        if extra:
            for key, value in parse_qsl(extra, keep_blank_values=True):
                if key and key not in query:
                    query[key] = value
        return urlunparse(parsed._replace(query=urlencode(query)))

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.temp_dir).mkdir(parents=True, exist_ok=True)
        Path(self.telegram_session_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            raw_path = self.database_url[len("sqlite:///"):]
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _load_saved() -> dict[str, Any]:
    raw = read_json_safe(config_path(), {})
    if not isinstance(raw, dict):
        return {}
    result = dict(raw)
    for name in SECRET_FIELDS:
        value = result.get(name)
        if isinstance(value, str) and value:
            try:
                result[name] = decrypt_secret(value) or ""
            except Exception:
                logger.warning("无法解密漫画配置字段 %s，将使用环境变量", name)
                result.pop(name, None)
    return result


def load_manga_settings() -> MangaSettings:
    values = asdict(MangaSettings.from_environment())
    for name, value in _load_saved().items():
        if name in values and value is not None:
            values[name] = value
    try:
        values["telegram_api_id"] = int(values["telegram_api_id"]) if values.get("telegram_api_id") else None
    except (TypeError, ValueError):
        values["telegram_api_id"] = None
    values["source_bindings"] = tuple(
        serialize_source_bindings(parse_source_bindings(values.get("source_bindings"), legacy_chats=str(values.get("tg_source_chats") or "")))
    )
    return MangaSettings(**values)


def save_manga_settings(updates: dict[str, Any]) -> MangaSettings:
    current = load_manga_settings()
    allowed = {item.name for item in fields(MangaSettings)}
    clean: dict[str, Any] = {}
    for name, value in updates.items():
        if name not in allowed or value is None:
            continue
        if name in SECRET_FIELDS and (not isinstance(value, str) or value.strip() in {"", MASKED_SECRET}):
            continue
        if name == "source_bindings":
            clean[name] = tuple(serialize_source_bindings(parse_source_bindings(value)))
            continue
        clean[name] = value

    if "source_bindings" in clean:
        clean["tg_source_chats"] = ",".join(binding_chat_refs(parse_source_bindings(clean["source_bindings"])))
    if "outbound_preview_count" in clean:
        try:
            clean["outbound_preview_count"] = max(1, min(int(clean["outbound_preview_count"]), 10))
        except (TypeError, ValueError):
            clean["outbound_preview_count"] = 4
    if "outbound_button_text" in clean:
        text = str(clean["outbound_button_text"] or "").strip()[:40]
        clean["outbound_button_text"] = text or "点击阅读"
    if "outbound_channel" in clean:
        clean["outbound_channel"] = str(clean["outbound_channel"] or "").strip()
    if "outbound_site_base" in clean:
        clean["outbound_site_base"] = str(clean["outbound_site_base"] or "").strip().rstrip("/")
    if "outbound_video_block_keywords" in clean:
        clean["outbound_video_block_keywords"] = str(clean["outbound_video_block_keywords"] or "")[:4000]
    if "outbound_video_allow_keywords" in clean:
        clean["outbound_video_allow_keywords"] = str(clean["outbound_video_allow_keywords"] or "")[:4000]
    for name in ("outbound_video_min_seconds", "outbound_video_max_seconds"):
        if name in clean:
            try:
                clean[name] = max(0, min(int(clean[name] or 0), 86400))
            except (TypeError, ValueError):
                clean[name] = 0
    if "ehentai_search" in clean:
        clean["ehentai_search"] = str(clean["ehentai_search"] or "")[:4000]
    if "ehentai_cats" in clean:
        digits = "".join(ch for ch in str(clean["ehentai_cats"] or "") if ch.isdigit())
        clean["ehentai_cats"] = digits or "0"
    if "ehentai_cookie" in clean:
        clean["ehentai_cookie"] = str(clean["ehentai_cookie"] or "").strip()
    if "ehentai_max_pages" in clean:
        try:
            clean["ehentai_max_pages"] = max(1, min(int(clean["ehentai_max_pages"] or 400), 2000))
        except (TypeError, ValueError):
            clean["ehentai_max_pages"] = 400
    if "ehentai_search_pages" in clean:
        try:
            clean["ehentai_search_pages"] = max(1, min(int(clean["ehentai_search_pages"] or 1), 10))
        except (TypeError, ValueError):
            clean["ehentai_search_pages"] = 1
    if "ehentai_delay_seconds" in clean:
        try:
            clean["ehentai_delay_seconds"] = max(float(clean["ehentai_delay_seconds"] or 0), 0.0)
        except (TypeError, ValueError):
            clean["ehentai_delay_seconds"] = 1.2
    if "ehentai_gallery_delay_seconds" in clean:
        try:
            clean["ehentai_gallery_delay_seconds"] = max(
                float(clean["ehentai_gallery_delay_seconds"] or 0), 0.0
            )
        except (TypeError, ValueError):
            clean["ehentai_gallery_delay_seconds"] = 5.0
    if "ehentai_poll_seconds" in clean:
        try:
            clean["ehentai_poll_seconds"] = max(float(clean["ehentai_poll_seconds"] or 60), 60.0)
        except (TypeError, ValueError):
            clean["ehentai_poll_seconds"] = 300.0
    if "ehentai_translation_url" in clean:
        url = str(clean["ehentai_translation_url"] or "").strip()[:500]
        if url and not url.startswith(("http://", "https://")):
            url = ""
        clean["ehentai_translation_url"] = url
    if "hmw_api_url" in clean:
        url = str(clean["hmw_api_url"] or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = "https://www.hmw.app/api/publisher"
        clean["hmw_api_url"] = url or "https://www.hmw.app/api/publisher"
    if "hmw_s3_endpoint" in clean:
        clean["hmw_s3_endpoint"] = str(clean["hmw_s3_endpoint"] or "").strip().rstrip("/")
    if "hmw_s3_region" in clean:
        clean["hmw_s3_region"] = str(clean["hmw_s3_region"] or "").strip() or "us-west-004"
    if "hmw_s3_bucket" in clean:
        clean["hmw_s3_bucket"] = str(clean["hmw_s3_bucket"] or "").strip()
    if "hmw_s3_public_url" in clean:
        clean["hmw_s3_public_url"] = str(clean["hmw_s3_public_url"] or "").strip().rstrip("/")
    if "hmw_s3_prefix" in clean:
        prefix = str(clean["hmw_s3_prefix"] or "").strip().strip("/")
        clean["hmw_s3_prefix"] = prefix or "comics/zh"
    if "hmw_avif_quality" in clean:
        try:
            clean["hmw_avif_quality"] = max(1, min(int(clean["hmw_avif_quality"] or 50), 100))
        except (TypeError, ValueError):
            clean["hmw_avif_quality"] = 50
    if "hmw_convert_workers" in clean:
        try:
            clean["hmw_convert_workers"] = max(1, min(int(clean["hmw_convert_workers"] or 1), 4))
        except (TypeError, ValueError):
            clean["hmw_convert_workers"] = 1
    if "hmw_upload_workers" in clean:
        try:
            clean["hmw_upload_workers"] = max(1, min(int(clean["hmw_upload_workers"] or 6), 32))
        except (TypeError, ValueError):
            clean["hmw_upload_workers"] = 6
    if "hmw_request_timeout" in clean:
        try:
            clean["hmw_request_timeout"] = max(int(clean["hmw_request_timeout"] or 180), 1)
        except (TypeError, ValueError):
            clean["hmw_request_timeout"] = 180
    if "ai_refine_prompt" in clean:
        clean["ai_refine_prompt"] = str(clean["ai_refine_prompt"] or "").replace(
            "\r\n", "\n"
        )[:8000]

    effective = replace(current, **clean)
    stored = _load_saved()
    stored.update(clean)
    persisted = dict(stored)
    for name in SECRET_FIELDS:
        raw = persisted.get(name)
        if raw:
            persisted[name] = encrypt_secret(str(raw))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(config_path(), persisted)
    return effective


def manga_config_public(
    settings: MangaSettings | None = None, *, reveal: bool = False
) -> dict[str, Any]:
    current = settings or load_manga_settings()
    value = asdict(current)
    for name in SECRET_FIELDS:
        raw = str(value.get(name) or "")
        value[name] = raw if reveal else None
        value[f"{name}_set"] = bool(raw)
    value["database_url"] = None
    value["data_dir"] = None
    value["temp_dir"] = None
    value["source_bindings"] = serialize_source_bindings(current.binding_list)
    from backend.services.ai.client import ai_available
    from backend.services.ai.tasks import TASK_MANGA_REFINE, default_prompt

    value["ai_refine_prompt_default"] = default_prompt(TASK_MANGA_REFINE)
    value["ai_model_configured"] = ai_available()
    return value
