"""Coser 图集：本地 AVIF、upload.json、CDN 路径与站点 URL 防重复。"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from backend.services.games.paths import IMAGE_SUFFIXES
from backend.utils.atomic_io import read_json_safe, write_json_atomic
from backend.utils.time import utc_now_iso_z

from .avif import avif_is_valid, convert_to_avif
from .config import CoserSettings
from .keys import (
    gallery_folder,
    image_filename,
    object_key,
    path_is_unsafe,
    urls_match,
)
from .paths import coser_dirs
from .s3 import (
    CoserS3Error,
    count_prefix,
    object_exists,
    public_object_url,
    upload_file,
    verify_public_url,
)

logger = logging.getLogger("backend.coser.gallery")

_NATURAL = re.compile(r"(\d+)")
ProgressFn = Callable[[int, int, str], None]


@dataclass(frozen=True)
class GalleryDecision:
    upload: bool
    update_site_images: bool
    reason: str
    reasons: tuple[str, ...] = ()


@dataclass
class GalleryResult:
    urls: list[str]
    cover: str
    keys: list[str]
    uploaded: bool
    reason: str
    manifest: dict[str, Any] = field(default_factory=dict)
    work_id: int = 0
    folder: str = ""
    skipped: bool = False


def _natural_key(name: str) -> list:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in _NATURAL.split(name)
    ]


def collect_source_images(*roots: Path | None) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root is None:
            continue
        path = Path(root)
        candidates: list[tuple[Path, Path]]
        if path.is_file():
            candidates = [(path, Path(path.name))]
        elif path.is_dir():
            candidates = [
                (item, item.relative_to(path))
                for item in path.rglob("*")
                if item.is_file()
            ]
        else:
            continue
        for item, relative in candidates:
            if item.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if any(
                part.startswith(".") or part == "__MACOSX"
                for part in relative.parts
            ):
                continue
            resolved = item.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(item)
    files.sort(key=lambda item: (_natural_key(item.parent.as_posix()), _natural_key(item.name)))
    return files


def gallery_local_dir(
    settings: CoserSettings,
    *,
    coser_name: str,
    work_id: int,
    title: str,
) -> Path:
    folder = gallery_folder(
        settings, coser_name=coser_name, work_id=work_id, title=title
    )
    return coser_dirs(settings).gallery.joinpath(*folder.split("/"))


def manifest_path(directory: Path) -> Path:
    return directory / "upload.json"


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = read_json_safe(path, default=None)
    return raw if isinstance(raw, dict) else None


def find_manifest_for_work(settings: CoserSettings, work_id: int) -> dict[str, Any] | None:
    root = coser_dirs(settings).gallery
    if not root.is_dir():
        return None
    for path in root.glob("*/*/upload.json"):
        data = load_manifest(path)
        if data and int(data.get("work_id") or 0) == int(work_id):
            return data
    return None


def _manifest_urls(manifest: dict[str, Any] | None) -> list[str]:
    if not isinstance(manifest, dict):
        return []
    images = manifest.get("images") or []
    urls: list[str] = []
    for item in images:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
        else:
            url = str(item or "").strip()
        if url:
            urls.append(url)
    return urls


def _manifest_complete(manifest: dict[str, Any] | None, expected_urls: list[str]) -> bool:
    if not isinstance(manifest, dict):
        return False
    if str(manifest.get("status") or "") != "success":
        return False
    if int(manifest.get("expected_count") or 0) != len(expected_urls):
        return False
    return urls_match(_manifest_urls(manifest), expected_urls)


def decide_gallery(
    *,
    expected_count: int,
    expected_urls: list[str],
    manifest: dict[str, Any] | None,
    cdn_ok: list[bool],
    cdn_listed_count: int | None,
    site_images: list[str],
    local_avif_ok: list[bool],
    existing_urls: list[str],
) -> GalleryDecision:
    """判定要不要重新上传。CDN 完好时只修正站点 URL，不重复 put。"""
    expected_count = int(expected_count or 0)
    reasons: list[str] = []
    cdn_complete = (
        expected_count > 0
        and len(cdn_ok) == expected_count
        and all(bool(flag) for flag in cdn_ok)
        and (cdn_listed_count is None or int(cdn_listed_count) >= expected_count)
    )
    if cdn_listed_count is not None and int(cdn_listed_count) < expected_count:
        reasons.append("count_short")
    if not cdn_complete:
        reasons.append("cdn_url_failed")

    manifest_ok = _manifest_complete(manifest, expected_urls)
    if not manifest_ok:
        reasons.append("upload_json_failed")

    unsafe_existing = any(path_is_unsafe(url) for url in existing_urls if url)
    unsafe_expected = any(path_is_unsafe(url) for url in expected_urls)
    if unsafe_existing or unsafe_expected:
        reasons.append("unsafe_path")

    local_ok = (
        expected_count > 0
        and len(local_avif_ok) == expected_count
        and all(bool(flag) for flag in local_avif_ok)
    )
    if not local_ok:
        reasons.append("corrupt_avif")

    site_ok = expected_count > 0 and urls_match(site_images, expected_urls)
    if not site_ok:
        reasons.append("url_mismatch")

    upload = False
    if expected_count <= 0:
        upload = False
    elif unsafe_expected:
        upload = True
    elif not cdn_complete:
        upload = True
    elif not local_ok and not cdn_complete:
        upload = True

    update_site = (not site_ok) or upload
    if cdn_complete and manifest_ok and site_ok and local_ok and not unsafe_expected:
        return GalleryDecision(False, False, "reuse", ())

    if upload:
        reason = next(
            (
                item
                for item in (
                    "unsafe_path",
                    "upload_json_failed",
                    "count_short",
                    "cdn_url_failed",
                    "corrupt_avif",
                    "url_mismatch",
                )
                if item in reasons
            ),
            "upload",
        )
    elif update_site:
        reason = "site_urls"
    else:
        reason = "reuse"
        update_site = False
        upload = False
    return GalleryDecision(upload, update_site, reason, tuple(dict.fromkeys(reasons)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["updated_at"] = utc_now_iso_z()
    write_json_atomic(path, payload)
    return payload


def sync_gallery(
    settings: CoserSettings,
    *,
    sources: list[Path],
    work_id: int,
    title: str,
    coser_name: str,
    site_images: list[str] | None = None,
    progress: ProgressFn | None = None,
) -> GalleryResult:
    images = [Path(item) for item in sources if Path(item).is_file()]
    if not images:
        raise CoserS3Error("没有可上传的图集")
    work_id = int(work_id)
    if work_id <= 0:
        raise CoserS3Error("缺少作品编号，无法生成 CDN 路径")
    total = len(images)
    folder = gallery_folder(
        settings, coser_name=coser_name, work_id=work_id, title=title
    )
    local_dir = gallery_local_dir(
        settings, coser_name=coser_name, work_id=work_id, title=title
    )
    local_dir.mkdir(parents=True, exist_ok=True)
    keys = [
        object_key(
            settings,
            coser_name=coser_name,
            work_id=work_id,
            title=title,
            index=index,
            total=total,
        )
        for index in range(1, total + 1)
    ]
    urls = [public_object_url(settings, key) for key in keys]
    avifs: list[Path] = []
    for index, source in enumerate(images, start=1):
        dest = local_dir / image_filename(index, total)
        if progress:
            progress(index - 1, total, f"转 AVIF {index}/{total}")
        if not (dest.is_file() and avif_is_valid(dest)):
            convert_to_avif(source, dest)
        avifs.append(dest)
    local_ok = [avif_is_valid(path) for path in avifs]
    if not all(local_ok):
        for index, (source, dest, ok) in enumerate(
            zip(images, avifs, local_ok, strict=True), start=1
        ):
            if ok:
                continue
            if progress:
                progress(index - 1, total, f"修复损坏的 AVIF {index}/{total}")
            convert_to_avif(source, dest)
        local_ok = [avif_is_valid(path) for path in avifs]
    if not all(local_ok):
        raise CoserS3Error("本地 AVIF 文件损坏，无法上传")

    expected_json = manifest_path(local_dir)
    manifest = load_manifest(expected_json) or find_manifest_for_work(settings, work_id)
    existing_urls = list(_manifest_urls(manifest))
    for url in site_images or []:
        if str(url or "").strip():
            existing_urls.append(str(url).strip())
    cdn_ok = [
        bool(object_exists(settings, key) or verify_public_url(url))
        for key, url in zip(keys, urls, strict=True)
    ]
    listed = count_prefix(settings, folder)

    decision = decide_gallery(
        expected_count=total,
        expected_urls=urls,
        manifest=manifest,
        cdn_ok=cdn_ok,
        cdn_listed_count=listed,
        site_images=list(site_images or []),
        local_avif_ok=local_ok,
        existing_urls=existing_urls,
    )

    uploaded = False
    if decision.upload:
        for index, (path, key) in enumerate(zip(avifs, keys, strict=True), start=1):
            if progress:
                progress(index - 1, total, f"S3 上传 {image_filename(index, total)}")
            upload_file(settings, path, key, content_type="image/avif")
        uploaded = True
        cdn_ok = [
            bool(object_exists(settings, key) or verify_public_url(url))
            for key, url in zip(keys, urls, strict=True)
        ]
        if not all(cdn_ok):
            payload = _manifest_payload(
                work_id=work_id,
                title=title,
                coser_name=coser_name,
                folder=folder,
                keys=keys,
                urls=urls,
                avifs=avifs,
                status="failed",
                error="CDN 校验失败",
            )
            _write_manifest(expected_json, payload)
            raise CoserS3Error("上传后 CDN 对象校验失败")

    status = "success" if all(cdn_ok) else "failed"
    payload = _manifest_payload(
        work_id=work_id,
        title=title,
        coser_name=coser_name,
        folder=folder,
        keys=keys,
        urls=urls,
        avifs=avifs,
        status=status,
        error="" if status == "success" else "图集未完整成功",
    )
    saved = _write_manifest(expected_json, payload)
    skipped = (not uploaded) and decision.reason in {"reuse", "site_urls"}
    return GalleryResult(
        urls=urls,
        cover=urls[0] if urls else "",
        keys=keys,
        uploaded=uploaded,
        reason=decision.reason,
        manifest=saved,
        work_id=work_id,
        folder=folder,
        skipped=skipped,
    )


def _manifest_payload(
    *,
    work_id: int,
    title: str,
    coser_name: str,
    folder: str,
    keys: list[str],
    urls: list[str],
    avifs: list[Path],
    status: str,
    error: str = "",
) -> dict[str, Any]:
    images = []
    for index, (key, url, path) in enumerate(
        zip(keys, urls, avifs, strict=True), start=1
    ):
        item = {
            "index": index,
            "name": path.name,
            "key": key,
            "url": url,
            "ok": status == "success" and path.is_file(),
            "size": path.stat().st_size if path.is_file() else 0,
        }
        if path.is_file():
            item["sha256"] = _sha256(path)
        images.append(item)
    return {
        "status": status,
        "work_id": int(work_id),
        "coser_name": coser_name,
        "title": title,
        "folder": folder,
        "expected_count": len(urls),
        "cover_url": urls[0] if urls else "",
        "images": images,
        "error": error,
    }
