"""按采集源把图片发到图床 / FTP / SFTP / S3 兼容对象存储。"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from ftplib import FTP, FTP_TLS, error_perm
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from backend.utils.atomic_io import read_json_safe, update_json_atomic

from .config import MangaSettings
from .imgbed import ImgBedClient, ImgBedError, prepare_upload_image

logger = logging.getLogger(__name__)

OBJECT_TARGETS = ("s3", "r2", "b2")
UPLOAD_TARGETS = ("imgbed", "ftp", "sftp") + OBJECT_TARGETS
UPLOAD_SOURCES = ("telegram", "ehentai", "wnacg")
OUTBOUND_SOURCES = UPLOAD_SOURCES
_OBJECT_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}


class MediaStoreError(RuntimeError):
    pass


def normalize_upload_target(raw: Any) -> str:
    value = str(raw or "imgbed").strip().lower()
    return value if value in UPLOAD_TARGETS else "imgbed"


def upload_target_for(settings: MangaSettings, source: str) -> str:
    key = {
        "telegram": "upload_telegram",
        "ehentai": "upload_ehentai",
        "wnacg": "upload_wnacg",
    }.get(source, "upload_telegram")
    return normalize_upload_target(getattr(settings, key, "imgbed"))


def ftp_configured(settings: MangaSettings) -> bool:
    return bool(
        (settings.ftp_host or "").strip()
        and (settings.ftp_username or "").strip()
        and (settings.ftp_public_base or "").strip()
    )


def sftp_configured(settings: MangaSettings) -> bool:
    return bool(
        (settings.sftp_host or "").strip()
        and (settings.sftp_username or "").strip()
        and (settings.sftp_public_base or "").strip()
    )


def object_endpoint(settings: MangaSettings) -> str:
    raw = str(settings.s3_endpoint or "").strip()
    if raw and not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw.rstrip("/")


def object_configured(settings: MangaSettings) -> bool:
    return bool(
        (settings.s3_bucket or "").strip()
        and (settings.s3_access_key or "").strip()
        and (settings.s3_secret_key or "").strip()
        and (settings.s3_public_base or "").strip()
    )


_B2_ENDPOINT_REGION = re.compile(
    r"(?:^https?://)?s3\.([a-z0-9-]+)\.backblazeb2\.com(?:[:/]|$)",
    re.I,
)


def object_region(settings: MangaSettings, target: str) -> str:
    raw = str(settings.s3_region or "").strip()
    if raw:
        return raw
    if target == "r2":
        return "auto"
    if target == "b2":
        match = _B2_ENDPOINT_REGION.search(object_endpoint(settings))
        if match:
            return match.group(1)
        return "us-west-004"
    return "us-east-1"


def object_addressing(endpoint: str, target: str) -> str:
    text = (endpoint or "").casefold()
    if target == "b2" or "backblazeb2.com" in text:
        return "virtual"
    return "auto"


def upload_configured(settings: MangaSettings, source: str) -> bool:
    target = upload_target_for(settings, source)
    if target == "ftp":
        return ftp_configured(settings)
    if target == "sftp":
        return sftp_configured(settings)
    if target in OBJECT_TARGETS:
        if not object_configured(settings):
            return False
        if target in ("r2", "b2") and not object_endpoint(settings):
            return False
        return True
    return bool((settings.cfbed_upload_url or "").strip())


def upload_missing_message(settings: MangaSettings, source: str) -> str:
    target = upload_target_for(settings, source)
    label = {"telegram": "频道采集", "ehentai": "E-Hentai", "wnacg": "WNACG"}.get(source, source)
    if target == "ftp":
        return f"请先配置 FTP（主机、用户名、公网基址），{label} 当前走 FTP"
    if target == "sftp":
        return f"请先配置 SFTP（主机、用户名、公网基址），{label} 当前走 SFTP"
    if target in OBJECT_TARGETS:
        kind = {"s3": "Amazon S3", "r2": "Cloudflare R2", "b2": "Backblaze B2"}[target]
        extra = "；R2 / B2 还需 Endpoint" if target in ("r2", "b2") else ""
        return f"请先配置对象存储（Bucket、Access Key、Secret Key、公网基址{extra}），{label} 当前走 {kind}"
    return f"请先配置图床，{label} 图片要先上传再发主站"


def outbound_allows_source(settings: MangaSettings, source: str) -> bool:
    if not (settings.outbound_enabled and (settings.outbound_channel or "").strip()):
        return False
    flag = {
        "telegram": settings.outbound_telegram,
        "ehentai": settings.outbound_ehentai,
        "wnacg": settings.outbound_wnacg,
    }.get(source)
    if flag is None:
        return True
    return bool(flag)


def _clean_dir(raw: str) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    return text.strip("/")


def normalize_remote_dir(raw: Any, default: str = "manga") -> str:
    """保留以 / 开头的绝对路径；空值回落到 default。"""
    text = str(raw or "").strip().replace("\\", "/")
    absolute = text.startswith("/")
    text = text.strip("/")
    if not text:
        return default
    return f"/{text}" if absolute else text


def _album_clock(when: datetime | None = None) -> datetime:
    """FTP/SFTP 目录用采集当天的年/月/日，时区跟随 TZ（默认 Asia/Shanghai）。"""
    if when is not None:
        return when
    name = (os.getenv("TZ") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        zone = ZoneInfo(name)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.now(zone)


def build_album_rel_dir(*, when: datetime | None = None, album_id: str | None = None) -> str:
    stamp = _album_clock(when)
    year, month, day = stamp.year, stamp.month, stamp.day
    ident = str(album_id or uuid.uuid4()).strip() or str(uuid.uuid4())
    return f"{year:04d}/{month:02d}/{day:02d}/{ident}"


def sequenced_filename(index: int, ext: str) -> str:
    suffix = ext if str(ext).startswith(".") else f".{ext}" if ext else ".jpg"
    return f"{max(int(index), 1):03d}{suffix}"


@dataclass
class _RemoteAlbum:
    rel_dir: str
    next_index: int = 1


REMOTE_PAGE_RE = re.compile(
    r"(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<album_id>[^/]+)/(?P<seq>\d+)\.[A-Za-z0-9]+(?:\?|$)",
    re.I,
)
REMOTE_ALBUMS_FILE = "manga_remote_albums.json"


def album_state_from_urls(urls: list[str] | None) -> _RemoteAlbum | None:
    """从已上传 URL 恢复最早那天的目录，隔夜续传仍进同一文件夹。"""
    found: dict[str, int] = {}
    for raw in urls or []:
        match = REMOTE_PAGE_RE.search(str(raw or ""))
        if not match:
            continue
        rel_dir = f"{match.group('year')}/{match.group('month')}/{match.group('day')}/{match.group('album_id')}"
        seq = int(match.group("seq"))
        found[rel_dir] = max(found.get(rel_dir, 0), seq)
    if not found:
        return None
    rel_dir = min(found)
    return _RemoteAlbum(rel_dir=rel_dir, next_index=found[rel_dir] + 1)


def _merge_albums(*parts: _RemoteAlbum | None) -> _RemoteAlbum | None:
    items = [item for item in parts if item is not None and item.rel_dir]
    if not items:
        return None
    rel_dir = min(item.rel_dir for item in items)
    next_index = max(item.next_index for item in items if item.rel_dir == rel_dir)
    return _RemoteAlbum(rel_dir=rel_dir, next_index=max(int(next_index), 1))


def _albums_path(settings: MangaSettings) -> Path:
    return Path(settings.data_dir) / REMOTE_ALBUMS_FILE


def _join_public(base: str, rel: str) -> str:
    origin = str(base or "").strip()
    if not origin:
        raise MediaStoreError("请填写公网基址，用于拼图片 URL")
    if not origin.startswith(("http://", "https://")):
        origin = f"https://{origin}"
    origin = origin.rstrip("/") + "/"
    return urljoin(origin, str(rel or "").lstrip("/"))


def _ftp_remote_spec(settings: MangaSettings, rel_dir: str) -> tuple[str, bool]:
    raw = normalize_remote_dir(settings.ftp_remote_dir)
    absolute = raw.startswith("/")
    folder = "/".join(part for part in (_clean_dir(raw), _clean_dir(rel_dir)) if part)
    return folder, absolute


def _sftp_remote_spec(settings: MangaSettings, rel_dir: str) -> str:
    raw = normalize_remote_dir(settings.sftp_remote_dir)
    absolute = raw.startswith("/")
    folder = "/".join(part for part in (_clean_dir(raw), _clean_dir(rel_dir)) if part)
    if absolute and folder:
        return "/" + folder
    return folder


def _object_key(settings: MangaSettings, rel_dir: str, filename: str) -> str:
    prefix = _clean_dir(settings.s3_prefix or "manga")
    name = Path(filename).name
    return "/".join(part for part in (prefix, _clean_dir(rel_dir), name) if part)


def _object_content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return _OBJECT_CONTENT_TYPES.get(suffix, "image/jpeg")


def _ensure_ftp_dir(ftp: FTP, path: str, *, absolute: bool = False) -> None:
    parts = [item for item in path.split("/") if item]
    if absolute:
        ftp.cwd("/")
    for part in parts:
        try:
            ftp.cwd(part)
        except error_perm:
            try:
                ftp.mkd(part)
            except error_perm:
                pass
            ftp.cwd(part)


def _ensure_sftp_dir(sftp: Any, path: str) -> None:
    parts = [item for item in path.split("/") if item]
    current = "/" if path.startswith("/") else ""
    for part in parts:
        current = posixpath.join(current, part) if current else part
        try:
            sftp.stat(current)
            continue
        except OSError:
            pass
        try:
            sftp.mkdir(current)
        except OSError:
            sftp.stat(current)


def _sftp_known_hosts_path(settings: MangaSettings) -> Path:
    return Path(settings.data_dir) / "sftp_known_hosts"


class PersistSftpHostKey:
    """Trust-on-first-use：新主机写入 data_dir/sftp_known_hosts，之后指纹对不上由 paramiko 拒绝。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        client.get_host_keys().add(hostname, key.get_name(), key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(self.path))


