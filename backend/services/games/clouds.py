from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import mimetypes
import re
import secrets
import string
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import quote

import httpx
from pypinyin import Style, lazy_pinyin

from backend.utils.http_error_text import looks_like_html, safe_response_snippet
from backend.utils.outbound import (
    botocore_config_with_proxy,
    create_async_httpx_transport,
    get_proxy_runtime_settings,
    httpx_async_client_kwargs,
)

from .config import GamesSettings, save_games_settings

logger = logging.getLogger("backend.games.clouds")

PIKPAK_CLIENT_ID = "YNxT9w7GMdWvEOKa"
TERABOX_BASE = "https://www.terabox.com"
TERABOX_APP_UA = "terabox;1.40.0.132;PC;PC-Windows;10.0.26100;WindowsTeraBox"
QUARK_API_BASES = (
    "https://drive-pc.quark.cn/1/clouddrive",
    "https://drive.quark.cn/1/clouddrive",
)
QUARK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
QUARK_OSS_UA = "aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit"
CLOUD_TARGETS = ("baidu", "pikpak", "terabox", "quark")
# 海外到百度 PCS 经常只有几十 KB/s，单独放最后，避免挡住 WordPress。
FAST_CLOUD_TARGETS = ("pikpak", "terabox", "quark")
SLOW_CLOUD_TARGETS = ("baidu",)
_SHARE_CODE_ALPHABET = string.ascii_lowercase + string.digits
_BAIDU_PERMANENT_EXPIRED_TYPES = {None, 0, 1, "0", "1"}
_BAIDU_EXPIRED_SHARE_MARKERS = {"分享已过期", "分享的文件已被删除"}
_JS_TOKEN_RE = re.compile(
    r"fn%28%22(?P<enc>[^%\"]+)%22%29|window\.jsToken\s*=\s*\"(?P<plain>[^\"]+)\""
)
_BDSTOKEN_RE = re.compile(r'"bdstoken"\s*:\s*"([^"]+)"')
_BAIDU_WEB_QUERY = {
    "channel": "chunlei",
    "web": "1",
    "app_id": "250528",
    "clienttype": "0",
}
# 目录已存在。绝不能用 rtype=1 自动改名，否则会在根目录堆出 网站(1)、game(2)。
_BAIDU_DIR_EXISTS = {None, 0, -8, "-8", 31061, "31061"}
_BAIDU_DIR_MISSING = {2, -9, 12, 31066, "-9", "2", "12", "31066"}
# list/filemetas 常因缺 STOKEN 返回 -6，不能当成整次上传失败。
_BAIDU_LIST_UNAUTH = {-6, "-6", 31045, "31045", -7, "-7"}
_BAIDU_AUTH_COOKIE_NAMES = {"BDUSS", "STOKEN", "PANPSC", "BAIDUID"}
_BAIDU_UPLOAD_HOSTS = (
    "d.pcs.baidu.com",
    "pcs.baidu.com",
    "qdall01.baidupcs.com",
    "c.pcs.baidu.com",
)
ProgressFn = Callable[[str, int, int, str], Awaitable[None] | None]


class CloudError(RuntimeError):
    pass


def cloud_upload_phases(
    settings: GamesSettings, selected: Sequence[str] | None
) -> tuple[list[str] | None, list[str]]:
    """拆成「先发」和「后补」。百度太慢时不要挡住 WordPress。"""
    targets = list(CLOUD_TARGETS if selected is None else selected)
    statuses = cloud_status(settings)
    ready = [
        target
        for target in targets
        if (statuses.get(target) or {}).get("configured")
    ]
    slow = [target for target in ready if target in SLOW_CLOUD_TARGETS]
    fast = [target for target in FAST_CLOUD_TARGETS if target in ready]
    extra = [
        target
        for target in ready
        if target not in slow and target not in fast
    ]
    fast.extend(extra)
    if slow and fast:
        return fast, slow
    return (None if selected is None else list(selected), [])


def normalize_upload_targets(selected: Sequence[str] | None) -> list[str] | None:
    """None 表示四个网盘都传；否则返回去重后的子集，可以是空列表。"""
    if selected is None:
        return None
    wanted: list[str] = []
    seen: set[str] = set()
    for item in selected:
        key = str(item or "").strip().lower()
        if key in CLOUD_TARGETS and key not in seen:
            wanted.append(key)
            seen.add(key)
    return wanted


@dataclass(frozen=True)
class PreparedUpload:
    path: Path
    remote_name: str
    temporary: bool = False


PrepareFn = Callable[[Path, int, int], PreparedUpload | Awaitable[PreparedUpload]]


def _cookie_header(raw: str) -> str:
    return str(raw or "").strip()


def _cookie_dict(raw: str, required_key: str) -> dict[str, str]:
    text = _cookie_header(raw)
    if not text:
        return {}
    if "=" not in text:
        return {required_key: text}
    if required_key.casefold() not in text.casefold() and ";" not in text:
        return {required_key: text}
    cookies: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def _cookie_header_from_map(cookies: dict[str, str]) -> str:
    return "; ".join(
        f"{name}={value}"
        for name, value in cookies.items()
        if name and value
    )


def _has_cookie_key(cookies: dict[str, str], required_key: str) -> bool:
    needle = required_key.casefold()
    return any(name.casefold() == needle for name in cookies)


def _client_cookie_pairs(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    jar = getattr(client.cookies, "jar", None)
    if jar is not None:
        pairs: list[tuple[str, str]] = []
        for cookie in jar:
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            if name and value:
                pairs.append((name, value))
        return pairs
    try:
        return [(str(name), str(value)) for name, value in client.cookies.items()]
    except Exception:
        return []


def persist_client_cookies(
    field: str,
    original: str,
    required_key: str,
    client: httpx.AsyncClient,
) -> bool:
    """把会话里刷新过的 Cookie 写回配置，避免网盘登录态过期。"""
    original = str(original or "").strip()
    if not original:
        return False
    merged = _cookie_dict(original, required_key)
    original_map = {key.casefold(): (key, value) for key, value in merged.items()}
    for name, value in _client_cookie_pairs(client):
        if not name or not str(value).strip():
            continue
        previous = original_map.get(name.casefold())
        if (
            name.casefold() in {item.casefold() for item in _BAIDU_AUTH_COOKIE_NAMES}
            and previous
            and previous[1]
            and len(str(value)) < max(16, int(len(previous[1]) * 0.6))
        ):
            continue
        merged[name] = value
    for auth in _BAIDU_AUTH_COOKIE_NAMES:
        if _has_cookie_key(merged, auth):
            continue
        previous = original_map.get(auth.casefold())
        if previous:
            merged[previous[0]] = previous[1]
    if required_key and not _has_cookie_key(merged, required_key):
        return False
    if merged == _cookie_dict(original, required_key):
        return False
    try:
        save_games_settings({field: _cookie_header_from_map(merged)})
    except Exception:
        logger.warning("无法保存更新后的 %s", field)
        return False
    logger.info("已写回刷新后的 %s", field)
    return True


def random_share_code() -> str:
    """网盘提取码：每次发布随机 4 位字母数字。"""
    return "".join(secrets.choice(_SHARE_CODE_ALPHABET) for _ in range(4))


def _share_extract_code(raw: str, fallback: str = "") -> str:
    chars = re.sub(r"[^A-Za-z0-9]", "", str(raw or ""))
    if len(chars) >= 4:
        return chars[:4]
    return fallback or random_share_code()


def _with_share_pwd(link: str, pwd: str) -> str:
    if not pwd:
        return link
    if re.search(r"[?&]pwd=", link):
        return re.sub(r"([?&]pwd=)[^&#]*", rf"\g<1>{pwd}", link, count=1)
    sep = "&" if "?" in link else "?"
    return f"{link}{sep}pwd={pwd}"


def share_pwd_from_links(links: dict[str, str] | None, fallback: str = "") -> str:
    for url in (links or {}).values():
        match = re.search(r"[?&]pwd=([A-Za-z0-9]{4})\b", str(url or ""))
        if match:
            return match.group(1)
    return fallback


def _safe_original_folder(title: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(title or "")).strip(" .")
    value = re.sub(r"\s+", " ", value)
    return value[:100].strip(" .") or "game"


def baidu_title_folder(title: str) -> str:
    """Convert Chinese characters and ASCII words to stable title initials."""
    parts: list[str] = []
    ascii_word: list[str] = []

    def flush_ascii() -> None:
        if not ascii_word:
            return
        token = "".join(ascii_word)
        parts.append(token[0])
        parts.extend(char for char in token[1:] if char.isdigit())
        ascii_word.clear()

    for char in str(title or "").strip():
        if "\u4e00" <= char <= "\u9fff":
            flush_ascii()
            initial = lazy_pinyin(
                char,
                style=Style.FIRST_LETTER,
                errors=lambda value: list(value),
            )
            if initial:
                parts.append(initial[0][:1])
        elif char.isascii() and char.isalnum():
            ascii_word.append(char)
        else:
            flush_ascii()
    flush_ascii()
    value = re.sub(r"[^a-z0-9]", "", "".join(parts).casefold())[:64]
    if value:
        return value
    digest = hashlib.sha1(str(title or "game").encode("utf-8")).hexdigest()[:8]
    return f"game{digest}"


def target_folder_name(target: str, title: str) -> str:
    """百度/夸克用拼音首字母；其它网盘用原标题。都挂在各自配置的远程目录下。"""
    if target in {"baidu", "quark"}:
        return baidu_title_folder(title)
    return _safe_original_folder(title)


def target_base_dir(settings: GamesSettings, target: str) -> str:
    mapping = {
        "baidu": settings.baidu_remote_dir,
        "pikpak": settings.pikpak_remote_dir,
        "terabox": settings.terabox_remote_dir,
        "quark": settings.quark_remote_dir,
    }
    return str(mapping.get(target) or "").strip() or "/games"


def target_remote_path(settings: GamesSettings, target: str, title: str) -> str:
    return _join_remote(target_base_dir(settings, target), target_folder_name(target, title))


def _join_remote(*parts: str) -> str:
    values: list[str] = []
    for part in parts:
        values.extend(
            item for item in str(part or "").replace("\\", "/").split("/") if item
        )
    return "/" + "/".join(values)


def _path_segments(path: str) -> list[str]:
    return [item for item in str(path or "").replace("\\", "/").split("/") if item]


def _baidu_query(token: str = "", extra: dict[str, Any] | None = None) -> dict[str, str]:
    params = {key: str(value) for key, value in _BAIDU_WEB_QUERY.items()}
    if token:
        params["bdstoken"] = token
    for key, value in (extra or {}).items():
        if value is None:
            continue
        params[str(key)] = str(value)
    return params


def _json_data(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _fs_id(payload: dict[str, Any]) -> str | int | None:
    direct = payload.get("fs_id")
    if direct:
        return direct
    nested = payload.get("info")
    if isinstance(nested, dict) and nested.get("fs_id"):
        return nested["fs_id"]
    for key in ("info", "list"):
        items = payload.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            value = items[0].get("fs_id")
            if value:
                return value
    return None


async def _maybe_progress(
    progress: ProgressFn | None,
    target: str,
    current: int,
    total: int,
    message: str,
) -> None:
    if progress is None:
        return
    result = progress(target, current, total, message)
    if inspect.isawaitable(result):
        await result


async def _prepare(
    source: Path,
    index: int,
    total: int,
    prepare: PrepareFn | None,
) -> PreparedUpload:
    if prepare is None:
        return PreparedUpload(source, source.name)
    result = prepare(source, index, total)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, PreparedUpload):
        raise CloudError("上传文件准备器返回了无效结果")
    if not result.path.is_file():
        raise CloudError(f"待上传文件不存在：{result.path.name}")
    return result


def _cleanup_prepared(item: PreparedUpload) -> None:
    if item.temporary:
        item.path.unlink(missing_ok=True)


def openlist_configured(settings: GamesSettings, target: str) -> bool:
    if not settings.openlist_url or not settings.openlist_token:
        return False
    mapping = {
        "baidu": settings.openlist_baidu_path,
        "pikpak": settings.openlist_pikpak_path,
        "terabox": settings.openlist_terabox_path,
        "quark": settings.openlist_quark_path,
    }
    return bool(str(mapping.get(target) or "").strip())


def _openlist_root(settings: GamesSettings, target: str) -> str:
    return str(
        {
            "baidu": settings.openlist_baidu_path,
            "pikpak": settings.openlist_pikpak_path,
            "terabox": settings.openlist_terabox_path,
            "quark": settings.openlist_quark_path,
        }.get(target, "")
        or ""
    ).strip()


def _check_openlist(response: httpx.Response, action: str) -> dict[str, Any]:
    if response.status_code >= 400 or looks_like_html(response.text):
        raise CloudError(
            f"OpenList {action}失败：{safe_response_snippet(response, limit=300)}"
        )
    payload = _json_data(response)
    code = payload.get("code")
    if code not in {None, 0, 200}:
        raise CloudError(f"OpenList {action}失败：{payload.get('message') or payload}")
    return payload


async def upload_openlist_folder(
    settings: GamesSettings,
    target: str,
    sources: Sequence[Path],
    folder_name: str,
    *,
    prepare: PrepareFn | None = None,
    progress: ProgressFn | None = None,
) -> str:
    base = str(settings.openlist_url or "").rstrip("/")
    token = str(settings.openlist_token or "").strip()
    root = _openlist_root(settings, target)
    if not base or not token or not root:
        raise CloudError(f"未配置 OpenList {target} 路径")
    remote_folder = _join_remote(root, folder_name)
    auth = {"Authorization": token}
    total = len(sources)
    async with httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(None), follow_redirects=True
        )
    ) as client:
        mkdir = await client.post(
            f"{base}/api/fs/mkdir",
            headers=auth,
            json={"path": remote_folder},
        )
        _check_openlist(mkdir, "创建目录")
        for index, source in enumerate(sources, start=1):
            item = await _prepare(source, index, total, prepare)
            try:
                await _maybe_progress(
                    progress,
                    target,
                    index - 1,
                    total,
                    f"{target} 上传 {index}/{total}",
                )
                remote = _join_remote(remote_folder, item.remote_name)
                mime = (
                    mimetypes.guess_type(item.remote_name)[0]
                    or "application/octet-stream"
                )
                headers = {
                    **auth,
                    "File-Path": quote(remote, safe="/"),
                    "Content-Type": mime,
                    "As-Task": "false",
                }
                with item.path.open("rb") as handle:
                    response = await client.put(
                        f"{base}/api/fs/put", headers=headers, content=handle
                    )
                _check_openlist(response, "上传")
                await _maybe_progress(
                    progress,
                    target,
                    index,
                    total,
                    f"{target} 已上传 {index}/{total}",
                )
            finally:
                _cleanup_prepared(item)
    return f"{base}/d{quote(remote_folder, safe='/')}"


