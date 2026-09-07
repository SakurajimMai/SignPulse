from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from backend.core.config import _merged_env
from backend.core.config import get_settings as get_panel_settings
from backend.utils.atomic_io import read_json_safe, write_json_atomic
from tg_signer.security import decrypt_secret, encrypt_secret

logger = logging.getLogger("backend.games.config")

SECRET_FIELDS = {
    "wp_app_password",
    "extract_passwords",
    "pack_password",
    "baidu_cookie",
    "pikpak_password",
    "pikpak_refresh_token",
    "terabox_cookie",
    "quark_cookie",
    "openlist_token",
}
# 解压/重打包密码会印在文章里，面板需要明文回显，但仍加密落盘。
VISIBLE_SECRET_FIELDS = {
    "extract_passwords",
    "pack_password",
}
MASKED_SECRET = "********"

DEFAULT_WP_URL = "https://www.ixacg.top"
DEFAULT_CATEGORIES = "640,637"
DEFAULT_TAGS = "SLG"
DEFAULT_AD_KEYWORDS = (
    "广告,advertisement,ads,www.,加群,推广,宣传,关注公众号,防失联,"
    "破解说明,扫码,官网下载,点击下载"
)
DEFAULT_EXTRACT_PASSWORDS = "sakuramai,ixacg.top"
DEFAULT_APATE_GUIDE_URL = "https://www.ixacg.top/16403.html"
DEFAULT_PAY_EXTRA_TEMPLATE = (
    "解压密码：{pack_password}\n"
    "\n"
    "网盘提取码：{extract}\n"
    "\n"
    "百度/夸克网盘文件为 Apate 伪装的 MP4，请先用 {apate} 还原再解压。"
)
NULLABLE_FLOAT_FIELDS = {
    "wp_vip1_price",
    "wp_vip2_price",
    "wp_vip1_points",
    "wp_vip2_points",
}
# 4 GiB，对应 7z -v4g；旧默认 3800 会在加载时迁到此值。
SPLIT_VOLUME_MB_DEFAULT = 4096
SPLIT_VOLUME_MB_MIN = 256
SPLIT_VOLUME_MB_MAX = 4096
LEGACY_SPLIT_VOLUME_MB = 3800


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


def optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return None


def _base_dir() -> Path:
    return Path(
        _env(
            "GAMES_DATA_DIR",
            "APP_DATA_DIR",
            "DATA_DIR",
            default=str(get_panel_settings().resolve_base_dir()),
        )
    )


def config_path() -> Path:
    raw = _env("GAMES_CONFIG_FILE")
    return Path(raw).expanduser() if raw else _base_dir() / ".games_config.json"