class MediaStore:
    """按源把图片发到该源选定的目的地。"""

    def __init__(self, settings: MangaSettings, source: str, http: Any | None = None):
        self.settings = settings
        self.source = source if source in UPLOAD_SOURCES else "telegram"
        self.imgbed = ImgBedClient(settings, http=http)
        self._lock = asyncio.Lock()
        self._albums: dict[str, _RemoteAlbum] = {}
        self._ftp: FTP | None = None
        self._ftp_home = ""
        self._ftp_folder: str | None = None
        self._ssh: Any | None = None
        self._sftp: Any | None = None
        self._sftp_folder: str | None = None
        self._s3: Any | None = None

    def _close_ftp(self) -> None:
        ftp = self._ftp
        self._ftp = None
        self._ftp_home = ""
        self._ftp_folder = None
        if ftp is None:
            return
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    def _close_sftp(self) -> None:
        sftp = self._sftp
        ssh = self._ssh
        self._sftp = None
        self._ssh = None
        self._sftp_folder = None
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass

    async def aclose(self) -> None:
        await self.imgbed.aclose()
        self._close_ftp()
        self._close_sftp()
        self._s3 = None

    def target(self) -> str:
        return upload_target_for(self.settings, self.source)

    def _store_key(self, group_key: str) -> str:
        return f"{self.source}:{group_key}"

    def _load_persisted(self, group_key: str) -> _RemoteAlbum | None:
        raw = read_json_safe(_albums_path(self.settings), default={})
        if not isinstance(raw, dict):
            return None
        item = raw.get(self._store_key(group_key))
        if not isinstance(item, dict):
            return None
        rel_dir = str(item.get("rel_dir") or "").strip().strip("/")
        if not rel_dir:
            return None
        try:
            next_index = max(int(item.get("next_index") or 1), 1)
        except (TypeError, ValueError):
            next_index = 1
        return _RemoteAlbum(rel_dir=rel_dir, next_index=next_index)

    def _persist(self, group_key: str, album: _RemoteAlbum) -> None:
        store_key = self._store_key(group_key)
        payload = {"rel_dir": album.rel_dir, "next_index": int(album.next_index)}

        def mutator(raw: Any) -> dict[str, Any]:
            data = raw if isinstance(raw, dict) else {}
            data[store_key] = payload
            return data

        update_json_atomic(_albums_path(self.settings), mutator, default={})

    def remember_group(self, group_key: str | None, urls: list[str] | None = None) -> _RemoteAlbum | None:
        """隔夜/重启续传：沿用第一次的年/月/日/uuid 目录。"""
        key = (group_key or "").strip()
        if not key:
            return None
        chosen = _merge_albums(
            self._albums.get(key),
            self._load_persisted(key),
            album_state_from_urls(urls),
        )
        if chosen is None:
            return None
        self._albums[key] = chosen
        self._persist(key, chosen)
        return chosen

    def _album(self, group_key: str | None) -> _RemoteAlbum:
        key = (group_key or "").strip()
        if not key:
            return _RemoteAlbum(rel_dir=build_album_rel_dir())
        existing = _merge_albums(self._albums.get(key), self._load_persisted(key))
        if existing is not None:
            self._albums[key] = existing
            return existing
        album = _RemoteAlbum(rel_dir=build_album_rel_dir())
        self._albums[key] = album
        self._persist(key, album)
        return album

    async def upload_file(
        self,
        path: Path,
        *,
        filename: str | None = None,
        group_key: str | None = None,
    ) -> str:
        if not path.is_file():
            raise MediaStoreError(f"File not found: {path}")
        name = filename or path.name
        return await self.upload_bytes(path.read_bytes(), filename=name, group_key=group_key)

    async def upload_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        mime: str | None = None,
        group_key: str | None = None,
    ) -> str:
        target = self.target()
        if target == "imgbed":
            try:
                return await self.imgbed.upload_bytes(data, filename=filename, mime=mime)
            except ImgBedError as exc:
                raise MediaStoreError(str(exc)) from exc
        processed, ext, _detected = prepare_upload_image(data)
        async with self._lock:
            album = self._album(group_key)
            name = sequenced_filename(album.next_index, ext)
            try:
                if target == "ftp":
                    url = await asyncio.to_thread(
                        self._ftp_put, processed, name, album.rel_dir
                    )
                elif target == "sftp":
                    url = await asyncio.to_thread(
                        self._sftp_put, processed, name, album.rel_dir
                    )
                elif target in OBJECT_TARGETS:
                    url = await asyncio.to_thread(
                        self._object_put, processed, name, album.rel_dir, target
                    )
                else:
                    raise MediaStoreError(f"未知上传目标 {target}")
            except MediaStoreError:
                raise
            except Exception as exc:
                self._close_ftp()
                self._close_sftp()
                self._s3 = None
                raise MediaStoreError(str(exc)) from exc
            album.next_index += 1
            key = (group_key or "").strip()
            if key:
                self._albums[key] = album
                self._persist(key, album)
            return url

    def _ensure_ftp(self) -> FTP:
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return self._ftp
            except Exception:
                self._close_ftp()
        settings = self.settings
        host = (settings.ftp_host or "").strip()
        if not host:
            raise MediaStoreError("FTP 主机未填写")
        ftp: FTP = FTP_TLS() if settings.ftp_tls else FTP()
        try:
            ftp.connect(host, int(settings.ftp_port or 21), timeout=30)
            ftp.login((settings.ftp_username or "").strip(), settings.ftp_password or "")
            if settings.ftp_tls and hasattr(ftp, "prot_p"):
                ftp.prot_p()
            ftp.set_pasv(bool(settings.ftp_passive))
            try:
                self._ftp_home = ftp.pwd() or ""
            except Exception:
                self._ftp_home = ""
            self._ftp = ftp
            self._ftp_folder = None
            return ftp
        except Exception as exc:
            try:
                ftp.close()
            except Exception:
                pass
            if isinstance(exc, MediaStoreError):
                raise
            raise MediaStoreError(f"FTP 连接失败：{exc}") from exc

    def _ftp_put(self, data: bytes, filename: str, rel_dir: str) -> str:
        try:
            ftp = self._ensure_ftp()
            folder, absolute = _ftp_remote_spec(self.settings, rel_dir)
            name = Path(filename).name
            if self._ftp_folder != folder and not absolute and not self._ftp_home:
                self._close_ftp()
                ftp = self._ensure_ftp()
            if self._ftp_folder != folder:
                if absolute:
                    ftp.cwd("/")
                elif self._ftp_home:
                    ftp.cwd(self._ftp_home)
                if folder:
                    _ensure_ftp_dir(ftp, folder, absolute=False)
                self._ftp_folder = folder
            ftp.storbinary(f"STOR {name}", BytesIO(data))
        except MediaStoreError:
            raise
        except Exception as exc:
            self._close_ftp()
            raise MediaStoreError(f"FTP 上传失败：{exc}") from exc
        public_rel = "/".join(part for part in (_clean_dir(rel_dir), name) if part)
        return _join_public(self.settings.ftp_public_base, public_rel)

    def _ensure_sftp(self) -> Any:
        if self._sftp is not None:
            try:
                self._sftp.stat(".")
                return self._sftp
            except Exception:
                self._close_sftp()
        try:
            import paramiko
        except ImportError as exc:
            raise MediaStoreError("未安装 paramiko，无法使用 SFTP") from exc
        settings = self.settings
        host = (settings.sftp_host or "").strip()
        if not host:
            raise MediaStoreError("SFTP 主机未填写")
        known_hosts = _sftp_known_hosts_path(settings)
        client = paramiko.SSHClient()
        try:
            if known_hosts.is_file():
                client.load_host_keys(str(known_hosts))
            client.set_missing_host_key_policy(PersistSftpHostKey(known_hosts))
            password = settings.sftp_password or ""
            client.connect(
                hostname=host,
                port=int(settings.sftp_port or 22),
                username=(settings.sftp_username or "").strip(),
                password=password or None,
                look_for_keys=not bool(password),
                allow_agent=not bool(password),
                timeout=30,
            )
            self._ssh = client
            self._sftp = client.open_sftp()
            self._sftp_folder = None
            return self._sftp
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            self._ssh = None
            self._sftp = None
            if isinstance(exc, MediaStoreError):
                raise
            raise MediaStoreError(f"SFTP 连接失败：{exc}") from exc

    def _sftp_put(self, data: bytes, filename: str, rel_dir: str) -> str:
        try:
            sftp = self._ensure_sftp()
            folder = _sftp_remote_spec(self.settings, rel_dir)
            name = Path(filename).name
            if self._sftp_folder != folder:
                if folder:
                    _ensure_sftp_dir(sftp, folder)
                self._sftp_folder = folder
            remote_path = posixpath.join(folder, name) if folder else name
            sftp.putfo(BytesIO(data), remote_path)
        except MediaStoreError:
            raise
        except Exception as exc:
            self._close_sftp()
            raise MediaStoreError(f"SFTP 上传失败：{exc}") from exc
        public_rel = "/".join(part for part in (_clean_dir(rel_dir), name) if part)
        return _join_public(self.settings.sftp_public_base, public_rel)

    def _ensure_s3(self, target: str) -> Any:
        if self._s3 is not None:
            return self._s3
        if not object_configured(self.settings):
            raise MediaStoreError("对象存储未配置完整（Bucket、Access Key、Secret Key、公网基址）")
        endpoint = object_endpoint(self.settings)
        if target in ("r2", "b2") and not endpoint:
            raise MediaStoreError("R2 / B2 需要填写 Endpoint")
        try:
            import boto3
            from botocore.config import Config as BotoConfig

            from backend.utils.outbound import botocore_config_with_proxy
        except ImportError as exc:
            raise MediaStoreError("未安装 boto3，无法使用对象存储") from exc
        region = object_region(self.settings, target)
        addressing = object_addressing(endpoint, target)
        kwargs: dict[str, Any] = {
            "region_name": region,
            "aws_access_key_id": (self.settings.s3_access_key or "").strip(),
            "aws_secret_access_key": self.settings.s3_secret_key or "",
            "config": botocore_config_with_proxy(
                BotoConfig(
                    retries={"max_attempts": 3, "mode": "standard"},
                    s3={"addressing_style": addressing},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
                endpoint_url=endpoint or None,
            ),
        }
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _object_put(self, data: bytes, filename: str, rel_dir: str, target: str) -> str:
        try:
            client = self._ensure_s3(target)
            name = Path(filename).name
            key = _object_key(self.settings, rel_dir, name)
            client.put_object(
                Bucket=(self.settings.s3_bucket or "").strip(),
                Key=key,
                Body=data,
                ContentType=_object_content_type(name),
                CacheControl="public, max-age=31536000, immutable",
            )
        except MediaStoreError:
            raise
        except Exception as exc:
            self._s3 = None
            raise MediaStoreError(f"对象存储上传失败：{exc}") from exc
        public_rel = "/".join(part for part in (_clean_dir(rel_dir), name) if part)
        return _join_public(self.settings.s3_public_base, public_rel)