async def upload_openlist(
    settings: GamesSettings,
    target: str,
    path: Path,
    remote_name: str,
) -> str:
    folder = _safe_original_folder(Path(remote_name).stem)

    def rename(source: Path, _index: int, _total: int) -> PreparedUpload:
        return PreparedUpload(source, remote_name)

    return await upload_openlist_folder(
        settings, target, [path], folder, prepare=rename
    )


def _md5_blocks(
    path: Path, block_size: int = 4 * 1024 * 1024
) -> tuple[list[str], str, int]:
    blocks: list[str] = []
    digest = hashlib.md5()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(block_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            blocks.append(hashlib.md5(chunk).hexdigest())
    if not blocks:
        empty = hashlib.md5(b"").hexdigest()
        blocks = [empty]
        digest.update(b"")
    return blocks, digest.hexdigest(), size


def _transient_cloud_error(exc: Exception) -> bool:
    text = str(exc or "").casefold()
    if isinstance(exc, httpx.TransportError):
        return True
    return any(
        token in text
        for token in (
            "all connection attempts failed",
            "connecterror",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "network is unreachable",
            "name or service not known",
            "server disconnected",
            "remote protocol error",
        )
    )


def _baidu_http_client(
    cookies: dict[str, str], headers: dict[str, str]
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(
                connect=30.0, read=90.0, write=120.0, pool=30.0
            ),
            follow_redirects=True,
            cookies=cookies,
            headers=headers,
            transport=create_async_httpx_transport(retries=3),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
    )


async def _baidu_token(client: httpx.AsyncClient) -> str:
    """从网盘 API 取 bdstoken，避免 /disk/home 跳登录页形成重定向环。"""
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            return await _baidu_token_once(client)
        except Exception as exc:
            last_error = exc
            if attempt >= 5 or not _transient_cloud_error(exc):
                break
            logger.warning("百度取 token 第 %s 次失败，重试：%s", attempt, exc)
            await asyncio.sleep(min(2 * attempt, 10))
    if isinstance(last_error, CloudError):
        raise last_error
    raise CloudError(f"百度无法取得 bdstoken：{last_error}")


async def _baidu_token_once(client: httpx.AsyncClient) -> str:
    response = await client.get(
        "https://pan.baidu.com/api/gettemplatevariable",
        params={"fields": '["bdstoken"]', "web": "1"},
    )
    data = _json_data(response)
    result = data.get("result")
    token = ""
    if isinstance(result, dict):
        token = str(result.get("bdstoken") or "")
    elif isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            token = str(first.get("bdstoken") or "")
        else:
            token = str(first or "")
    if not token:
        match = _BDSTOKEN_RE.search(response.text or "")
        token = match.group(1) if match else ""
    if token:
        return token
    home = await client.get(
        "https://pan.baidu.com/disk/home",
        follow_redirects=False,
    )
    match = _BDSTOKEN_RE.search(home.text or "")
    token = match.group(1) if match else ""
    if token:
        return token
    raise CloudError("百度 Cookie 可用，但无法取得 bdstoken，请重新填写 BDUSS")


async def _baidu_search_fs_id(
    client: httpx.AsyncClient,
    token: str,
    remote_path: str,
    *,
    isdir: bool | None = None,
) -> str | int | None:
    """list/filemetas 常因缺 STOKEN 返回 -6，搜索接口仍可用。"""
    path = str(remote_path or "").rstrip("/") or "/"
    name = path.rsplit("/", 1)[-1]
    parent = path.rsplit("/", 1)[0] or "/"
    if not name:
        return None
    params = _baidu_query(
        token,
        {
            "key": name,
            "dir": parent,
            "recursion": "0",
            "num": "50",
            "page": "1",
        },
    )
    response = await client.get("https://pan.baidu.com/api/search", params=params)
    if response.status_code >= 400:
        return None
    for item in _json_data(response).get("list") or []:
        if not isinstance(item, dict):
            continue
        item_path = str(item.get("path") or "").rstrip("/")
        if item_path != path and str(item.get("server_filename") or "") != name:
            continue
        if isdir is not None and bool(item.get("isdir")) != isdir:
            continue
        return item.get("fs_id")
    return None


async def _baidu_meta(
    client: httpx.AsyncClient, token: str, remote_path: str
) -> str | int | None:
    params = _baidu_query(
        token,
        {
            "target": json.dumps([remote_path], ensure_ascii=False),
            "dlink": "0",
        },
    )
    response = await client.get("https://pan.baidu.com/api/filemetas", params=params)
    if response.status_code >= 400:
        return None
    return _fs_id(_json_data(response))


def _baidu_match_dir(items: Sequence[Any], name: str) -> dict[str, Any] | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("server_filename") or "") != name:
            continue
        if not item.get("isdir"):
            continue
        return item
    return None


async def _baidu_list_dir(
    client: httpx.AsyncClient, token: str, remote_dir: str
) -> tuple[str, list[dict[str, Any]]]:
    """返回 ok / missing / unauth。unauth 时改走搜索，不要整次失败。"""
    path = str(remote_dir or "/").rstrip("/") or "/"
    response = await client.get(
        "https://pan.baidu.com/api/list",
        params=_baidu_query(
            token,
            {
                "dir": path,
                "order": "name",
                "desc": "0",
                "num": "1000",
                "page": "1",
                "showempty": "1",
            },
        ),
    )
    data = _json_data(response)
    errno = data.get("errno")
    if errno in {0, None}:
        items = [
            item for item in (data.get("list") or []) if isinstance(item, dict)
        ]
        return "ok", items
    if errno in _BAIDU_DIR_MISSING:
        return "missing", []
    if errno in _BAIDU_LIST_UNAUTH:
        logger.warning("百度 list 不可用 errno=%s path=%s，改用搜索", errno, path)
        return "unauth", []
    if response.status_code >= 400:
        raise CloudError(
            f"百度列目录失败：{safe_response_snippet(response, limit=300)}"
        )
    raise CloudError(f"百度列目录失败：{data}")


def _baidu_child_payload(name: str, path: str, fs_id: Any) -> dict[str, Any]:
    return {
        "fs_id": fs_id,
        "isdir": 1,
        "server_filename": name,
        "path": path,
    }


