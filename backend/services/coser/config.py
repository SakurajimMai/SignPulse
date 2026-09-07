from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from backend.core.config import _merged_env
from backend.core.config import get_settings as get_panel_settings
from backend.services.games.config import (
    GamesSettings,
    load_games_settings,
    normalize_remote_dir,
    normalize_split_volume_mb,
    split_csv,
)
from backend.utils.atomic_io import read_json_safe, write_json_atomic
from tg_signer.security import decrypt_secret, encrypt_secret

logger = logging.getLogger("backend.coser.config")

SECRET_FIELDS = {
    "site_password",
    "s3_access_key",
    "s3_secret_key",
    "extract_passwords",
    "pack_password",
}
VISIBLE_SECRET_FIELDS = {
    "extract_passwords",
    "pack_password",
}

DEFAULT_SITE_URL = "https://icoser.de"
DEFAULT_EXTRACT_PASSWORDS = "sakuramai,icoser.de"
DEFAULT_AD_KEYWORDS = (
    "广告,advertisement,ads,www.,加群,推广,宣传,关注公众号,防失联,"
    "破解说明,扫码,官网下载,点击下载"
)
DEFAULT_BAIDU_DIR = "/网站/icoser.de/coser"
DEFAULT_PIKPAK_DIR = "/site/icoser.de/coser"
DEFAULT_TERABOX_DIR = "/website/icoser.de/coser"
DEFAULT_QUARK_DIR = "/网站/icoser.de/coser"
DEFAULT_S3_PREFIX = "coser"


def normalize_s3_prefix(value: Any, default: str = DEFAULT_S3_PREFIX) -> str:
    text = str(value or "").strip().strip("/")
    if not text or text == "uploads":
        return default
    return text


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


def _base_dir() -> Path:
    return Path(
        _env(
            "COSER_DATA_DIR",
            "APP_DATA_DIR",
            "DATA_DIR",
            default=str(get_panel_settings().resolve_base_dir()),
        )
    )


def config_path() -> Path:
    raw = _env("COSER_CONFIG_FILE")
    return Path(raw).expanduser() if raw else _base_dir() / ".coser_config.json"


@dataclass(frozen=True)
class CoserSettings:
    """icoser.de 发布配置。网盘账号复用游戏发布；图片直传到本页配置的 S3。"""

    site_url: str = DEFAULT_SITE_URL
    site_email: str = ""
    site_password: str = ""
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = ""
    s3_region: str = "auto"
    s3_public_url: str = ""
    s3_prefix: str = DEFAULT_S3_PREFIX
    telegram_account_name: str = ""
    telegram_source_channels: str = ""
    extract_passwords: str = DEFAULT_EXTRACT_PASSWORDS
    pack_password: str = "sakuramai"
    split_volume_mb: int = 4096
    ad_keywords: str = DEFAULT_AD_KEYWORDS
    apate_enabled: bool = True
    baidu_remote_dir: str = DEFAULT_BAIDU_DIR
    pikpak_remote_dir: str = DEFAULT_PIKPAK_DIR
    terabox_remote_dir: str = DEFAULT_TERABOX_DIR
    quark_remote_dir: str = DEFAULT_QUARK_DIR
    cleanup_after_publish: bool = True
    data_dir: str = ""

    @classmethod
    def from_environment(cls) -> "CoserSettings":
        data_dir = _base_dir()
        return cls(
            site_url=_env("COSER_SITE_URL", default=DEFAULT_SITE_URL).rstrip("/")
            or DEFAULT_SITE_URL,
            site_email=_env("COSER_SITE_EMAIL"),
            site_password=_env("COSER_SITE_PASSWORD"),
            s3_endpoint=_env("COSER_S3_ENDPOINT").rstrip("/"),
            s3_access_key=_env("COSER_S3_ACCESS_KEY"),
            s3_secret_key=_env("COSER_S3_SECRET_KEY"),
            s3_bucket=_env("COSER_S3_BUCKET"),
            s3_region=_env("COSER_S3_REGION", default="auto") or "auto",
            s3_public_url=_env("COSER_S3_PUBLIC_URL").rstrip("/"),
            s3_prefix=normalize_s3_prefix(
                _env("COSER_S3_PREFIX", default=DEFAULT_S3_PREFIX)
            ),
            telegram_account_name=_env(
                "COSER_TELEGRAM_ACCOUNT", "GAMES_TELEGRAM_ACCOUNT"
            ),
            telegram_source_channels=_env("COSER_TELEGRAM_CHANNELS"),
            extract_passwords=_env(
                "COSER_EXTRACT_PASSWORDS", default=DEFAULT_EXTRACT_PASSWORDS
            )
            or DEFAULT_EXTRACT_PASSWORDS,
            pack_password=_env("COSER_PACK_PASSWORD", default="sakuramai")
            or "sakuramai",
            split_volume_mb=normalize_split_volume_mb(
                _int("COSER_SPLIT_VOLUME_MB", 4096)
            ),
            ad_keywords=_env("COSER_AD_KEYWORDS", default=DEFAULT_AD_KEYWORDS)
            or DEFAULT_AD_KEYWORDS,
            apate_enabled=_bool("COSER_APATE_ENABLED", True),
            baidu_remote_dir=_env("COSER_BAIDU_DIR", default=DEFAULT_BAIDU_DIR)
            or DEFAULT_BAIDU_DIR,
            pikpak_remote_dir=_env("COSER_PIKPAK_DIR", default=DEFAULT_PIKPAK_DIR)
            or DEFAULT_PIKPAK_DIR,
            terabox_remote_dir=_env("COSER_TERABOX_DIR", default=DEFAULT_TERABOX_DIR)
            or DEFAULT_TERABOX_DIR,
            quark_remote_dir=_env("COSER_QUARK_DIR", default=DEFAULT_QUARK_DIR)
            or DEFAULT_QUARK_DIR,
            cleanup_after_publish=_bool("COSER_CLEANUP_AFTER_PUBLISH", True),
            data_dir=str(data_dir),
        )

    @property
    def password_list(self) -> list[str]:
        return split_csv(self.extract_passwords)

    @property
    def ad_keyword_list(self) -> list[str]:
        return split_csv(self.ad_keywords)

    @property
    def source_channel_list(self) -> list[str]:
        return split_csv(self.telegram_source_channels)

    @property
    def api_base(self) -> str:
        return str(self.site_url or DEFAULT_SITE_URL).rstrip("/") + "/api/v1"