@dataclass(frozen=True)
class GamesSettings:
    """WordPress 游戏发布配置。密钥落盘时加密。"""

    wp_url: str = DEFAULT_WP_URL
    wp_user: str = ""
    wp_app_password: str = ""
    wp_default_categories: str = DEFAULT_CATEGORIES
    wp_default_tags: str = DEFAULT_TAGS
    wp_pay_enabled: bool = True
    wp_pay_modo: str = "0"
    wp_pay_price: float = 5.0
    wp_points_price: float = 20.0
    wp_vip1_price: float | None = None
    wp_vip2_price: float | None = None
    wp_vip1_points: float | None = None
    wp_vip2_points: float | None = None
    wp_pay_extra_template: str = DEFAULT_PAY_EXTRA_TEMPLATE
    wp_apate_url: str = DEFAULT_APATE_GUIDE_URL
    wp_status: str = "publish"
    telegram_account_name: str = ""
    telegram_source_channels: str = "Zhzbzx"
    auto_publish_enabled: bool = False
    telegram_poll_seconds: int = 30
    telegram_backfill_limit: int = 0
    auto_retry_limit: int = 3
    extract_passwords: str = DEFAULT_EXTRACT_PASSWORDS
    pack_password: str = "sakuramai"
    split_volume_mb: int = SPLIT_VOLUME_MB_DEFAULT
    ad_keywords: str = DEFAULT_AD_KEYWORDS
    apate_enabled: bool = True
    apate_bin: str = "apate"
    baidu_enabled: bool = True
    baidu_cookie: str = ""
    baidu_remote_dir: str = "/games"
    pikpak_username: str = ""
    pikpak_password: str = ""
    pikpak_refresh_token: str = ""
    pikpak_folder_id: str = ""
    pikpak_remote_dir: str = "/games"
    terabox_cookie: str = ""
    terabox_remote_dir: str = "/games"
    quark_cookie: str = ""
    quark_folder_id: str = ""
    quark_remote_dir: str = "/games"
    openlist_url: str = ""
    openlist_token: str = ""
    openlist_baidu_path: str = ""
    openlist_pikpak_path: str = ""
    openlist_terabox_path: str = ""
    openlist_quark_path: str = ""
    cleanup_after_publish: bool = True
    ai_enabled: bool = False
    ai_refine_prompt: str = ""
    ai_skip_non_games: bool = True
    data_dir: str = ""

    @classmethod
    def from_environment(cls) -> "GamesSettings":
        data_dir = _base_dir()
        return cls(
            wp_url=_env("GAMES_WP_URL", default=DEFAULT_WP_URL).rstrip("/")
            or DEFAULT_WP_URL,
            wp_user=_env("GAMES_WP_USER"),
            wp_app_password=_env("GAMES_WP_APP_PASSWORD"),
            wp_default_categories=_env(
                "GAMES_WP_CATEGORIES", default=DEFAULT_CATEGORIES
            )
            or DEFAULT_CATEGORIES,
            wp_default_tags=_env("GAMES_WP_TAGS", default=DEFAULT_TAGS) or DEFAULT_TAGS,
            wp_pay_enabled=_bool("GAMES_WP_PAY_ENABLED", True),
            wp_pay_modo=_env("GAMES_WP_PAY_MODO", default="0") or "0",
            wp_pay_price=max(_float("GAMES_WP_PAY_PRICE", 5.0), 0.0),
            wp_points_price=max(_float("GAMES_WP_POINTS_PRICE", 20.0), 0.0),
            wp_vip1_price=optional_float(_env("GAMES_WP_VIP1_PRICE") or None),
            wp_vip2_price=optional_float(_env("GAMES_WP_VIP2_PRICE") or None),
            wp_vip1_points=optional_float(_env("GAMES_WP_VIP1_POINTS") or None),
            wp_vip2_points=optional_float(_env("GAMES_WP_VIP2_POINTS") or None),
            wp_pay_extra_template=_env(
                "GAMES_WP_PAY_EXTRA", default=DEFAULT_PAY_EXTRA_TEMPLATE
            )
            or DEFAULT_PAY_EXTRA_TEMPLATE,
            wp_apate_url=_env("GAMES_WP_APATE_URL", default=DEFAULT_APATE_GUIDE_URL)
            or DEFAULT_APATE_GUIDE_URL,
            wp_status=_env("GAMES_WP_STATUS", default="publish") or "publish",
            telegram_account_name=_env(
                "GAMES_TELEGRAM_ACCOUNT", "MANGA_TELEGRAM_ACCOUNT"
            ),
            telegram_source_channels=_env("GAMES_TELEGRAM_CHANNELS", default="Zhzbzx")
            or "Zhzbzx",
            auto_publish_enabled=_bool("GAMES_AUTO_PUBLISH_ENABLED", False),
            telegram_poll_seconds=max(
                10, min(_int("GAMES_TELEGRAM_POLL_SECONDS", 30), 3600)
            ),
            telegram_backfill_limit=max(
                0, min(_int("GAMES_TELEGRAM_BACKFILL_LIMIT", 0), 100)
            ),
            auto_retry_limit=max(1, min(_int("GAMES_AUTO_RETRY_LIMIT", 3), 10)),
            extract_passwords=_env(
                "GAMES_EXTRACT_PASSWORDS", default=DEFAULT_EXTRACT_PASSWORDS
            )
            or DEFAULT_EXTRACT_PASSWORDS,
            pack_password=_env("GAMES_PACK_PASSWORD", default="sakuramai")
            or "sakuramai",
            split_volume_mb=normalize_split_volume_mb(
                _int("GAMES_SPLIT_VOLUME_MB", SPLIT_VOLUME_MB_DEFAULT)
            ),
            ad_keywords=_env("GAMES_AD_KEYWORDS", default=DEFAULT_AD_KEYWORDS)
            or DEFAULT_AD_KEYWORDS,
            apate_enabled=_bool("GAMES_APATE_ENABLED", True),
            apate_bin=_env("GAMES_APATE_BIN", default="apate") or "apate",
            baidu_enabled=_bool("GAMES_BAIDU_ENABLED", True),
            baidu_cookie=_env("GAMES_BAIDU_COOKIE"),
            baidu_remote_dir=_env("GAMES_BAIDU_DIR", default="/games") or "/games",
            pikpak_username=_env("GAMES_PIKPAK_USER"),
            pikpak_password=_env("GAMES_PIKPAK_PASSWORD"),
            pikpak_refresh_token=_env("GAMES_PIKPAK_REFRESH_TOKEN"),
            pikpak_folder_id=_env("GAMES_PIKPAK_FOLDER_ID"),
            pikpak_remote_dir=_env("GAMES_PIKPAK_DIR", default="/games") or "/games",
            terabox_cookie=_env("GAMES_TERABOX_COOKIE"),
            terabox_remote_dir=_env("GAMES_TERABOX_DIR", default="/games") or "/games",
            quark_cookie=_env("GAMES_QUARK_COOKIE"),
            quark_folder_id=_env("GAMES_QUARK_FOLDER_ID"),
            quark_remote_dir=_env("GAMES_QUARK_DIR", default="/games") or "/games",
            openlist_url=_env("GAMES_OPENLIST_URL").rstrip("/"),
            openlist_token=_env("GAMES_OPENLIST_TOKEN"),
            openlist_baidu_path=_env("GAMES_OPENLIST_BAIDU_PATH"),
            openlist_pikpak_path=_env("GAMES_OPENLIST_PIKPAK_PATH"),
            openlist_terabox_path=_env("GAMES_OPENLIST_TERABOX_PATH"),
            openlist_quark_path=_env("GAMES_OPENLIST_QUARK_PATH"),
            cleanup_after_publish=_bool("GAMES_CLEANUP_AFTER_PUBLISH", True),
            ai_enabled=_bool("GAMES_AI_ENABLED", False),
            ai_refine_prompt=_env("GAMES_AI_REFINE_PROMPT"),
            ai_skip_non_games=_bool("GAMES_AI_SKIP_NON_GAMES", True),
            data_dir=str(data_dir),
        )

    @property
    def category_ids(self) -> list[int]:
        return parse_int_list(self.wp_default_categories)

    @property
    def tag_names(self) -> list[str]:
        return split_csv(self.wp_default_tags)

    @property
    def password_list(self) -> list[str]:
        return split_csv(self.extract_passwords)

    @property
    def source_channel_list(self) -> list[str]:
        return split_csv(self.telegram_source_channels)

    @property
    def ad_keyword_list(self) -> list[str]:
        return split_csv(self.ad_keywords)

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)