async def _baidu_find_child(
    client: httpx.AsyncClient, token: str, parent: str, name: str
) -> tuple[str, dict[str, Any] | None]:
    wanted = _join_remote(parent, name)
    status, items = await _baidu_list_dir(client, token, parent)
    if status == "ok":
        child = _baidu_match_dir(items, name)
        if child is not None:
            return "ok", child
    elif status == "missing":
        return "missing", None
    fs_id = await _baidu_search_fs_id(client, token, wanted, isdir=True)
    if fs_id:
        return "ok", _baidu_child_payload(name, wanted, fs_id)
    if status == "unauth":
        return "unauth", None
    return "ok", None


async def _baidu_create_dir(
    client: httpx.AsyncClient, token: str, remote_folder: str
) -> dict[str, Any]:
    response = await client.post(
        "https://pan.baidu.com/api/create",
        params=_baidu_query(token, {"a": "commit"}),
        data={
            "path": remote_folder,
            "isdir": "1",
            "block_list": "[]",
            "rtype": "0",
        },
    )
    if response.status_code >= 400:
        raise CloudError(
            f"百度创建目录失败：{safe_response_snippet(response, limit=300)}"
        )
    payload = _json_data(response)
    if payload.get("errno") not in _BAIDU_DIR_EXISTS:
        raise CloudError(f"百度创建目录失败：{payload}")
    return payload


async def _baidu_ensure_folder(
    client: httpx.AsyncClient, token: str, remote_folder: str
) -> str | int:
    """只在已确认的父目录下创建下一级。禁止无 a=commit / 自动改名导致根目录乱堆文件夹。"""
    segments = _path_segments(remote_folder)
    if not segments:
        raise CloudError("百度远程目录不能为空")
    parent = "/"
    current = ""
    final_id: str | int | None = None
    for segment in segments:
        current = _join_remote(current, segment)
        status, child = await _baidu_find_child(client, token, parent, segment)
        if status == "missing" and parent != "/":
            raise CloudError(
                f"百度指定目录不存在：{parent}。请确认设置里的远程目录和网盘实际路径一致"
            )
        if child is None:
            payload = await _baidu_create_dir(client, token, current)
            final_id = (
                _fs_id(payload)
                or await _baidu_meta(client, token, current)
                or await _baidu_search_fs_id(client, token, current, isdir=True)
            )
            if not final_id:
                _status, child = await _baidu_find_child(client, token, parent, segment)
                if child is not None:
                    final_id = child.get("fs_id")
            if not final_id:
                raise CloudError(
                    f"百度没有在 {parent} 下建到「{segment}」，已停止以免乱建目录"
                )
            parent = current
        else:
            final_id = child.get("fs_id")
            parent = str(child.get("path") or current).rstrip("/") or "/"
    if not final_id:
        raise CloudError("百度目录已就绪，但无法取得目录 ID")
    logger.info("百度目录就绪 %s id=%s", remote_folder, final_id)
    return final_id


