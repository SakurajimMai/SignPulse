from __future__ import annotations

from dataclasses import dataclass


class ConfigError(ValueError):
    pass


REQUIRED_FIELDS = (
    "api_url",
    "publisher_token",
    "s3_endpoint",
    "s3_bucket",
    "s3_access_key",
    "s3_secret_key",
    "s3_public_url",
)


@dataclass(frozen=True, slots=True)
class UploaderConfig:
    api_url: str
    publisher_token: str
    s3_endpoint: str
    s3_region: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_public_url: str
    s3_prefix: str = "comics/zh"
    avif_quality: int = 50
    avif_background: str = "#FFFFFF"
    upload_workers: int = 4
    request_timeout: int = 30
    upload_retries: int = 3
    cache_control: str = "public, max-age=300, must-revalidate"
    convert_workers: int = 1

    @classmethod
    def from_manga_settings(cls, settings) -> "UploaderConfig":
        quality = _clamp_int(getattr(settings, "hmw_avif_quality", 50), 1, 100, 50)
        upload_workers = _clamp_int(getattr(settings, "hmw_upload_workers", 6), 1, 32, 6)
        timeout = max(int(getattr(settings, "hmw_request_timeout", 180) or 180), 1)
        convert_workers = _clamp_int(getattr(settings, "hmw_convert_workers", 1), 1, 4, 1)
        cfg = cls(
            api_url=str(getattr(settings, "hmw_api_url", "") or "").strip().rstrip("/"),
            publisher_token=str(getattr(settings, "hmw_publisher_token", "") or "").strip(),
            s3_endpoint=str(getattr(settings, "hmw_s3_endpoint", "") or "").strip().rstrip("/"),
            s3_region=str(getattr(settings, "hmw_s3_region", "") or "").strip() or "auto",
            s3_bucket=str(getattr(settings, "hmw_s3_bucket", "") or "").strip(),
            s3_access_key=str(getattr(settings, "hmw_s3_access_key", "") or "").strip(),
            s3_secret_key=str(getattr(settings, "hmw_s3_secret_key", "") or "").strip(),
            s3_public_url=str(getattr(settings, "hmw_s3_public_url", "") or "").strip().rstrip("/"),
            s3_prefix=str(getattr(settings, "hmw_s3_prefix", "") or "").strip().strip("/")
            or "comics/zh",
            avif_quality=quality,
            upload_workers=upload_workers,
            request_timeout=timeout,
            convert_workers=convert_workers,
        )
        missing = [name for name in REQUIRED_FIELDS if not str(getattr(cfg, name) or "").strip()]
        if missing:
            labels = "、".join(missing)
            raise ConfigError(f"HMW 配置不完整，缺少：{labels}")
        return cfg

    def is_configured(self) -> bool:
        return all(str(getattr(self, name) or "").strip() for name in REQUIRED_FIELDS)


def _clamp_int(value, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


def hmw_configured(settings) -> bool:
    try:
        UploaderConfig.from_manga_settings(settings)
        return True
    except ConfigError:
        return False
