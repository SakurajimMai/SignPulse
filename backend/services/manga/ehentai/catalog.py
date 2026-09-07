"""EhTagTranslation 词库：与 https://ehtt.vercel.app/list/all 同源。

编辑站本身是 SPA，完整译文在 DatabaseReleases 的 db.text.json。
用 ehtt.fly.dev 的 ETag（Git SHA）判断是否需要重新下载。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from backend.utils.atomic_io import write_json_atomic
from backend.utils.outbound import httpx_async_client_kwargs

from . import tags as tag_mod

logger = logging.getLogger(__name__)

_STATUS: TranslationStatus | None = None

EHTT_EDITOR_URL = "https://ehtt.vercel.app/list/all"
EHTT_VERSION_URL = "https://ehtt.fly.dev/database"
DEFAULT_DUMP_URLS = (
    "https://cdn.jsdelivr.net/gh/EhTagTranslation/DatabaseReleases@master/db.text.json",
    "https://fastly.jsdelivr.net/gh/EhTagTranslation/DatabaseReleases@master/db.text.json",
    "https://raw.githubusercontent.com/EhTagTranslation/DatabaseReleases/master/db.text.json",
    "https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json",
)


def catalog_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "ehentai" / "tag_names.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slim_dump(raw: dict[str, Any], *, source: str = "") -> dict[str, Any]:
    """只保留中文名，去掉 intro/links，方便加载和维护。"""
    data: list[dict[str, Any]] = []
    tag_count = 0
    for item in raw.get("data") or []:
        ns = str(item.get("namespace") or "").strip()
        if not ns:
            continue
        names: dict[str, dict[str, str]] = {}
        for key, value in (item.get("data") or {}).items():
            name = ""
            if isinstance(value, dict):
                name = str(value.get("name") or "").strip()
            elif isinstance(value, str):
                name = value.strip()
            if name:
                names[str(key)] = {"name": name}
        data.append({"namespace": ns, "data": names})
        if ns != "rows":
            tag_count += len(names)
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    return {
        "source": source,
        "editor": EHTT_EDITOR_URL,
        "repo": str(raw.get("repo") or ""),
        "version": str(raw.get("version") or ""),
        "sha": str(head.get("sha") or ""),
        "updated_at": _utcnow(),
        "tag_count": tag_count,
        "data": data,
    }


@dataclass
class TranslationStatus:
    editor_url: str = EHTT_EDITOR_URL
    source: str = ""
    repo: str = ""
    version: str = ""
    sha: str = ""
    tag_count: int = 0
    namespaces: int = 0
    updated_at: str = ""
    using_bundled: bool = True
    path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_catalog(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _status_from_payload(raw: dict[str, Any] | None, *, path: Path, bundled: bool) -> TranslationStatus:
    raw = raw or {}
    data = raw.get("data") if isinstance(raw.get("data"), list) else []
    tag_count = int(raw.get("tag_count") or 0)
    if not tag_count:
        for item in data:
            if not isinstance(item, dict):
                continue
            if str(item.get("namespace") or "") == "rows":
                continue
            names = item.get("data") or {}
            if isinstance(names, dict):
                tag_count += len(names)
    return TranslationStatus(
        editor_url=str(raw.get("editor") or EHTT_EDITOR_URL),
        source=str(raw.get("source") or ("bundled" if bundled else "")),
        repo=str(raw.get("repo") or ""),
        version=str(raw.get("version") or ""),
        sha=str(raw.get("sha") or ""),
        tag_count=tag_count,
        namespaces=len(data),
        updated_at=str(raw.get("updated_at") or ""),
        using_bundled=bundled,
        path=str(path),
    )


def translation_status(data_dir: str | Path) -> TranslationStatus:
    global _STATUS
    if _STATUS is not None:
        return _STATUS
    path = catalog_path(data_dir)
    cached = _read_catalog(path)
    if cached and cached.get("data"):
        _STATUS = _status_from_payload(cached, path=path, bundled=False)
        return _STATUS
    bundled = tag_mod.bundled_path()
    raw = _read_catalog(bundled)
    _STATUS = _status_from_payload(raw, path=bundled, bundled=True)
    return _STATUS


def apply_catalog(data_dir: str | Path) -> TranslationStatus:
    global _STATUS
    _STATUS = None
    status = translation_status(data_dir)
    tag_mod.set_catalog_path(None if status.using_bundled else Path(status.path))
    return status


def dump_urls(override: str = "") -> tuple[str, ...]:
    custom = (override or "").strip()
    if custom:
        return (custom,)
    return DEFAULT_DUMP_URLS


async def remote_sha(http: httpx.AsyncClient) -> str | None:
    try:
        response = await http.head(EHTT_VERSION_URL, follow_redirects=True)
        etag = (response.headers.get("ETag") or "").strip().strip('"')
        return etag or None
    except Exception:
        logger.debug("EhTagTranslation version HEAD failed", exc_info=True)
        return None


async def _fetch_json(http: httpx.AsyncClient, url: str) -> dict[str, Any]:
    response = await http.get(url, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError(f"EhTagTranslation dump 格式不对: {url}")
    return data


async def refresh_catalog(
    data_dir: str | Path,
    *,
    source_url: str = "",
    force: bool = False,
    http: httpx.AsyncClient | None = None,
) -> TranslationStatus:
    path = catalog_path(data_dir)
    current = translation_status(data_dir)
    owns = http is None
    client = http or httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(90.0, connect=20.0),
            headers={"User-Agent": "tg-manga-ehtt/1.0"},
            follow_redirects=True,
        )
    )
    try:
        if not force and not current.using_bundled and current.sha:
            latest = await remote_sha(client)
            if latest and latest == current.sha:
                logger.info("EhTagTranslation 词库已是最新 sha=%s", current.sha[:12])
                apply_catalog(data_dir)
                return current
            if latest is None:
                # 版本接口暂时不可用时不要每次启动都重下 4MB
                apply_catalog(data_dir)
                return current
        last_error: Exception | None = None
        raw: dict[str, Any] | None = None
        used = ""
        for url in dump_urls(source_url):
            try:
                logger.info("下载 EhTagTranslation 词库 %s", url)
                raw = await _fetch_json(client, url)
                used = url
                break
            except Exception as exc:
                last_error = exc
                logger.warning("词库下载失败 %s: %s", url, exc)
        if raw is None:
            raise RuntimeError(f"无法下载 EhTagTranslation 词库: {last_error}")
        slim = slim_dump(raw, source=used)
        if not slim["sha"]:
            slim["sha"] = (await remote_sha(client)) or ""
        write_json_atomic(path, slim)
        apply_catalog(data_dir)
        status = translation_status(data_dir)
        logger.info(
            "EhTagTranslation 词库已更新 sha=%s tags=%s source=%s",
            (status.sha or "-")[:12],
            status.tag_count,
            status.source,
        )
        return status
    finally:
        if owns:
            await client.aclose()