def split_csv(raw: str | None) -> list[str]:
    text = (
        str(raw or "")
        .replace("，", ",")
        .replace("、", ",")
        .replace("\r", ",")
        .replace("\n", ",")
    )
    seen: list[str] = []
    for item in text.split(","):
        value = item.strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def normalize_remote_dir(raw: Any) -> str:
    """远程目录允许用户粘贴 URL 编码路径，统一成 /a/b。"""
    value = unquote(str(raw or "").strip().replace("\\", "/"))
    value = "/".join(part for part in value.split("/") if part)
    return f"/{value}" if value else ""


def normalize_split_volume_mb(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = SPLIT_VOLUME_MB_DEFAULT
    if value == LEGACY_SPLIT_VOLUME_MB:
        value = SPLIT_VOLUME_MB_DEFAULT
    return max(SPLIT_VOLUME_MB_MIN, min(value, SPLIT_VOLUME_MB_MAX))


def parse_int_list(raw: str | None) -> list[int]:
    values: list[int] = []
    for item in split_csv(raw):
        try:
            number = int(item)
        except ValueError:
            continue
        if number not in values:
            values.append(number)
    return values


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
                logger.warning("无法解密游戏配置字段 %s，将使用环境变量", name)
                result.pop(name, None)
    return result


def load_games_settings() -> GamesSettings:
    values = asdict(GamesSettings.from_environment())
    for name, value in _load_saved().items():
        if name not in values:
            continue
        if name in NULLABLE_FLOAT_FIELDS:
            values[name] = optional_float(value)
            continue
        if value is not None:
            values[name] = value
    values["data_dir"] = str(_base_dir())
    values["split_volume_mb"] = normalize_split_volume_mb(values.get("split_volume_mb"))
    for key in (
        "baidu_remote_dir",
        "pikpak_remote_dir",
        "terabox_remote_dir",
        "quark_remote_dir",
        "openlist_baidu_path",
        "openlist_pikpak_path",
        "openlist_terabox_path",
        "openlist_quark_path",
    ):
        values[key] = normalize_remote_dir(values.get(key))
    return GamesSettings(**values)


def save_games_settings(updates: dict[str, Any]) -> GamesSettings:
    current = load_games_settings()
    allowed = {item.name for item in fields(GamesSettings)}
    clean: dict[str, Any] = {}
    for name, value in updates.items():
        if name not in allowed:
            continue
        if name in NULLABLE_FLOAT_FIELDS:
            clean[name] = optional_float(value)
            continue
        if value is None:
            continue
        if name in VISIBLE_SECRET_FIELDS:
            if not isinstance(value, str) or value.strip() == MASKED_SECRET:
                continue
            clean[name] = value.strip()
            continue
        if name in SECRET_FIELDS and (
            not isinstance(value, str) or value.strip() in {"", MASKED_SECRET}
        ):
            continue
        clean[name] = value

    if "wp_url" in clean:
        url = str(clean["wp_url"] or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        clean["wp_url"] = url or DEFAULT_WP_URL
    if "wp_status" in clean:
        status = str(clean["wp_status"] or "publish").strip().lower()
        clean["wp_status"] = (
            status if status in {"publish", "draft", "private"} else "publish"
        )
    if "wp_pay_price" in clean:
        try:
            clean["wp_pay_price"] = max(float(clean["wp_pay_price"] or 0), 0.0)
        except (TypeError, ValueError):
            clean["wp_pay_price"] = 5.0
    if "wp_points_price" in clean:
        try:
            clean["wp_points_price"] = max(float(clean["wp_points_price"] or 0), 0.0)
        except (TypeError, ValueError):
            clean["wp_points_price"] = 20.0
    if "ai_refine_prompt" in clean:
        clean["ai_refine_prompt"] = str(clean["ai_refine_prompt"] or "").replace(
            "\r\n", "\n"
        )[:8000]
    if "wp_pay_extra_template" in clean:
        template = str(clean["wp_pay_extra_template"] or "").replace("\r\n", "\n")
        clean["wp_pay_extra_template"] = template.strip() or DEFAULT_PAY_EXTRA_TEMPLATE
    if "wp_apate_url" in clean:
        url = str(clean["wp_apate_url"] or "").strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        clean["wp_apate_url"] = url or DEFAULT_APATE_GUIDE_URL
    if "wp_pay_modo" in clean:
        modo = str(clean["wp_pay_modo"] or "0").strip().lower()
        if modo in {"points", "point", "积分"}:
            clean["wp_pay_modo"] = "points"
        else:
            clean["wp_pay_modo"] = "0"
    if "openlist_url" in clean:
        clean["openlist_url"] = str(clean["openlist_url"] or "").strip().rstrip("/")
    if "apate_bin" in clean:
        clean["apate_bin"] = str(clean["apate_bin"] or "apate").strip() or "apate"
    if "wp_default_categories" in clean:
        clean["wp_default_categories"] = (
            ",".join(
                str(item)
                for item in parse_int_list(str(clean["wp_default_categories"]))
            )
            or DEFAULT_CATEGORIES
        )
    if "telegram_source_channels" in clean:
        clean["telegram_source_channels"] = ",".join(
            split_csv(str(clean["telegram_source_channels"]))
        )
    for key, default, lower, upper in (
        ("telegram_poll_seconds", 30, 10, 3600),
        ("telegram_backfill_limit", 0, 0, 100),
        ("auto_retry_limit", 3, 1, 10),
    ):
        if key in clean:
            try:
                clean[key] = max(lower, min(int(clean[key]), upper))
            except (TypeError, ValueError):
                clean[key] = default
    if "split_volume_mb" in clean:
        clean["split_volume_mb"] = normalize_split_volume_mb(clean["split_volume_mb"])
    for key in (
        "baidu_remote_dir",
        "pikpak_remote_dir",
        "terabox_remote_dir",
        "quark_remote_dir",
        "openlist_baidu_path",
        "openlist_pikpak_path",
        "openlist_terabox_path",
        "openlist_quark_path",
    ):
        if key in clean:
            clean[key] = normalize_remote_dir(clean[key])

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


def games_config_public(
    settings: GamesSettings | None = None, *, reveal: bool = False
) -> dict[str, Any]:
    current = settings or load_games_settings()
    value = asdict(current)
    for name in SECRET_FIELDS:
        raw = str(value.get(name) or "")
        if name in VISIBLE_SECRET_FIELDS or reveal:
            value[name] = raw
        else:
            value[name] = None
        value[f"{name}_set"] = bool(raw)
    value["data_dir"] = None
    from backend.services.ai.client import ai_available
    from backend.services.ai.tasks import TASK_GAMES_REFINE, default_prompt

    value["ai_refine_prompt_default"] = default_prompt(TASK_GAMES_REFINE)
    value["ai_model_configured"] = ai_available()
    return value
