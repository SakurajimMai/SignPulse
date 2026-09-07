from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from backend.utils.outbound import botocore_config_with_proxy, httpx_client_kwargs

from .config import CoserSettings

logger = logging.getLogger("backend.coser.s3")


class CoserS3Error(RuntimeError):
    pass


def s3_configured(settings: CoserSettings) -> bool:
    return bool(
        str(settings.s3_endpoint or "").strip()
        and str(settings.s3_access_key or "").strip()
        and str(settings.s3_secret_key or "").strip()
        and str(settings.s3_bucket or "").strip()
    )


def _client(settings: CoserSettings):
    if not s3_configured(settings):
        raise CoserS3Error("请填写 S3 endpoint、Access Key、Secret Key 和 Bucket")
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except Exception as exc:
        raise CoserS3Error(f"S3 上传需要 boto3：{exc}") from exc
    endpoint = str(settings.s3_endpoint or "").strip()
    if endpoint and not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    region = str(settings.s3_region or "").strip() or "auto"
    addressing = (
        "virtual"
        if "backblazeb2.com" in endpoint.casefold()
        else "path"
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=str(settings.s3_access_key or "").strip(),
        aws_secret_access_key=str(settings.s3_secret_key or "").strip(),
        config=botocore_config_with_proxy(
            BotoConfig(
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": addressing},
            ),
            endpoint_url=endpoint,
        ),
    )


def _bucket(settings: CoserSettings) -> str:
    return str(settings.s3_bucket or "").strip()


def public_object_url(settings: CoserSettings, key: str) -> str:
    public = str(settings.s3_public_url or "").strip().rstrip("/")
    key = str(key or "").lstrip("/")
    if public:
        return f"{public}/{key}"
    endpoint = str(settings.s3_endpoint or "").strip().rstrip("/")
    bucket = _bucket(settings)
    return f"{endpoint}/{bucket}/{key}"


def probe_s3(settings: CoserSettings) -> dict[str, Any]:
    if not s3_configured(settings):
        return {"ok": False, "configured": False, "error": "未填写 S3"}
    try:
        client = _client(settings)
        client.head_bucket(Bucket=_bucket(settings))
        return {"ok": True, "configured": True, "error": ""}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": str(exc)}


def object_exists(settings: CoserSettings, key: str) -> bool:
    if not key or not s3_configured(settings):
        return False
    try:
        _client(settings).head_object(Bucket=_bucket(settings), Key=key)
        return True
    except Exception:
        return False


def count_prefix(settings: CoserSettings, prefix: str) -> int | None:
    if not prefix or not s3_configured(settings):
        return None
    folder = str(prefix).strip().strip("/") + "/"
    try:
        client = _client(settings)
        token: str | None = ""
        count = 0
        while token is not None:
            kwargs: dict[str, Any] = {
                "Bucket": _bucket(settings),
                "Prefix": folder,
            }
            if token:
                kwargs["ContinuationToken"] = token
            response = client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key.lower().endswith(".avif"):
                    count += 1
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated"):
                token = None
        return count
    except Exception:
        logger.warning("列举 S3 前缀失败：%s", prefix)
        return None


def verify_public_url(url: str, timeout: float = 15.0) -> bool:
    target = str(url or "").strip()
    if not target.startswith(("http://", "https://")):
        return False
    try:
        with httpx.Client(
            **httpx_client_kwargs(
                follow_redirects=True,
                timeout=timeout,
            )
        ) as client:
            response = client.head(target, headers={"Cache-Control": "no-cache"})
            if response.status_code < 400:
                return True
            if response.status_code in {400, 403, 405}:
                response = client.get(
                    target,
                    headers={"Range": "bytes=0-0", "Cache-Control": "no-cache"},
                )
            return response.status_code < 400
    except Exception:
        return False


def upload_file(
    settings: CoserSettings,
    path: Path,
    key: str,
    *,
    content_type: str = "image/avif",
) -> str:
    if not path.is_file():
        raise CoserS3Error(f"图片不存在：{path.name}")
    key = str(key or "").lstrip("/")
    if not key:
        raise CoserS3Error("S3 对象键为空")
    client = _client(settings)
    extra = {
        "ContentType": content_type or "image/avif",
        "CacheControl": "public, max-age=31536000, immutable",
    }
    try:
        # 从本地文件流式上传，避免把特殊路径塞进内存 Body 触发 IncompleteBody。
        client.upload_file(
            Filename=str(path),
            Bucket=_bucket(settings),
            Key=key,
            ExtraArgs=extra,
        )
    except Exception as exc:
        logger.exception("Coser S3 上传失败")
        raise CoserS3Error(f"S3 上传失败：{exc}") from exc
    return public_object_url(settings, key)