def overlay_games_settings(coser: CoserSettings) -> GamesSettings:
    """共用游戏发布的网盘凭证，只覆盖 Coser 远程目录与打包参数。"""
    games = load_games_settings()
    return replace(
        games,
        telegram_account_name=coser.telegram_account_name
        or games.telegram_account_name,
        baidu_remote_dir=normalize_remote_dir(coser.baidu_remote_dir)
        or DEFAULT_BAIDU_DIR,
        pikpak_remote_dir=normalize_remote_dir(coser.pikpak_remote_dir)
        or DEFAULT_PIKPAK_DIR,
        terabox_remote_dir=normalize_remote_dir(coser.terabox_remote_dir)
        or DEFAULT_TERABOX_DIR,
        quark_remote_dir=normalize_remote_dir(coser.quark_remote_dir)
        or DEFAULT_QUARK_DIR,
        apate_enabled=coser.apate_enabled,
        pack_password=coser.pack_password or games.pack_password,
        extract_passwords=coser.extract_passwords or games.extract_passwords,
        split_volume_mb=normalize_split_volume_mb(coser.split_volume_mb),
        ad_keywords=coser.ad_keywords or games.ad_keywords,
        cleanup_after_publish=False,
    )


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
                logger.warning("无法解密 Coser 配置字段 %s，将使用环境变量", name)
                result.pop(name, None)
    return result


def load_coser_settings() -> CoserSettings:
    values = asdict(CoserSettings.from_environment())
    for name, value in _load_saved().items():
        if name not in values:
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
    ):
        values[key] = normalize_remote_dir(values.get(key)) or values[key]
    values["site_url"] = str(values.get("site_url") or DEFAULT_SITE_URL).rstrip("/")
    values["s3_endpoint"] = str(values.get("s3_endpoint") or "").strip().rstrip("/")
    values["s3_public_url"] = str(values.get("s3_public_url") or "").strip().rstrip("/")
    values["s3_prefix"] = normalize_s3_prefix(values.get("s3_prefix"))
    values["s3_region"] = str(values.get("s3_region") or "auto").strip() or "auto"
    return CoserSettings(**values)


def save_coser_settings(updates: dict[str, Any]) -> CoserSettings:
    current = load_coser_settings()
    allowed = {item.name for item in fields(CoserSettings)}
    clean: dict[str, Any] = {}
    for name, value in updates.items():
        if name not in allowed or name == "data_dir":
            continue
        if value is None:
            continue
        if name in VISIBLE_SECRET_FIELDS:
            if not isinstance(value, str) or value.strip() == "********":
                continue
            clean[name] = value.strip()
            continue
        if name in SECRET_FIELDS and (
            not isinstance(value, str) or value.strip() in {"", "********"}
        ):
            continue
        clean[name] = value
    if "site_url" in clean:
        url = str(clean["site_url"] or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        clean["site_url"] = url or DEFAULT_SITE_URL
    if "s3_endpoint" in clean:
        endpoint = str(clean["s3_endpoint"] or "").strip().rstrip("/")
        if endpoint and not endpoint.startswith(("http://", "https://")):
            endpoint = "https://" + endpoint
        clean["s3_endpoint"] = endpoint
    if "s3_public_url" in clean:
        public = str(clean["s3_public_url"] or "").strip().rstrip("/")
        if public and not public.startswith(("http://", "https://")):
            public = "https://" + public
        clean["s3_public_url"] = public
    if "s3_prefix" in clean:
        clean["s3_prefix"] = normalize_s3_prefix(clean["s3_prefix"])
    if "s3_region" in clean:
        clean["s3_region"] = str(clean["s3_region"] or "auto").strip() or "auto"
    if "s3_bucket" in clean:
        clean["s3_bucket"] = str(clean["s3_bucket"] or "").strip()
    if "split_volume_mb" in clean:
        clean["split_volume_mb"] = normalize_split_volume_mb(clean["split_volume_mb"])
    for key in (
        "baidu_remote_dir",
        "pikpak_remote_dir",
        "terabox_remote_dir",
        "quark_remote_dir",
    ):
        if key in clean:
            clean[key] = normalize_remote_dir(clean[key]) or clean[key]
    effective = replace(current, **clean)
    stored = _load_saved()
    stored.update(clean)
    persisted = dict(stored)
    persisted.pop("data_dir", None)
    for name in SECRET_FIELDS:
        raw = persisted.get(name)
        if raw:
            persisted[name] = encrypt_secret(str(raw))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(config_path(), persisted)
    return effective


def coser_config_public(settings: CoserSettings, *, reveal: bool = False) -> dict[str, Any]:
    data = asdict(settings)
    data.pop("data_dir", None)
    for name in SECRET_FIELDS:
        present = bool(str(data.get(name) or "").strip())
        data[f"{name}_set"] = present
        if name in VISIBLE_SECRET_FIELDS:
            continue
        if not reveal:
            data[name] = None
    return data