async def _baidu_host_reachable(host: str, timeout: float = 5.0) -> bool:
    if get_proxy_runtime_settings()["enabled"]:
        # A raw TCP probe would bypass an HTTP/SOCKS application proxy. Let the
        # proxied upload request determine reachability instead.
        return True
    try:
        connection = asyncio.open_connection(host, 443)
        reader, writer = await asyncio.wait_for(connection, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        del reader
        return True
    except Exception:
        return False


async def _baidu_upload_host(
    client: httpx.AsyncClient, *, skip: set[str] | None = None
) -> str:
    """海外 c.pcs.baidu.com 常能 TCP 通但上传极慢，不当首选。"""
    skip = {item.strip() for item in (skip or set()) if str(item).strip()}
    skip.add("c.pcs.baidu.com")
    candidates: list[str] = []
    try:
        response = await client.get(
            "https://pcs.baidu.com/rest/2.0/pcs/file",
            params={"method": "locateupload", "app_id": "250528", "upload_version": "2.0"},
        )
        data = _json_data(response)
        host = str(data.get("host") or "").strip().split(":")[0]
        servers = data.get("servers")
        if not host and isinstance(servers, list) and servers:
            first = servers[0] if isinstance(servers[0], dict) else {}
            host = str(first.get("server") or first.get("host") or "").strip().split(":")[0]
        if host and host not in skip and host not in candidates:
            candidates.append(host)
    except Exception as exc:
        logger.warning("百度 locateupload 失败：%s", exc)
    for host in _BAIDU_UPLOAD_HOSTS:
        if host not in skip and host not in candidates:
            candidates.append(host)
    if "c.pcs.baidu.com" not in candidates:
        candidates.append("c.pcs.baidu.com")
    for host in candidates:
        if host in skip and host != "c.pcs.baidu.com":
            continue
        if await _baidu_host_reachable(host):
            logger.info("百度选用可连通上传节点 %s", host)
            return host
        logger.warning("百度上传节点不可达，跳过 %s", host)
    return next((item for item in candidates if item not in skip), "d.pcs.baidu.com")


async def _baidu_upload_file(
    client: httpx.AsyncClient,
    token: str,
    path: Path,
    remote_path: str,
    remote_name: str,
    progress: ProgressFn | None = None,
) -> str | int:
    blocks, _md5, size = _md5_blocks(path)
    block_list = json.dumps(blocks)
    pre = await client.post(
        "https://pan.baidu.com/api/precreate",
        params=_baidu_query(token),
        data={
            "path": remote_path,
            "autoinit": "1",
            "isdir": "0",
            "rtype": "3",
            "size": str(size),
            "block_list": block_list,
        },
    )
    if pre.status_code >= 400:
        raise CloudError(
            f"百度预创建失败：{safe_response_snippet(pre, limit=300)}"
        )
    pre_data = _json_data(pre)
    if pre_data.get("errno") not in {0, None} and pre_data.get("return_type") != 2:
        raise CloudError(f"百度预创建失败：{pre_data}")
    upload_id = str(pre_data.get("uploadid") or "")
    if pre_data.get("return_type") != 2:
        total_parts = len(blocks)
        upload_host = await _baidu_upload_host(client)
        upload_url = f"https://{upload_host}/rest/2.0/pcs/superfile2"
        logger.info("百度分片上传节点 %s parts=%s size=%s", upload_host, total_parts, size)
        with path.open("rb") as handle:
            for index in range(total_parts):
                await _maybe_progress(
                    progress,
                    "baidu",
                    index,
                    total_parts,
                    f"百度分片 {index + 1}/{total_parts}",
                )
                chunk = handle.read(4 * 1024 * 1024)
                last_error = "未知错误"
                for attempt in range(1, 8):
                    try:
                        logger.info(
                            "百度开始上传分片 %s/%s attempt=%s bytes=%s host=%s",
                            index + 1,
                            total_parts,
                            attempt,
                            len(chunk),
                            upload_host,
                        )
                        part = await client.post(
                            upload_url,
                            params={
                                "method": "upload",
                                "type": "tmpfile",
                                "app_id": "250528",
                                "path": remote_path,
                                "uploadid": upload_id,
                                "partseq": str(index),
                            },
                            files={"file": (remote_name, chunk, "application/octet-stream")},
                            timeout=httpx.Timeout(
                                connect=15.0, read=60.0, write=90.0, pool=15.0
                            ),
                        )
                    except httpx.TransportError as exc:
                        last_error = f"{type(exc).__name__}: {exc or 'connection dropped'}"
                        logger.warning(
                            "百度分片 %s 第 %s 次网络失败，重试：%s",
                            index,
                            attempt,
                            last_error[:160],
                        )
                        if isinstance(
                            exc,
                            (
                                httpx.ConnectTimeout,
                                httpx.ConnectError,
                                httpx.WriteTimeout,
                                httpx.ReadTimeout,
                            ),
                        ):
                            nxt = await _baidu_upload_host(client, skip={upload_host})
                            if nxt != upload_host:
                                logger.info("百度切换上传节点 %s -> %s", upload_host, nxt)
                                upload_host = nxt
                                upload_url = f"https://{upload_host}/rest/2.0/pcs/superfile2"
                        await asyncio.sleep(min(3 * attempt, 20))
                        continue
                    data = _json_data(part)
                    err = data.get("error_code", data.get("errno"))
                    if part.status_code < 400 and err in {None, 0, "0"}:
                        last_error = ""
                        break
                    last_error = safe_response_snippet(part, limit=300)
                    if attempt < 7:
                        logger.warning(
                            "百度分片 %s 第 %s 次失败，重试：%s",
                            index,
                            attempt,
                            last_error[:120],
                        )
                        await asyncio.sleep(min(3 * attempt, 20))
                if last_error:
                    raise CloudError(f"百度分片上传失败：{last_error}")
        await _maybe_progress(
            progress,
            "baidu",
            total_parts,
            total_parts,
            "百度分片已传完，正在创建文件",
        )
        created = await client.post(
            "https://pan.baidu.com/api/create",
            params=_baidu_query(token, {"a": "commit"}),
            data={
                "path": remote_path,
                "size": str(size),
                "isdir": "0",
                "rtype": "3",
                "block_list": block_list,
                "uploadid": upload_id,
            },
        )
        if created.status_code >= 400:
            raise CloudError(
                f"百度创建文件失败：{safe_response_snippet(created, limit=300)}"
            )
        created_data = _json_data(created)
        if created_data.get("errno") not in {None, 0}:
            raise CloudError(f"百度创建文件失败：{created_data}")
    else:
        created_data = pre_data
    result = _fs_id(created_data) or await _baidu_meta(client, token, remote_path)
    if not result:
        raise CloudError("百度上传成功但未返回 fs_id")
    return result


def _baidu_fid_list(ids: Sequence[str | int] | str | int) -> list[int | str]:
    values = ids if isinstance(ids, (list, tuple)) else [ids]
    result: list[int | str] = []
    for item in values:
        if item in {None, ""}:
            continue
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            result.append(item)
    return result


def _baidu_normalize_share_url(link: str, pwd: str) -> str:
    raw = str(link or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        url = raw
    else:
        surl = raw.lstrip("/")
        if surl[:2].lower() == "s/":
            surl = surl[2:]
        url = f"https://pan.baidu.com/s/{surl}"
    return _with_share_pwd(url, pwd)


async def _baidu_confirm_permanent_share(
    client: httpx.AsyncClient,
    token: str,
    *,
    shareid: Any,
    shorturl: str,
    fid: str | int | None,
) -> None:
    """分享记录里 expiredType=1 且 status=0 才是永久；-1/status=9 是立刻失效。"""
    params: dict[str, str] = {
        "channel": "chunlei",
        "clienttype": "0",
        "web": "1",
        "page": "1",
        "num": "10",
        "app_id": "250528",
    }
    if token:
        params["bdstoken"] = token
    try:
        response = await client.get("https://pan.baidu.com/share/record", params=params)
    except Exception as exc:
        logger.warning("百度分享记录查询失败：%s", exc)
        return
    data = _json_data(response)
    if data.get("errno") not in {None, 0}:
        return
    fid_s = str(fid or "")
    shareid_s = str(shareid or "")
    short = str(shorturl or "")
    for item in data.get("list") or []:
        if not isinstance(item, dict):
            continue
        fsids = [str(value) for value in (item.get("fsIds") or item.get("fs_ids") or [])]
        sl = str(item.get("shortlink") or item.get("shorturl") or "")
        sid = str(item.get("shareId") or item.get("shareid") or "")
        matched = (
            (shareid_s and sid == shareid_s)
            or (short and short in sl)
            or (fid_s and fid_s in fsids)
        )
        if not matched:
            continue
        path = str(item.get("typicalPath") or "")
        expired_type = item.get("expiredType")
        status = item.get("status")
        if (
            expired_type in {-1, "-1"}
            or status in {9, "9"}
            or path in _BAIDU_EXPIRED_SHARE_MARKERS
        ):
            raise CloudError(
                f"百度文件夹分享未生效（已过期）。link={sl or short} path={path}"
            )
        logger.info(
            "百度分享已确认永久 typicalPath=%s expiredType=%s", path, expired_type
        )
        return


async def _baidu_share(
    client: httpx.AsyncClient,
    token: str,
    file_ids: Sequence[str | int] | str | int,
    pwd: str,
) -> str:
    """分享文件或文件夹。period=0 + expiredType=1 对应网页端「永久有效」。

    /share/set 成功时 expiredType=0 表示永久；分享记录里则记成 expiredType=1。
    文件夹也可以永久分享，前提是 fid 必须是目标目录本身，不能误用上级目录。
    """
    pwd = _share_extract_code(pwd)
    fid_list = _baidu_fid_list(file_ids)
    if not fid_list:
        raise CloudError("百度分享缺少文件或目录 ID")
    params = {
        "channel": "chunlei",
        "web": "1",
        "app_id": "250528",
        "clienttype": "0",
    }
    if token:
        params["bdstoken"] = token
    share = await client.post(
        "https://pan.baidu.com/share/set",
        params=params,
        data={
            "schannel": "4",
            "channel_list": "[]",
            "period": "0",
            "expiredType": "1",
            "pwd": pwd,
            "fid_list": json.dumps(fid_list),
        },
    )
    if share.status_code >= 400:
        raise CloudError(
            f"百度创建分享失败：{safe_response_snippet(share, limit=300)}"
        )
    data = _json_data(share)
    if data.get("errno") not in {None, 0}:
        raise CloudError(f"百度创建分享失败：{data}")
    expired_type = data.get("expiredType")
    if expired_type not in _BAIDU_PERMANENT_EXPIRED_TYPES:
        logger.warning("百度分享有效期异常 expiredType=%s data=%s", expired_type, data)
    link = _baidu_normalize_share_url(
        str(data.get("link") or data.get("shorturl") or ""),
        str(data.get("pwd") or pwd),
    )
    if not link:
        raise CloudError(f"百度未返回分享链接：{data}")
    await _baidu_confirm_permanent_share(
        client,
        token,
        shareid=data.get("shareid"),
        shorturl=str(data.get("shorturl") or data.get("link") or ""),
        fid=fid_list[0],
    )
    return link


async def upload_baidu_folder(
    settings: GamesSettings,
    sources: Sequence[Path],
    folder_name: str,
    *,
    prepare: PrepareFn | None = None,
    progress: ProgressFn | None = None,
    share_pwd: str | None = None,
) -> str:
    cookie = _cookie_header(settings.baidu_cookie)
    if not cookie:
        raise CloudError("未配置百度网盘 Cookie（BDUSS）")
    cookies = _cookie_dict(cookie, "BDUSS")
    headers = {
        "User-Agent": "netdisk;1.0",
        "Referer": "https://pan.baidu.com/disk/home",
    }
    share_pwd = _share_extract_code(share_pwd)
    remote_folder = _join_remote(target_base_dir(settings, "baidu"), folder_name)
    logger.info("百度上传到 %s", remote_folder)
    total = len(sources)
    async with _baidu_http_client(cookies, headers) as client:
        try:
            token = await _baidu_token(client)
            folder_id = await _baidu_ensure_folder(client, token, remote_folder)
            for index, source in enumerate(sources, start=1):
                item = await _prepare(source, index, total, prepare)
                try:
                    await _maybe_progress(
                        progress, "baidu", index - 1, total, f"百度上传 {index}/{total}"
                    )
                    await _baidu_upload_file(
                        client,
                        token,
                        item.path,
                        _join_remote(remote_folder, item.remote_name),
                        item.remote_name,
                        progress=progress,
                    )
                    await _maybe_progress(
                        progress, "baidu", index, total, f"百度已上传 {index}/{total}"
                    )
                finally:
                    _cleanup_prepared(item)
            share_id = (
                await _baidu_search_fs_id(
                    client, token, remote_folder, isdir=True
                )
                or folder_id
            )
            if not share_id:
                raise CloudError("百度目录已上传，但无法取得目录 ID，无法创建文件夹分享")
            return await _baidu_share(client, token, [share_id], share_pwd)
        finally:
            persist_client_cookies(
                "baidu_cookie", settings.baidu_cookie, "BDUSS", client
            )


async def upload_baidu(settings: GamesSettings, path: Path, remote_name: str) -> str:
    folder = baidu_title_folder(Path(remote_name).stem)

    def rename(source: Path, _index: int, _total: int) -> PreparedUpload:
        return PreparedUpload(source, remote_name)

    return await upload_baidu_folder(settings, [path], folder, prepare=rename)


def _pikpak_device_id(settings: GamesSettings) -> str:
    seed = str(settings.pikpak_username or settings.pikpak_refresh_token or "tg-signpulse")
    return hashlib.md5(f"games-pikpak:{seed}".encode("utf-8")).hexdigest()


async def _pikpak_captcha_token(
    client: httpx.AsyncClient, device_id: str, username: str
) -> str:
    meta: dict[str, str] = {"username": username}
    if "@" in username:
        meta["email"] = username
    else:
        meta["phone_number"] = username
    response = await client.post(
        "https://user.mypikpak.com/v1/shield/captcha/init",
        params={"client_id": PIKPAK_CLIENT_ID},
        headers={
            "x-client-id": PIKPAK_CLIENT_ID,
            "x-device-id": device_id,
        },
        json={
            "client_id": PIKPAK_CLIENT_ID,
            "action": "POST:/v1/auth/signin",
            "device_id": device_id,
            "captcha_token": "",
            "meta": meta,
        },
    )
    data = _json_data(response)
    token = str(data.get("captcha_token") or "").strip()
    if data.get("url"):
        raise CloudError(
            "PikPak 账号登录需要人机验证。请在网页登录后填写 refresh_token，不要只填密码"
        )
    if response.status_code >= 400 or not token:
        detail = data or safe_response_snippet(response, limit=300)
        raise CloudError(f"PikPak 验证码初始化失败：{detail}")
    return token


async def _pikpak_login(settings: GamesSettings, client: httpx.AsyncClient) -> str:
    device_id = _pikpak_device_id(settings)
    refresh = str(settings.pikpak_refresh_token or "").strip()
    if refresh:
        response = await client.post(
            "https://user.mypikpak.com/v1/auth/token",
            json={
                "client_id": PIKPAK_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
        )
        if response.status_code < 400:
            data = _json_data(response)
            token = data.get("access_token")
            next_refresh = str(data.get("refresh_token") or "").strip()
            if next_refresh and next_refresh != refresh:
                try:
                    save_games_settings({"pikpak_refresh_token": next_refresh})
                except Exception:
                    logger.warning("无法保存更新后的 PikPak refresh_token")
            if token:
                return str(token)
    user = str(settings.pikpak_username or "").strip()
    password = str(settings.pikpak_password or "").strip()
    if not user or not password:
        raise CloudError("未配置 PikPak 账号或 refresh_token")
    captcha = await _pikpak_captcha_token(client, device_id, user)
    response = await client.post(
        "https://user.mypikpak.com/v1/auth/signin",
        headers={
            "x-client-id": PIKPAK_CLIENT_ID,
            "x-device-id": device_id,
            "x-captcha-token": captcha,
        },
        json={
            "client_id": PIKPAK_CLIENT_ID,
            "captcha_token": captcha,
            "username": user,
            "password": password,
        },
    )
    data = _json_data(response)
    if response.status_code >= 400:
        if str(data.get("error") or "") in {"captcha_required", "captcha_invalid"}:
            raise CloudError(
                "PikPak 密码登录被验证码拦截。请在 https://mypikpak.com 登录后，"
                "从开发者工具复制 refresh_token 填到设置里"
            )
        raise CloudError(
            f"PikPak 登录失败：{safe_response_snippet(response, limit=300)}"
        )
    token = data.get("access_token")
    next_refresh = str(data.get("refresh_token") or "").strip()
    if next_refresh:
        try:
            save_games_settings({"pikpak_refresh_token": next_refresh})
        except Exception:
            logger.warning("无法保存 PikPak refresh_token")
    if not token:
        raise CloudError("PikPak 未返回 access_token")
    return str(token)


async def _pikpak_find_folder(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    parent_id: str,
    name: str,
) -> str | None:
    response = await client.get(
        "https://api-drive.mypikpak.com/drive/v1/files",
        headers=headers,
        params={"parent_id": parent_id, "limit": "100", "with_audit": "true"},
    )
    if response.status_code >= 400:
        raise CloudError(
            f"PikPak 读取目录失败：{safe_response_snippet(response, limit=300)}"
        )
    files = _json_data(response).get("files")
    if not isinstance(files, list):
        return None
    for item in files:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "drive#folder" and str(item.get("name")) == name:
            value = item.get("id")
            return str(value) if value else None
    return None


async def _pikpak_create_folder(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    parent_id: str,
    name: str,
) -> str:
    existing = await _pikpak_find_folder(client, headers, parent_id, name)
    if existing:
        return existing
    body: dict[str, Any] = {"kind": "drive#folder", "name": name}
    if parent_id:
        body["parent_id"] = parent_id
    response = await client.post(
        "https://api-drive.mypikpak.com/drive/v1/files",
        headers=headers,
        json=body,
    )
    if response.status_code >= 400:
        raise CloudError(
            f"PikPak 创建目录失败：{safe_response_snippet(response, limit=300)}"
        )
    data = _json_data(response)
    item = data.get("file") if isinstance(data.get("file"), dict) else data
    folder_id = item.get("id") if isinstance(item, dict) else None
    if not folder_id:
        raise CloudError("PikPak 创建目录后未返回目录 ID")
    return str(folder_id)


async def _pikpak_ensure_folder(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    root_id: str,
    remote_dir: str,
    game_folder: str,
) -> str:
    parent_id = root_id
    for segment in [*_path_segments(remote_dir), game_folder]:
        parent_id = await _pikpak_create_folder(client, headers, parent_id, segment)
    return parent_id


def _pikpak_gcid(path: Path) -> str:
    size = path.stat().st_size
    block = 256 * 1024
    while size / block > 512 and block < 2 * 1024 * 1024:
        block *= 2
    overall = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(block)
            if not chunk:
                break
            overall.update(hashlib.sha1(chunk).digest())
    return overall.hexdigest().upper()


async def _pikpak_upload_file(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    parent_id: str,
    path: Path,
    remote_name: str,
) -> str:
    body: dict[str, Any] = {
        "kind": "drive#file",
        "name": remote_name,
        "size": str(path.stat().st_size),
        "hash": _pikpak_gcid(path),
        "upload_type": "UPLOAD_TYPE_RESUMABLE",
        "folder_type": "NORMAL",
        "parent_id": parent_id,
    }
    created = await client.post(
        "https://api-drive.mypikpak.com/drive/v1/files",
        headers=headers,
        json=body,
    )
    if created.status_code >= 400:
        raise CloudError(
            f"PikPak 创建上传失败：{safe_response_snippet(created, limit=300)}"
        )
    data = _json_data(created)
    file_info = data.get("file") if isinstance(data.get("file"), dict) else {}
    file_id = file_info.get("id") if isinstance(file_info, dict) else data.get("id")
    phase = str(file_info.get("phase") or "")
    if phase == "PHASE_TYPE_COMPLETE" and file_id:
        return str(file_id)
    resumable = data.get("resumable")
    params = resumable.get("params") if isinstance(resumable, dict) else None
    if isinstance(params, dict):
        await asyncio.to_thread(_pikpak_s3_upload, params, path)
    if not file_id:
        raise CloudError("PikPak 未返回文件 ID")
    return str(file_id)


def _pikpak_s3_upload(params: dict[str, Any], path: Path) -> None:
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:
        raise CloudError(f"PikPak 上传需要 boto3：{exc}") from exc
    access = str(params.get("access_key_id") or "")
    secret = str(params.get("access_key_secret") or "")
    session = str(params.get("security_token") or "")
    bucket = str(params.get("bucket") or "")
    key = str(params.get("key") or "")
    if not (access and secret and bucket and key):
        raise CloudError("PikPak 未返回完整的 OSS 上传凭证")
    endpoint = str(params.get("endpoint") or "mypikpak.com").strip()
    if endpoint.startswith("upload-") and endpoint.endswith(".mypikpak.com"):
        endpoint = "mypikpak.com"
    if not endpoint.startswith("http"):
        endpoint = "https://" + endpoint
    client = boto3.client(
        "s3",
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        aws_session_token=session or None,
        endpoint_url=endpoint,
        region_name="ap-southeast-1",
        config=botocore_config_with_proxy(
            Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
            endpoint_url=endpoint,
        ),
    )
    client.upload_file(str(path), bucket, key)


async def _pikpak_share(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    folder_id: str,
    pwd: str = "",
) -> str:
    pwd = _share_extract_code(pwd) if pwd else ""
    body: dict[str, Any] = {
        "file_ids": [folder_id],
        "share_to": "publiclink",
        "expiration_days": -1,
        "pass_code_option": "REQUIRED" if pwd else "NOT_REQUIRED",
    }
    if pwd:
        body["pass_code"] = pwd
    share = await client.post(
        "https://api-drive.mypikpak.com/drive/v1/share",
        headers=headers,
        json=body,
    )
    if share.status_code >= 400 and pwd:
        logger.warning(
            "PikPak 指定提取码失败，改用 password 字段重试：%s",
            safe_response_snippet(share, limit=200),
        )
        alt = dict(body)
        alt["password"] = pwd
        retry = await client.post(
            "https://api-drive.mypikpak.com/drive/v1/share",
            headers=headers,
            json=alt,
        )
        if retry.status_code < 400:
            share = retry
    if share.status_code >= 400:
        raise CloudError(
            f"PikPak 分享失败：{safe_response_snippet(share, limit=300)}"
        )
    data = _json_data(share)
    # PikPak 会忽略请求里的 pass_code，以返回值为准。
    actual = str(data.get("pass_code") or data.get("password") or pwd or "")
    if pwd and actual and actual != pwd:
        logger.info("PikPak 使用平台生成的提取码 %s（请求为 %s）", actual, pwd)
    url = data.get("share_url") or data.get("share_id")
    if isinstance(url, str) and url.startswith("http"):
        return _with_share_pwd(url, actual)
    if data.get("share_id"):
        return _with_share_pwd(
            f"https://mypikpak.com/s/{data['share_id']}",
            actual,
        )
    raise CloudError(f"PikPak 未返回分享链接：{data}")


async def upload_pikpak_folder(
    settings: GamesSettings,
    sources: Sequence[Path],
    folder_name: str,
    *,
    prepare: PrepareFn | None = None,
    progress: ProgressFn | None = None,
    share_pwd: str | None = None,
) -> str:
    total = len(sources)
    async with httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(None), follow_redirects=True
        )
    ) as client:
        token = await _pikpak_login(settings, client)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        folder_id = await _pikpak_ensure_folder(
            client,
            headers,
            str(settings.pikpak_folder_id or "").strip(),
            settings.pikpak_remote_dir or "/games",
            folder_name,
        )
        for index, source in enumerate(sources, start=1):
            item = await _prepare(source, index, total, prepare)
            try:
                await _maybe_progress(
                    progress, "pikpak", index - 1, total, f"PikPak 上传 {index}/{total}"
                )
                await _pikpak_upload_file(
                    client, headers, folder_id, item.path, item.remote_name
                )
                await _maybe_progress(
                    progress, "pikpak", index, total, f"PikPak 已上传 {index}/{total}"
                )
            finally:
                _cleanup_prepared(item)
        return await _pikpak_share(
            client, headers, folder_id, _share_extract_code(share_pwd)
        )


async def _pikpak_file_download_url(
    client: httpx.AsyncClient, headers: dict[str, str], file_id: str
) -> str:
    response = await client.get(
        f"https://api-drive.mypikpak.com/drive/v1/files/{file_id}",
        headers=headers,
    )
    if response.status_code >= 400:
        raise CloudError(
            f"PikPak 读取文件失败：{safe_response_snippet(response, limit=300)}"
        )
    data = _json_data(response)
    links = data.get("links") if isinstance(data.get("links"), dict) else {}
    octet = links.get("application/octet-stream") if isinstance(links, dict) else {}
    url = (
        str(data.get("web_content_link") or "").strip()
        or str(octet.get("url") if isinstance(octet, dict) else "").strip()
        or str(data.get("medias") or "")
    )
    if url.startswith("http"):
        return url
    raise CloudError(f"PikPak 未返回下载地址：{data.get('name') or file_id}")


async def download_pikpak_folder(
    settings: GamesSettings,
    folder_name: str,
    dest_dir: Path,
    *,
    progress: ProgressFn | None = None,
) -> list[Path]:
    """把已上传到 PikPak 的游戏目录拉回本地，便于补传失败网盘。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(None), follow_redirects=True
        )
    ) as client:
        token = await _pikpak_login(settings, client)
        headers = {"Authorization": f"Bearer {token}", "x-device-id": _pikpak_device_id(settings)}
        folder_id = await _pikpak_ensure_folder(
            client,
            headers,
            str(settings.pikpak_folder_id or ""),
            settings.pikpak_remote_dir or "/games",
            folder_name,
        )
        listed = await client.get(
            "https://api-drive.mypikpak.com/drive/v1/files",
            headers=headers,
            params={"parent_id": folder_id, "limit": "100"},
        )
        if listed.status_code >= 400:
            raise CloudError(
                f"PikPak 列出目录失败：{safe_response_snippet(listed, limit=300)}"
            )
        files = [
            item
            for item in (_json_data(listed).get("files") or [])
            if isinstance(item, dict) and item.get("kind") != "drive#folder"
        ]
        if not files:
            raise CloudError(f"PikPak 目录为空：{folder_name}")
        saved: list[Path] = []
        total = len(files)
        for index, item in enumerate(files, start=1):
            name = str(item.get("name") or f"file-{index}")
            dest = dest_dir / Path(name).name
            await _maybe_progress(
                progress, "pikpak", index - 1, total, f"PikPak 下载 {index}/{total}"
            )
            url = await _pikpak_file_download_url(client, headers, str(item.get("id")))
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise CloudError(f"PikPak 下载失败 {name}：HTTP {response.status_code}")
                with dest.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        handle.write(chunk)
            if dest.stat().st_size <= 0:
                dest.unlink(missing_ok=True)
                raise CloudError(f"PikPak 下载空文件：{name}")
            saved.append(dest)
            await _maybe_progress(
                progress, "pikpak", index, total, f"PikPak 已下载 {index}/{total}"
            )
        return saved


async def upload_pikpak(settings: GamesSettings, path: Path, remote_name: str) -> str:
    folder = _safe_original_folder(Path(remote_name).stem)

    def rename(source: Path, _index: int, _total: int) -> PreparedUpload:
        return PreparedUpload(source, remote_name)

    return await upload_pikpak_folder(settings, [path], folder, prepare=rename)


def _parse_template_data(html: str) -> dict[str, Any]:
    marker = "var templateData = "
    start = (html or "").find(marker)
    if start < 0:
        return {}
    decoder = json.JSONDecoder()
    try:
        data, _end = decoder.raw_decode(html[start + len(marker) :])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_js_token(html: str) -> str:
    data = _parse_template_data(html)
    raw = str(data.get("jsToken") or "")
    match = _JS_TOKEN_RE.search(raw) or _JS_TOKEN_RE.search(html or "")
    if not match:
        return ""
    return match.group("enc") or match.group("plain") or ""


def _terabox_ok(payload: dict[str, Any]) -> bool:
    return payload.get("errno") in {None, 0, "0"}


def _terabox_fid_json(fs_id: str | int | None) -> str:
    if fs_id in {None, ""}:
        return ""
    try:
        return json.dumps([int(str(fs_id))])
    except (TypeError, ValueError):
        return json.dumps([fs_id])


def _terabox_share_payloads(
    remote_path: str, fs_id: str | int | None, pwd: str
) -> list[dict[str, str]]:
    """TeraBox /share/pset 认 path_list（路径），不是百度盘那套纯 fid_list。"""
    path_list = json.dumps([remote_path], ensure_ascii=False)
    fid_list = _terabox_fid_json(fs_id)
    payloads: list[dict[str, str]] = [
        {
            "schannel": "4",
            "channel_list": "[]",
            "period": "0",
            "path_list": path_list,
            "pwd": pwd,
        },
        {
            "schannel": "0",
            "channel_list": "[]",
            "period": "0",
            "path_list": path_list,
        },
    ]
    if fid_list:
        payloads.append(
            {
                "schannel": "4",
                "channel_list": "[]",
                "period": "0",
                "path_list": path_list,
                "fid_list": fid_list,
                "pwd": pwd,
            }
        )
    return payloads


def _terabox_normalize_share_url(data: dict[str, Any], pwd: str = "") -> str:
    raw = str(
        data.get("link")
        or data.get("shorturl")
        or data.get("sharelink")
        or data.get("typical")
        or ""
    ).strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        url = f"https:{raw}"
    elif raw.startswith(("http://", "https://")):
        url = raw
    elif raw.startswith("/"):
        url = f"{TERABOX_BASE}{raw}"
    else:
        url = f"{TERABOX_BASE}/s/{raw.lstrip('/')}"
    share_pwd = str(data.get("pwd") or pwd or "")
    if share_pwd and "pwd=" not in url:
        return _with_share_pwd(url, share_pwd)
    return url


def _apply_terabox_page_tokens(
    client: httpx.AsyncClient, html: str, headers: httpx.Headers | None = None
) -> str:
    data = _parse_template_data(html)
    token = _extract_js_token(html)
    bdstoken = str(data.get("bdstoken") or "")
    if not bdstoken:
        match = _BDSTOKEN_RE.search(html or "")
        bdstoken = match.group(1) if match else ""
    csrf = str(data.get("csrf") or client.cookies.get("csrfToken") or "")
    client._terabox_bdstoken = bdstoken  # type: ignore[attr-defined]
    client._terabox_csrf = csrf  # type: ignore[attr-defined]
    if headers is not None:
        logid = headers.get("logid") or headers.get("dp-logid")
        if logid:
            client._terabox_logid = str(logid)  # type: ignore[attr-defined]
    return token


def _terabox_params(js_token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    params = {
        "app_id": "250528",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
    }
    if js_token:
        params["jsToken"] = js_token
    if extra:
        params.update({key: value for key, value in extra.items() if value})
    return params


async def _terabox_refresh_js_token(client: httpx.AsyncClient) -> str:
    html = ""
    headers: httpx.Headers | None = None
    for path in ("/main", "/"):
        page = await client.get(f"{TERABOX_BASE}{path}")
        html = page.text or html
        headers = page.headers
        token = _apply_terabox_page_tokens(client, html, headers)
        if token:
            return token
    raise CloudError("TeraBox Cookie 有效，但无法从首页解析 jsToken，请重新复制完整 Cookie")


def _terabox_query_extra(client: httpx.AsyncClient) -> dict[str, str]:
    extra: dict[str, str] = {}
    bdstoken = str(getattr(client, "_terabox_bdstoken", "") or "")
    csrf = str(
        getattr(client, "_terabox_csrf", "") or client.cookies.get("csrfToken") or ""
    )
    logid = str(getattr(client, "_terabox_logid", "") or "")
    if bdstoken:
        extra["bdstoken"] = bdstoken
    if csrf:
        extra["csrfToken"] = csrf
    if logid:
        extra["dp-logid"] = logid
    return extra


async def _terabox_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    js_token: str,
    *,
    data: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    url = path if path.startswith("http") else f"{TERABOX_BASE}{path}"
    request = client.build_request(
        method,
        url,
        params=_terabox_params(js_token, extra),
        data=data,
        headers=headers,
    )
    response = await client.send(request)
    payload = _json_data(response)
    errno = payload.get("errno")
    if errno == 4000023:
        js_token = await _terabox_refresh_js_token(client)
        request = client.build_request(
            method,
            url,
            params=_terabox_params(js_token, extra),
            data=data,
            headers=headers,
        )
        response = await client.send(request)
        payload = _json_data(response)
    if looks_like_html(response.text):
        raise CloudError(
            f"TeraBox 请求失败：{safe_response_snippet(response, limit=300)}"
        )
    if response.status_code >= 400:
        if not payload:
            payload = {
                "errno": response.status_code,
                "errmsg": safe_response_snippet(response, limit=180),
            }
        elif _terabox_ok(payload):
            payload["errno"] = response.status_code
    return payload, js_token


async def _terabox_meta(
    client: httpx.AsyncClient, js_token: str, remote_path: str
) -> str | int | None:
    payload, _token = await _terabox_json(
        client,
        "GET",
        "/api/filemetas",
        js_token,
        extra={
            "target": json.dumps([remote_path], ensure_ascii=False),
            "dlink": "0",
        },
    )
    return _fs_id(payload)


async def _terabox_ensure_folder(
    client: httpx.AsyncClient, js_token: str, remote_folder: str
) -> tuple[str | int, str]:
    current = ""
    final_id: str | int | None = None
    for segment in _path_segments(remote_folder):
        current = _join_remote(current, segment)
        data, js_token = await _terabox_json(
            client,
            "POST",
            "/api/create",
            js_token,
            data={"path": current, "isdir": "1", "block_list": "[]"},
        )
        if data.get("errno") not in {None, 0, -8}:
            raise CloudError(f"TeraBox 创建目录失败：{data}")
        final_id = _fs_id(data) or await _terabox_meta(client, js_token, current)
    if not final_id:
        raise CloudError("TeraBox 目录已创建，但无法取得目录 ID")
    return final_id, js_token


async def _terabox_upload_file(
    client: httpx.AsyncClient,
    js_token: str,
    path: Path,
    remote_path: str,
    remote_name: str,
) -> tuple[str | int, str]:
    blocks, _md5, size = _md5_blocks(path)
    block_list = json.dumps(blocks)
    pre_data, js_token = await _terabox_json(
        client,
        "POST",
        "/api/precreate",
        js_token,
        data={
            "path": remote_path,
            "autoinit": "1",
            "isdir": "0",
            "rtype": "3",
            "size": str(size),
            "block_list": block_list,
        },
    )
    if pre_data.get("errno") not in {None, 0} and pre_data.get("return_type") != 2:
        raise CloudError(f"TeraBox 预创建失败：{pre_data}")
    upload_id = str(pre_data.get("uploadid") or "")
    if pre_data.get("return_type") != 2:
        with path.open("rb") as handle:
            for index in range(len(blocks)):
                chunk = handle.read(4 * 1024 * 1024)
                uploaded = await client.post(
                    "https://c-jp.terabox.com/rest/2.0/pcs/superfile2",
                    params={
                        "method": "upload",
                        "type": "tmpfile",
                        "app_id": "250528",
                        "path": remote_path,
                        "uploadid": upload_id,
                        "partseq": str(index),
                    },
                    files={"file": (remote_name, chunk, "application/octet-stream")},
                )
                if uploaded.status_code >= 400:
                    raise CloudError(
                        "TeraBox 分片上传失败："
                        f"{safe_response_snippet(uploaded, limit=300)}"
                    )
        created_data, js_token = await _terabox_json(
            client,
            "POST",
            "/api/create",
            js_token,
            data={
                "path": remote_path,
                "size": str(size),
                "uploadid": upload_id,
                "isdir": "0",
                "rtype": "3",
                "block_list": block_list,
            },
        )
        if created_data.get("errno") not in {None, 0}:
            raise CloudError(f"TeraBox 创建文件失败：{created_data}")
    else:
        created_data = pre_data
    result = _fs_id(created_data) or await _terabox_meta(client, js_token, remote_path)
    if not result:
        raise CloudError("TeraBox 上传成功但未返回 fs_id")
    return result, js_token


async def _terabox_share(
    client: httpx.AsyncClient,
    js_token: str,
    remote_path: str,
    fs_id: str | int | None,
    pwd: str,
) -> str:
    pwd = _share_extract_code(pwd)
    extra = _terabox_query_extra(client)
    share_headers = {
        "User-Agent": TERABOX_APP_UA,
        "Origin": TERABOX_BASE,
        "Referer": f"{TERABOX_BASE}/main",
    }
    last: dict[str, Any] = {}
    for payload in _terabox_share_payloads(remote_path, fs_id, pwd):
        data, js_token = await _terabox_json(
            client,
            "POST",
            "/share/pset",
            js_token,
            data=payload,
            extra=extra or None,
            headers=share_headers,
        )
        last = data
        if not _terabox_ok(data):
            continue
        url = _terabox_normalize_share_url(
            data, pwd if payload.get("schannel") == "4" else ""
        )
        if url:
            return url
    raise CloudError(
        f"TeraBox 分享失败（path={remote_path} fid={fs_id}）：{last}"
    )


async def upload_terabox_folder(
    settings: GamesSettings,
    sources: Sequence[Path],
    folder_name: str,
    *,
    prepare: PrepareFn | None = None,
    progress: ProgressFn | None = None,
    share_pwd: str | None = None,
) -> str:
    cookies = _cookie_dict(settings.terabox_cookie, "ndus")
    if not any(key.casefold() == "ndus" for key in cookies):
        raise CloudError("未配置 TeraBox Cookie（ndus）")
    cookies.setdefault("lang", "en")
    remote_folder = _join_remote(settings.terabox_remote_dir or "/games", folder_name)
    total = len(sources)
    async with httpx.AsyncClient(
        **httpx_async_client_kwargs(
            timeout=httpx.Timeout(None),
            follow_redirects=True,
            cookies=cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": f"{TERABOX_BASE}/main",
                "Accept": "application/json, text/plain, */*",
            },
        )
    ) as client:
        try:
            js_token = await _terabox_refresh_js_token(client)
            folder_id, js_token = await _terabox_ensure_folder(
                client, js_token, remote_folder
            )
            uploaded: list[tuple[str, str | int]] = []
            for index, source in enumerate(sources, start=1):
                item = await _prepare(source, index, total, prepare)
                try:
                    await _maybe_progress(
                        progress,
                        "terabox",
                        index - 1,
                        total,
                        f"TeraBox 上传 {index}/{total}",
                    )
                    remote_path = _join_remote(remote_folder, item.remote_name)
                    file_id, js_token = await _terabox_upload_file(
                        client,
                        js_token,
                        item.path,
                        remote_path,
                        item.remote_name,
                    )
                    uploaded.append((remote_path, file_id))
                    await _maybe_progress(
                        progress, "terabox", index, total, f"TeraBox 已上传 {index}/{total}"
                    )
                finally:
                    _cleanup_prepared(item)
            last_error: CloudError | None = None
            for remote_path, share_id in ((remote_folder, folder_id), *uploaded):
                try:
                    return await _terabox_share(
                        client,
                        js_token,
                        remote_path,
                        share_id,
                        _share_extract_code(share_pwd),
                    )
                except CloudError as exc:
                    last_error = exc
            if last_error:
                raise last_error
            raise CloudError("TeraBox 上传完成但无法创建分享")
        finally:
            persist_client_cookies(
                "terabox_cookie", settings.terabox_cookie, "ndus", client
            )


async def upload_terabox(settings: GamesSettings, path: Path, remote_name: str) -> str:
    folder = _safe_original_folder(Path(remote_name).stem)

    def rename(source: Path, _index: int, _total: int) -> PreparedUpload:
        return PreparedUpload(source, remote_name)

    return await upload_terabox_folder(settings, [path], folder, prepare=rename)


def _quark_cookie_header(raw: str) -> str:
    text = _cookie_header(raw)
    if not text:
        return ""
    if "=" not in text:
        return f"__puus={text}"
    return text


def _quark_ok(payload: dict[str, Any]) -> bool:
    return payload.get("code") in {None, 0, "0"} and payload.get("status") in {
        None,
        0,
        200,
        "200",
    }


async def _quark_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    base = str(getattr(client, "_quark_base", "") or QUARK_API_BASES[0])
    query = {"pr": "ucpro", "fr": "pc"}
    if params:
        query.update({key: value for key, value in params.items() if value is not None})
    url = path if path.startswith("http") else f"{base}{path}"
    response = await client.request(
        method,
        url,
        params=query,
        json=json_body,
    )
    payload = _json_data(response)
    if looks_like_html(response.text):
        raise CloudError(
            f"夸克网盘请求失败：{safe_response_snippet(response, limit=300)}"
        )
    if response.status_code >= 400:
        message = payload.get("message") or payload.get("msg")
        if not message or looks_like_html(str(message)) or looks_like_html(response.text):
            message = safe_response_snippet(response, limit=300)
        raise CloudError(f"夸克网盘请求失败：{str(message)[:300]}")
    if not _quark_ok(payload):
        message = payload.get("message") or payload.get("msg") or payload
        raise CloudError(f"夸克网盘请求失败：{str(message)[:300]}")
    return payload


async def _quark_list(
    client: httpx.AsyncClient, parent_id: str
) -> list[dict[str, Any]]:
    payload = await _quark_json(
        client,
        "GET",
        "/file/sort",
        params={
            "pdir_fid": parent_id or "0",
            "_page": "1",
            "_size": "100",
            "_fetch_total": "1",
            "_fetch_sub_dirs": "0",
            "_sort": "file_type:asc,file_name:asc",
        },
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = data.get("list") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


async def _quark_find(
    client: httpx.AsyncClient, parent_id: str, name: str
) -> str | None:
    for item in await _quark_list(client, parent_id):
        if not isinstance(item, dict):
            continue
        if str(item.get("file_name") or "") == name and item.get("fid"):
            return str(item["fid"])
    return None


async def _quark_mkdir(
    client: httpx.AsyncClient, parent_id: str, name: str
) -> str:
    existing = await _quark_find(client, parent_id, name)
    if existing:
        return existing
    try:
        payload = await _quark_json(
            client,
            "POST",
            "/file",
            json_body={
                "pdir_fid": parent_id or "0",
                "file_name": name,
                "dir_path": "",
                "dir_init_lock": False,
            },
        )
    except CloudError as exc:
        if "同名" not in str(exc):
            raise
        found = await _quark_find(client, parent_id, name)
        if found:
            return found
        raise
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    fid = str((data or {}).get("fid") or "")
    if not fid:
        fid = await _quark_find(client, parent_id, name) or ""
    if not fid:
        raise CloudError(f"夸克创建目录未返回 fid：{payload}")
    return fid


async def _quark_ensure_folder(
    client: httpx.AsyncClient, parent_id: str, remote_dir: str, folder_name: str
) -> str:
    current = parent_id or "0"
    for segment in _path_segments(_join_remote(remote_dir, folder_name)):
        current = await _quark_mkdir(client, current, segment)
    return current


def _file_md5_sha1(path: Path) -> tuple[str, str]:
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
    return md5.hexdigest(), sha1.hexdigest()


def _quark_oss_url(pre: dict[str, Any]) -> str:
    bucket = str(pre.get("bucket") or "")
    obj_key = str(pre.get("obj_key") or "")
    upload_url = str(pre.get("upload_url") or "")
    host = upload_url.split("://", 1)[-1].rstrip("/")
    return f"https://{bucket}.{host}/{obj_key}"


async def _quark_oss_auth(
    client: httpx.AsyncClient, pre: dict[str, Any], auth_meta: str
) -> str:
    payload = await _quark_json(
        client,
        "POST",
        "/file/upload/auth",
        json_body={
            "auth_info": pre.get("auth_info"),
            "auth_meta": auth_meta,
            "task_id": pre.get("task_id"),
        },
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    key = str((data or {}).get("auth_key") or "")
    if not key:
        raise CloudError(f"夸克上传授权失败：{payload}")
    return key


async def _quark_upload_file(
    client: httpx.AsyncClient,
    path: Path,
    parent_id: str,
    remote_name: str,
) -> str:
    mime = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
    now_ms = int(path.stat().st_mtime * 1000)
    pre_payload = await _quark_json(
        client,
        "POST",
        "/file/upload/pre",
        json_body={
            "ccp_hash_update": True,
            "dir_name": "",
            "file_name": remote_name,
            "format_type": mime,
            "l_created_at": now_ms,
            "l_updated_at": now_ms,
            "pdir_fid": parent_id or "0",
            "size": path.stat().st_size,
        },
    )
    pre = pre_payload.get("data") if isinstance(pre_payload.get("data"), dict) else {}
    if not isinstance(pre, dict):
        raise CloudError(f"夸克预上传失败：{pre_payload}")
    if pre.get("finish") and pre.get("fid"):
        return str(pre["fid"])
    md5_hex, sha1_hex = _file_md5_sha1(path)
    hashed = await _quark_json(
        client,
        "POST",
        "/file/update/hash",
        json_body={
            "md5": md5_hex,
            "sha1": sha1_hex,
            "task_id": pre.get("task_id"),
        },
    )
    hashed_data = hashed.get("data") if isinstance(hashed.get("data"), dict) else {}
    if isinstance(hashed_data, dict) and hashed_data.get("finish"):
        return str(hashed_data.get("fid") or pre.get("fid") or "")
    meta = pre_payload.get("metadata") if isinstance(pre_payload.get("metadata"), dict) else {}
    part_size = int((meta or {}).get("part_size") or pre.get("part_size") or 10 * 1024 * 1024)
    oss_url = _quark_oss_url(pre)
    etags: list[str] = []
    with path.open("rb") as handle:
        part_number = 1
        while True:
            chunk = handle.read(part_size)
            if not chunk:
                break
            time_str = formatdate(usegmt=True)
            auth_meta = (
                f"PUT\n\n{mime}\n{time_str}\n"
                f"x-oss-date:{time_str}\n"
                f"x-oss-user-agent:{QUARK_OSS_UA}\n"
                f"/{pre.get('bucket')}/{pre.get('obj_key')}"
                f"?partNumber={part_number}&uploadId={pre.get('upload_id')}"
            )
            auth_key = await _quark_oss_auth(client, pre, auth_meta)
            uploaded = await client.put(
                oss_url,
                params={
                    "partNumber": str(part_number),
                    "uploadId": str(pre.get("upload_id") or ""),
                },
                content=chunk,
                headers={
                    "Authorization": auth_key,
                    "Content-Type": mime,
                    "Referer": "https://pan.quark.cn/",
                    "x-oss-date": time_str,
                    "x-oss-user-agent": QUARK_OSS_UA,
                },
            )
            if uploaded.status_code >= 400:
                raise CloudError(
                    f"夸克分片上传失败：{safe_response_snippet(uploaded, limit=300)}"
                )
            etag = uploaded.headers.get("ETag") or uploaded.headers.get("etag") or ""
            if not etag:
                raise CloudError("夸克分片上传未返回 ETag")
            etags.append(etag)
            part_number += 1
    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<CompleteMultipartUpload>"]
    for index, etag in enumerate(etags, start=1):
        xml_parts.append(
            f"<Part><PartNumber>{index}</PartNumber><ETag>{etag}</ETag></Part>"
        )
    xml_parts.append("</CompleteMultipartUpload>")
    xml_body = "\n".join(xml_parts)
    content_md5 = base64.b64encode(hashlib.md5(xml_body.encode("utf-8")).digest()).decode()
    callback = json.dumps(pre.get("callback") or {}, ensure_ascii=False)
    callback_b64 = base64.b64encode(callback.encode("utf-8")).decode()
    time_str = formatdate(usegmt=True)
    commit_meta = (
        f"POST\n{content_md5}\napplication/xml\n{time_str}\n"
        f"x-oss-callback:{callback_b64}\n"
        f"x-oss-date:{time_str}\n"
        f"x-oss-user-agent:{QUARK_OSS_UA}\n"
        f"/{pre.get('bucket')}/{pre.get('obj_key')}?uploadId={pre.get('upload_id')}"
    )
    commit_key = await _quark_oss_auth(client, pre, commit_meta)
    committed = await client.post(
        oss_url,
        params={"uploadId": str(pre.get("upload_id") or "")},
        content=xml_body.encode("utf-8"),
        headers={
            "Authorization": commit_key,
            "Content-MD5": content_md5,
            "Content-Type": "application/xml",
            "Referer": "https://pan.quark.cn/",
            "x-oss-callback": callback_b64,
            "x-oss-date": time_str,
            "x-oss-user-agent": QUARK_OSS_UA,
        },
    )
    if committed.status_code >= 400:
        raise CloudError(
            f"夸克分片合并失败：{safe_response_snippet(committed, limit=300)}"
        )
    await _quark_json(
        client,
        "POST",
        "/file/upload/finish",
        json_body={"obj_key": pre.get("obj_key"), "task_id": pre.get("task_id")},
    )
    return str(pre.get("fid") or hashed_data.get("fid") or "")


async def _quark_share(
    client: httpx.AsyncClient, folder_id: str, title: str, pwd: str
) -> str:
    pwd = _share_extract_code(pwd)
    created = await _quark_json(
        client,
        "POST",
        "/share",
        json_body={
            "fid_list": [folder_id],
            "title": (title or "game")[:80],
            "url_type": 2,
            "expired_type": 1,
            "passcode": pwd,
        },
    )
    data = created.get("data") if isinstance(created.get("data"), dict) else {}
    task_id = str((data or {}).get("task_id") or "")
    share_id = str((data or {}).get("share_id") or "")
    if not share_id and task_id:
        for index in range(12):
            task = await _quark_json(
                client,
                "GET",
                "/task",
                params={"task_id": task_id, "retry_index": str(index)},
            )
            task_data = task.get("data") if isinstance(task.get("data"), dict) else {}
            share_id = str((task_data or {}).get("share_id") or "")
            if share_id or (task_data or {}).get("status") == 2:
                break
            await asyncio.sleep(0.35)
    if not share_id:
        raise CloudError(f"夸克分享任务未返回 share_id：{created}")
    passworded = await _quark_json(
        client,
        "POST",
        "/share/password",
        json_body={"share_id": share_id, "passcode": pwd},
    )
    info = passworded.get("data") if isinstance(passworded.get("data"), dict) else {}
    url = str((info or {}).get("share_url") or "")
    if not url:
        raise CloudError(f"夸克未返回分享链接：{passworded}")
    return _with_share_pwd(url, str((info or {}).get("passcode") or pwd))


async def upload_quark_folder(
    settings: GamesSettings,
    sources: Sequence[Path],
    folder_name: str,
    *,
    prepare: PrepareFn | None = None,
    progress: ProgressFn | None = None,
    share_pwd: str | None = None,
) -> str:
    cookie = _quark_cookie_header(settings.quark_cookie)
    if not cookie:
        raise CloudError("未配置夸克网盘 Cookie（__puus）")
    share_pwd = _share_extract_code(share_pwd)
    cookies = _cookie_dict(cookie, "__puus")
    total = len(sources)
    last_error: Exception | None = None
    for base in QUARK_API_BASES:
        async with httpx.AsyncClient(
            **httpx_async_client_kwargs(
                timeout=httpx.Timeout(None),
                follow_redirects=True,
                cookies=cookies,
                headers={
                    "User-Agent": QUARK_UA,
                    "Origin": "https://pan.quark.cn",
                    "Referer": "https://pan.quark.cn/",
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                },
            )
        ) as client:
            client._quark_base = base  # type: ignore[attr-defined]
            try:
                folder_id = await _quark_ensure_folder(
                    client,
                    str(settings.quark_folder_id or "").strip() or "0",
                    settings.quark_remote_dir or "/games",
                    folder_name,
                )
                for index, source in enumerate(sources, start=1):
                    item = await _prepare(source, index, total, prepare)
                    try:
                        await _maybe_progress(
                            progress,
                            "quark",
                            index - 1,
                            total,
                            f"夸克上传 {index}/{total}",
                        )
                        await _quark_upload_file(
                            client, item.path, folder_id, item.remote_name
                        )
                        await _maybe_progress(
                            progress,
                            "quark",
                            index,
                            total,
                            f"夸克已上传 {index}/{total}",
                        )
                    finally:
                        _cleanup_prepared(item)
                return await _quark_share(client, folder_id, folder_name, share_pwd)
            except Exception as exc:
                last_error = exc
                logger.warning("夸克网盘 %s 失败：%s", base, exc)
                continue
            finally:
                persist_client_cookies(
                    "quark_cookie", settings.quark_cookie, "__puus", client
                )
    raise CloudError(str(last_error) if last_error else "夸克网盘上传失败")


async def upload_quark(settings: GamesSettings, path: Path, remote_name: str) -> str:
    folder = _safe_original_folder(Path(remote_name).stem)

    def rename(source: Path, _index: int, _total: int) -> PreparedUpload:
        return PreparedUpload(source, remote_name)

    return await upload_quark_folder(settings, [path], folder, prepare=rename)


def cloud_status(settings: GamesSettings) -> dict[str, Any]:
    return {
        "baidu": {
            "configured": bool(settings.baidu_enabled)
            and (bool(settings.baidu_cookie) or openlist_configured(settings, "baidu")),
            "via": "openlist" if openlist_configured(settings, "baidu") else "cookie",
        },
        "pikpak": {
            "configured": bool(
                settings.pikpak_refresh_token
                or (settings.pikpak_username and settings.pikpak_password)
            )
            or openlist_configured(settings, "pikpak"),
            "via": "openlist" if openlist_configured(settings, "pikpak") else "api",
        },
        "terabox": {
            "configured": bool(settings.terabox_cookie)
            or openlist_configured(settings, "terabox"),
            "via": "openlist" if openlist_configured(settings, "terabox") else "cookie",
        },
        "quark": {
            "configured": bool(settings.quark_cookie)
            or openlist_configured(settings, "quark"),
            "via": "openlist" if openlist_configured(settings, "quark") else "cookie",
        },
        "openlist": {
            "configured": bool(settings.openlist_url and settings.openlist_token),
        },
    }


async def upload_folder(
    settings: GamesSettings,
    target: str,
    sources: Sequence[Path],
    folder_name: str,
    *,
    prepare: PrepareFn | None = None,
    progress: ProgressFn | None = None,
    share_pwd: str | None = None,
) -> str:
    if not sources:
        raise CloudError("没有待上传文件")
    pwd = _share_extract_code(share_pwd)
    if openlist_configured(settings, target):
        return await upload_openlist_folder(
            settings,
            target,
            sources,
            folder_name,
            prepare=prepare,
            progress=progress,
        )
    if target == "baidu":
        return await upload_baidu_folder(
            settings,
            sources,
            folder_name,
            prepare=prepare,
            progress=progress,
            share_pwd=pwd,
        )
    if target == "pikpak":
        return await upload_pikpak_folder(
            settings,
            sources,
            folder_name,
            prepare=prepare,
            progress=progress,
            share_pwd=pwd,
        )
    if target == "terabox":
        return await upload_terabox_folder(
            settings,
            sources,
            folder_name,
            prepare=prepare,
            progress=progress,
            share_pwd=pwd,
        )
    if target == "quark":
        return await upload_quark_folder(
            settings,
            sources,
            folder_name,
            prepare=prepare,
            progress=progress,
            share_pwd=pwd,
        )
    raise CloudError(f"未知网盘：{target}")


async def upload_one(
    settings: GamesSettings,
    target: str,
    path: Path,
    remote_name: str,
) -> str:
    folder = target_folder_name(target, Path(remote_name).stem)

    def rename(source: Path, _index: int, _total: int) -> PreparedUpload:
        return PreparedUpload(source, remote_name)

    return await upload_folder(settings, target, [path], folder, prepare=rename)


async def upload_targets(
    settings: GamesSettings,
    *,
    title: str = "",
    archive_parts: Sequence[Path] | None = None,
    archive_7z: Path | None = None,
    disguised_mp4: Path | None = None,
    prepare_baidu: PrepareFn | None = None,
    prepare_disguise: PrepareFn | None = None,
    manual: dict[str, str] | None = None,
    only: Sequence[str] | None = None,
    progress: ProgressFn | None = None,
    share_pwd: str | None = None,
    on_update: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    """Upload targets and files strictly one at a time, sharing one game folder."""
    sources = list(archive_parts or ([] if archive_7z is None else [archive_7z]))
    if not sources:
        raise CloudError("没有待上传的 7z 文件")
    game_title = str(title or "").strip() or sources[0].name.split(".7z", 1)[0]
    links: dict[str, str] = {}
    errors: dict[str, str] = {}
    folders: dict[str, str] = {}
    selected = normalize_upload_targets(only)
    manual = {
        key: str(value).strip()
        for key, value in (manual or {}).items()
        if str(value).strip()
        and (selected is None or key == "other" or key in selected)
    }
    statuses = cloud_status(settings)
    share_pwd = _share_extract_code(share_pwd) or random_share_code()
    targets = list(CLOUD_TARGETS if selected is None else selected)

    async def _emit_update() -> None:
        if on_update is None:
            return
        payload = {
            "links": dict(links),
            "errors": dict(errors),
            "folders": dict(folders),
            "share_pwd": share_pwd,
        }
        result = on_update(payload)
        if inspect.isawaitable(result):
            await result
    # PikPak 不能指定提取码，先上传它再用返回的码同步其它网盘。
    if "pikpak" in targets:
        targets.remove("pikpak")
        pikpak_ready = (statuses.get("pikpak") or {}).get("configured") or bool(
            manual.get("pikpak")
        )
        if pikpak_ready:
            targets.insert(0, "pikpak")
        else:
            targets.append("pikpak")
    for target in targets:
        folder = target_folder_name(target, game_title)
        folders[target] = _join_remote(target_base_dir(settings, target), folder)
        if manual.get(target):
            links[target] = manual[target]
            if target == "pikpak":
                extracted = share_pwd_from_links({target: links[target]})
                if extracted:
                    share_pwd = extracted
            await _maybe_progress(progress, target, 1, 1, f"{target} 使用手动链接")
            await _emit_update()
            continue
        if not (statuses.get(target) or {}).get("configured"):
            errors[target] = "未配置"
            await _maybe_progress(progress, target, 0, 1, f"{target} 未配置，已跳过")
            await _emit_update()
            continue
        target_sources = sources
        disguise = prepare_disguise or prepare_baidu
        target_prepare = disguise if target in {"baidu", "quark"} else None
        if target in {"baidu", "quark"} and disguised_mp4 and disguised_mp4.is_file():
            target_sources = [disguised_mp4]
            target_prepare = None
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                links[target] = await upload_folder(
                    settings,
                    target,
                    target_sources,
                    folder,
                    prepare=target_prepare,
                    progress=progress,
                    share_pwd=share_pwd,
                )
                last_error = None
                if target == "pikpak":
                    extracted = share_pwd_from_links({target: links[target]})
                    if extracted:
                        share_pwd = extracted
                await _maybe_progress(
                    progress,
                    target,
                    len(target_sources),
                    len(target_sources),
                    f"{target} 已得到目录分享链接",
                )
                await _emit_update()
                break
            except Exception as exc:
                last_error = exc
                if attempt < 3 and _transient_cloud_error(exc):
                    logger.warning(
                        "上传 %s 第 %s 次失败，重试：%s %s",
                        target,
                        attempt,
                        type(exc).__name__,
                        exc or repr(exc),
                    )
                    await _maybe_progress(
                        progress, target, 0, 1, f"{target} 失败，正在重试 {attempt}/2"
                    )
                    await asyncio.sleep(min(3 * attempt, 12))
                    continue
                logger.exception("上传 %s 失败", target)
                errors[target] = str(exc)
                break
        if last_error is not None and target not in errors and target not in links:
            errors[target] = str(last_error)
        await _emit_update()
    if manual.get("other"):
        links["other"] = manual["other"]
    return {
        "links": links,
        "errors": errors,
        "folders": folders,
        "share_pwd": share_pwd,
    }
