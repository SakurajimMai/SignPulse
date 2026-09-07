from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import boto3
import httpx
from botocore.config import Config as BotoConfig

from backend.utils.outbound import (
    botocore_config_with_proxy,
    httpx_client_kwargs,
)

from .config import UploaderConfig


def _path_url(base: str, key: str) -> str:
    encoded = "/".join(quote(part, safe="") for part in key.strip("/").split("/"))
    return f"{base.rstrip('/')}/{encoded}"


def _chapter_label(number: float) -> str:
    return f"{float(number):g}"


class S3Storage:
    def __init__(self, config: UploaderConfig):
        self.config = config
        addressing_style = (
            "virtual" if "backblazeb2.com" in config.s3_endpoint.casefold() else "auto"
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint,
            region_name=config.s3_region,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            config=botocore_config_with_proxy(
                BotoConfig(
                    retries={
                        "max_attempts": config.upload_retries,
                        "mode": "standard",
                    },
                    s3={"addressing_style": addressing_style},
                ),
                endpoint_url=config.s3_endpoint,
            ),
        )

    def cover_key(self, manga_slug: str) -> str:
        return f"{self.config.s3_prefix}/{manga_slug}/cover.avif"

    def chapter_key(self, manga_slug: str, chapter_number: float, filename: str) -> str:
        return (
            f"{self.config.s3_prefix}/{manga_slug}/"
            f"{_chapter_label(chapter_number)}/{filename}"
        )

    def public_url(self, key: str) -> str:
        return _path_url(self.config.s3_public_url, key)

    def key_from_public_url(self, url: str) -> str | None:
        base = urlsplit(self.config.s3_public_url.rstrip("/"))
        target = urlsplit(url)
        if target.scheme != base.scheme or target.netloc != base.netloc:
            return None
        base_path = base.path.rstrip("/")
        target_path = target.path
        if base_path and not target_path.startswith(f"{base_path}/"):
            return None
        relative = target_path[len(base_path) :].lstrip("/")
        return "/".join(unquote(part) for part in relative.split("/")) if relative else None

    def check_connection(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.config.s3_bucket)
            return True
        except Exception:
            return False

    def upload_file(self, source: str | Path, key: str) -> str:
        error: Exception | None = None
        for attempt in range(self.config.upload_retries):
            try:
                self.client.upload_file(
                    Filename=str(Path(source)),
                    Bucket=self.config.s3_bucket,
                    Key=key,
                    ExtraArgs={
                        "ContentType": "image/avif",
                        "CacheControl": self.config.cache_control,
                    },
                )
                return self.public_url(key)
            except Exception as exc:
                error = exc
                if attempt + 1 < self.config.upload_retries:
                    time.sleep(0.2 * (attempt + 1))
        assert error is not None
        raise error

    def verify_public_url(self, url: str) -> bool:
        try:
            with httpx.Client(
                **httpx_client_kwargs(
                    follow_redirects=True,
                    timeout=self.config.request_timeout,
                )
            ) as client:
                response = client.head(url)
                if response.status_code == 405:
                    response = client.get(
                        url,
                        headers={"Range": "bytes=0-0"},
                    )
            return response.status_code < 400
        except httpx.HTTPError:
            return False

    def delete_keys(self, keys: list[str]) -> list[str]:
        errors: list[str] = []
        unique_keys = list(dict.fromkeys(key for key in keys if key))
        for offset in range(0, len(unique_keys), 1000):
            chunk = unique_keys[offset : offset + 1000]
            response = self.client.delete_objects(
                Bucket=self.config.s3_bucket,
                Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
            )
            errors.extend(
                f"{item.get('Key', '')}: {item.get('Message', item.get('Code', '删除失败'))}"
                for item in response.get("Errors", [])
            )
        return errors
