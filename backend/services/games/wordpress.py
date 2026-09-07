from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from backend.utils.http_error_text import safe_response_snippet
from backend.utils.outbound import httpx_async_client_kwargs

from .config import (
    DEFAULT_APATE_GUIDE_URL,
    DEFAULT_PAY_EXTRA_TEMPLATE,
    GamesSettings,
    optional_float,
    parse_int_list,
    split_csv,
)

logger = logging.getLogger("backend.games.wordpress")


class WordPressError(RuntimeError):
    pass


DEFAULT_CATEGORY_CHOICES = [
    {"id": 640, "name": "PC游戏", "slug": "pc游戏"},
    {"id": 637, "name": "汉化游戏", "slug": "chinesegame"},
    {"id": 641, "name": "安卓游戏", "slug": "安卓游戏"},
    {"id": 639, "name": "原生游戏", "slug": "native"},
    {"id": 22, "name": "游戏分享", "slug": "gameshare"},
]


def _client(settings: GamesSettings, timeout: float = 60.0) -> httpx.AsyncClient:
    url = str(settings.wp_url or "").rstrip("/")
    user = str(settings.wp_user or "").strip()
    password = str(settings.wp_app_password or "").strip()
    if not url or not user or not password:
        raise WordPressError("请填写 WordPress 站点、用户名和应用密码")
    return httpx.AsyncClient(
        **httpx_async_client_kwargs(
            base_url=url,
            auth=(user, password),
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={"User-Agent": "TG-SignPulse-Games/1.0"},
        )
    )


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            message = data.get("message") or data.get("detail")
            if message:
                return str(message)
    except Exception:
        pass
    return safe_response_snippet(response, limit=300)


def source_post_slug(source_key: str) -> str:
    value = str(source_key or "").strip()
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"tg-game-{digest}"


async def _find_post_by_slug(
    client: httpx.AsyncClient, slug: str
) -> dict[str, Any] | None:
    if not slug:
        return None
    queries = (
        {
            "slug": slug,
            "status": "publish,future,draft,pending,private",
            "context": "edit",
            "per_page": 1,
        },
        {"slug": slug, "per_page": 1},
    )
    for params in queries:
        response = await client.get("/wp-json/wp/v2/posts", params=params)
        if response.status_code >= 400:
            continue
        payload = response.json()
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
    return None


async def probe_wordpress(settings: GamesSettings) -> dict[str, Any]:
    if not settings.wp_url or not settings.wp_user or not settings.wp_app_password:
        return {"ok": False, "configured": False, "error": "未配置 WordPress 应用密码"}
    try:
        async with _client(settings, timeout=20.0) as client:
            response = await client.get("/wp-json/wp/v2/users/me")
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "configured": True,
                    "error": _detail(response),
                }
            data = response.json()
            name = ""
            if isinstance(data, dict):
                name = str(data.get("name") or data.get("slug") or "")
            return {"ok": True, "configured": True, "user": name}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": str(exc)}


def escape(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def extract_image_urls(html: str) -> list[str]:
    seen: list[str] = []
    for url in _IMG_SRC_RE.findall(str(html or "")):
        value = str(url or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


async def get_post(settings: GamesSettings, post_id: int) -> dict[str, Any]:
    async with _client(settings, timeout=30.0) as client:
        response = await client.get(
            f"/wp-json/wp/v2/posts/{int(post_id)}",
            params={"context": "edit"},
        )
        if response.status_code >= 400:
            raise WordPressError(f"读取文章失败：{_detail(response)}")
        data = response.json()
    if not isinstance(data, dict) or not data.get("id"):
        raise WordPressError("WordPress 未返回文章")
    return data


def paragraph_html(text: str) -> str:
    blocks = [item.strip() for item in str(text or "").split("\n\n") if item.strip()]
    if not blocks:
        return ""
    chunks: list[str] = []
    for index, block in enumerate(blocks):
        body = escape(block).replace("\n", "<br>\n")
        if index == 0 and not block.startswith("简介"):
            body = f"简介：<br>\n{body}"
        chunks.append(f'<p class="wp-block-paragraph">{body}</p>')
    return "\n\n".join(chunks)


def image_html(url: str, alt: str, index: int) -> str:
    safe_url = escape(url)
    safe_alt = escape(alt or f"图片[{index}]")
    if index == 1:
        return (
            f'<figure class="wp-block-image size-large">'
            f'<img alt="{safe_alt}" decoding="async" src="{safe_url}" />'
            f"</figure>"
        )
    return f'<img alt="{safe_alt}" decoding="async" src="{safe_url}"/>'


PAY_TYPE_DOWNLOAD = "2"
PAY_DETAILS_DEFAULT = ""
APATE_GUIDE_URL = DEFAULT_APATE_GUIDE_URL
DOWNLOAD_LABELS = (
    ("baidu", "百度网盘"),
    ("pikpak", "PikPak"),
    ("terabox", "TeraBox"),
    ("quark", "夸克网盘"),
    ("other", "其它"),
)
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_IMG_SRC_RE = re.compile(r"<img[^>]+src=['\"]([^'\"]+)['\"]", re.I)


def format_pay_amount(value: Any) -> str:
    amount = optional_float(value)
    if amount is None:
        return ""
    return f"{amount:g}"


def _plain_blocks_to_html(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    if re.search(r"<p[\s>]", raw, flags=re.I):
        return raw
    parts: list[str] = []
    for block in re.split(r"\n\s*\n", raw):
        body = block.strip()
        if not body:
            continue
        parts.append(f"<p>{body.replace(chr(10), '<br>' + chr(10))}</p>")
    return "\n".join(parts)


def render_pay_extra_hide(
    template: str | None,
    *,
    pack_password: str,
    extract: str,
    apate_url: str | None = None,
    apate_used: bool = False,
) -> str:
    """把后台模板填进付费隐藏区。{extract} 始终用本次自动生成的提取码。"""
    text = str(template or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        text = DEFAULT_PAY_EXTRA_TEMPLATE
    if not apate_used:
        text = "\n".join(
            line for line in text.splitlines() if "{apate" not in line.lower()
        )
    if extract and not re.search(r"\{(?:extract|share_pwd)\}", text, flags=re.I):
        text = text.rstrip() + "\n\n网盘提取码：{extract}"
    guide = str(apate_url or DEFAULT_APATE_GUIDE_URL).strip() or DEFAULT_APATE_GUIDE_URL
    apate_link = f'<a href="{escape(guide)}">Apate</a>'
    values = {
        "pack_password": escape(pack_password),
        "password": escape(pack_password),
        "extract": escape(extract),
        "share_pwd": escape(extract),
        "apate_url": escape(guide),
        "apate": apate_link,
        "apate_note": (
            f"百度/夸克网盘文件为 Apate 伪装的 MP4，请先用 {apate_link} 还原再解压。"
            if apate_used
            else ""
        ),
    }

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip().lower()
        return str(values.get(key, match.group(0)))

    rendered = _PLACEHOLDER_RE.sub(repl, text)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return _plain_blocks_to_html(rendered)


def share_pwd_from_links(links: dict[str, str] | None, fallback: str = "") -> str:
    for url in (links or {}).values():
        match = re.search(r"[?&]pwd=([A-Za-z0-9]{4})\b", str(url or ""))
        if match:
            return match.group(1)
    return fallback


def normalize_pay_modo(raw: str | None) -> str:
    value = str(raw or "0").strip().lower()
    if value in {"points", "point", "积分", "1"}:
        return "points"
    return "0"


def build_pay_download(
    links: dict[str, str],
    *,
    pack_password: str,
    apate_used: bool,
    share_pwd: str = "",
) -> list[dict[str, str]]:
    extract = share_pwd or share_pwd_from_links(links)
    items: list[dict[str, str]] = []
    for key, label in DOWNLOAD_LABELS:
        url = str(links.get(key) or "").strip()
        if not url:
            continue
        more = pack_password
        if extract:
            more = f"解压 {pack_password} / 提取码 {extract}"
        if key in {"baidu", "quark"} and apate_used:
            more = f"Apate 还原后解压，密码 {pack_password}" + (
                f" / 提取码 {extract}" if extract else ""
            )
        items.append({"link": url, "name": label, "more": more})
    return items


def build_pay_extra_hide(
    *,
    pack_password: str,
    apate_used: bool,
    links: dict[str, str],
    share_pwd: str = "",
    extra_template: str | None = None,
    apate_url: str | None = None,
) -> str:
    extract = share_pwd or share_pwd_from_links(links)
    used = bool(apate_used and (links.get("baidu") or links.get("quark")))
    return render_pay_extra_hide(
        extra_template,
        pack_password=pack_password,
        extract=extract,
        apate_url=apate_url,
        apate_used=used,
    )


def build_zibpay_meta(
    *,
    pay_modo: str,
    pay_price: float,
    points_price: float,
    links: dict[str, str],
    pack_password: str,
    apate_used: bool,
    share_pwd: str = "",
    extra_template: str | None = None,
    apate_url: str | None = None,
    vip1_price: float | None = None,
    vip2_price: float | None = None,
    vip1_points: float | None = None,
    vip2_points: float | None = None,
) -> dict[str, Any]:
    modo = normalize_pay_modo(pay_modo)
    cash = format_pay_amount(pay_price)
    points = format_pay_amount(points_price)
    extract = share_pwd or share_pwd_from_links(links)
    return {
        "pay_type": PAY_TYPE_DOWNLOAD,
        "pay_limit": "0",
        "pay_modo": modo,
        "pay_price": cash if modo == "0" else "",
        "pay_original_price": "",
        "vip_1_price": format_pay_amount(vip1_price),
        "vip_2_price": format_pay_amount(vip2_price),
        "points_price": points if modo == "points" else "",
        "vip_1_points": format_pay_amount(vip1_points),
        "vip_2_points": format_pay_amount(vip2_points),
        "pay_cuont": "0",
        "pay_title": "",
        "pay_doc": "",
        "pay_details": PAY_DETAILS_DEFAULT,
        "pay_extra_hide": build_pay_extra_hide(
            pack_password=pack_password,
            apate_used=apate_used,
            links=links,
            share_pwd=extract,
            extra_template=extra_template,
            apate_url=apate_url,
        ),
        "pay_download": build_pay_download(
            links,
            pack_password=pack_password,
            apate_used=apate_used,
            share_pwd=extract,
        ),
    }


def pay_html(
    *,
    pack_password: str,
    links: dict[str, str],
    apate_used: bool,
    share_pwd: str = "",
    extra_template: str | None = None,
    apate_url: str | None = None,
) -> str:
    extra = build_pay_extra_hide(
        pack_password=pack_password,
        apate_used=apate_used,
        links=links,
        share_pwd=share_pwd,
        extra_template=extra_template,
        apate_url=apate_url,
    )
    rows: list[str] = [extra] if extra else []
    mapping = [
        ("baidu", "百度网盘"),
        ("pikpak", "PikPak"),
        ("terabox", "TeraBox"),
        ("quark", "夸克网盘"),
    ]
    items = []
    for key, label in mapping:
        url = str(links.get(key) or "").strip()
        if url:
            items.append(
                f'<li>{escape(label)}：<a href="{escape(url)}">{escape(url)}</a></li>'
            )
    extra = str(links.get("other") or "").strip()
    if extra:
        items.append(f'<li>其它：<a href="{escape(extra)}">{escape(extra)}</a></li>')
    if items:
        rows.append("<ul>\n" + "\n".join(items) + "\n</ul>")
    return "<!--paystart-->\n" + "\n".join(rows) + "\n<!--payend-->"


def build_post_html(
    *,
    summary: str,
    image_urls: list[str],
    title: str,
    pack_password: str,
    links: dict[str, str],
    apate_used: bool,
    pay_enabled: bool,
    pay_in_content: bool = True,
    share_pwd: str = "",
    extra_template: str | None = None,
    apate_url: str | None = None,
) -> str:
    parts: list[str] = []
    for index, url in enumerate(image_urls, start=1):
        parts.append(image_html(url, f"{title} {index}", index))
        if index == 1:
            html_summary = paragraph_html(summary)
            if html_summary:
                parts.append(html_summary)
    if not image_urls:
        html_summary = paragraph_html(summary)
        if html_summary:
            parts.append(html_summary)
    if pay_in_content:
        html = pay_html(
            pack_password=pack_password,
            links=links,
            apate_used=apate_used,
            share_pwd=share_pwd,
            extra_template=extra_template,
            apate_url=apate_url,
        )
        if not pay_enabled:
            html = html.replace("<!--paystart-->\n", "").replace("\n<!--payend-->", "")
        parts.append(html)
    return "\n\n".join(part for part in parts if part)


async def upload_media(
    settings: GamesSettings, path: Path, title: str
) -> dict[str, Any]:
    if not path.is_file():
        raise WordPressError(f"图片不存在：{path}")
    filename = path.name
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".avif": "image/avif",
    }.get(path.suffix.casefold(), "application/octet-stream")
    data = path.read_bytes()
    last_error = ""
    async with _client(settings, timeout=180.0) as client:
        for attempt in range(1, 4):
            response = await client.post(
                "/wp-json/wp/v2/media",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Type": mime,
                },
                content=data,
            )
            if response.status_code < 400:
                payload = response.json()
                break
            last_error = _detail(response)
            if response.status_code not in {502, 503, 504} or attempt == 3:
                raise WordPressError(f"上传图片失败：{last_error}")
            logger.warning(
                "WordPress 传图 %s 第 %s 次失败 %s，重试",
                filename,
                attempt,
                response.status_code,
            )
            await asyncio.sleep(min(2 * attempt, 8))
        else:
            raise WordPressError(f"上传图片失败：{last_error}")
    if not isinstance(payload, dict) or not payload.get("source_url"):
        raise WordPressError("WordPress 未返回图片地址")
    return payload


async def _get_or_create_tag(client: httpx.AsyncClient, name: str) -> int | None:
    clean = name.strip()
    if not clean:
        return None
    listed = await client.get(
        "/wp-json/wp/v2/tags", params={"search": clean, "per_page": 20}
    )
    if listed.status_code < 400:
        items = listed.json()
        if isinstance(items, list):
            for item in items:
                if (
                    isinstance(item, dict)
                    and str(item.get("name") or "").casefold() == clean.casefold()
                ):
                    try:
                        return int(item["id"])
                    except (TypeError, ValueError, KeyError):
                        continue
    created = await client.post("/wp-json/wp/v2/tags", json={"name": clean})
    if created.status_code >= 400:
        logger.warning("创建标签失败 %s: %s", clean, _detail(created))
        return None
    data = created.json()
    try:
        return int(data["id"]) if isinstance(data, dict) else None
    except (TypeError, ValueError, KeyError):
        return None


async def resolve_tag_ids(settings: GamesSettings, names: list[str]) -> list[int]:
    ids: list[int] = []
    async with _client(settings, timeout=30.0) as client:
        for name in names:
            tag_id = await _get_or_create_tag(client, name)
            if tag_id and tag_id not in ids:
                ids.append(tag_id)
    return ids


async def list_categories(settings: GamesSettings) -> list[dict[str, Any]]:
    try:
        async with _client(settings, timeout=20.0) as client:
            response = await client.get(
                "/wp-json/wp/v2/categories",
                params={"per_page": 50, "hide_empty": False},
            )
            if response.status_code >= 400:
                return list(DEFAULT_CATEGORY_CHOICES)
            items = response.json()
    except Exception:
        return list(DEFAULT_CATEGORY_CHOICES)
    if not isinstance(items, list):
        return list(DEFAULT_CATEGORY_CHOICES)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            result.append(
                {
                    "id": int(item["id"]),
                    "name": str(item.get("name") or ""),
                    "slug": str(item.get("slug") or ""),
                }
            )
        except (TypeError, ValueError, KeyError):
            continue
    return result or list(DEFAULT_CATEGORY_CHOICES)


async def create_post(
    settings: GamesSettings,
    *,
    title: str,
    content: str,
    categories: list[int],
    tags: list[int],
    featured_media: int | None,
    pay_enabled: bool,
    price: float,
    points_price: float = 0.0,
    pay_modo: str = "0",
    links: dict[str, str] | None = None,
    pack_password: str = "",
    apate_used: bool = False,
    share_pwd: str = "",
    extra_template: str | None = None,
    apate_url: str | None = None,
    vip1_price: float | None = None,
    vip2_price: float | None = None,
    vip1_points: float | None = None,
    vip2_points: float | None = None,
    status: str = "publish",
    source_key: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": title,
        "content": content,
        "status": status or settings.wp_status or "publish",
        "categories": categories or settings.category_ids,
        "tags": tags,
    }
    if featured_media:
        payload["featured_media"] = featured_media
    slug = source_post_slug(source_key)
    if slug:
        payload["slug"] = slug
    zibpay = None
    if pay_enabled:
        zibpay = build_zibpay_meta(
            pay_modo=pay_modo,
            pay_price=price,
            points_price=points_price,
            links=links or {},
            pack_password=pack_password,
            apate_used=apate_used,
            share_pwd=share_pwd,
            extra_template=(
                extra_template
                if extra_template is not None
                else settings.wp_pay_extra_template
            ),
            apate_url=apate_url or settings.wp_apate_url,
            vip1_price=(
                vip1_price if vip1_price is not None else settings.wp_vip1_price
            ),
            vip2_price=(
                vip2_price if vip2_price is not None else settings.wp_vip2_price
            ),
            vip1_points=(
                vip1_points if vip1_points is not None else settings.wp_vip1_points
            ),
            vip2_points=(
                vip2_points if vip2_points is not None else settings.wp_vip2_points
            ),
        )
        payload["meta"] = {"posts_zibpay": zibpay}
        payload["posts_zibpay"] = zibpay

    async with _client(settings, timeout=60.0) as client:
        existing = await _find_post_by_slug(client, slug)
        endpoint = "/wp-json/wp/v2/posts"
        if existing is not None and existing.get("id"):
            logger.info(
                "更新已有游戏文章 source=%s post_id=%s",
                source_key,
                existing.get("id"),
            )
            endpoint = f"/wp-json/wp/v2/posts/{existing['id']}"
        response = await client.post(endpoint, json=payload)
        if response.status_code >= 400 and pay_enabled and "meta" in payload:
            logger.warning(
                "带付费 meta 发文失败，改为顶层 posts_zibpay：%s", _detail(response)
            )
            payload.pop("meta", None)
            response = await client.post(endpoint, json=payload)
        if response.status_code >= 400 and pay_enabled and "posts_zibpay" in payload:
            logger.warning(
                "写入 Zibll 付费字段失败，改为 paystart 隐藏内容：%s", _detail(response)
            )
            payload.pop("posts_zibpay", None)
            fallback = pay_html(
                pack_password=pack_password,
                links=links or {},
                apate_used=apate_used,
                share_pwd=share_pwd,
                extra_template=(
                    extra_template
                    if extra_template is not None
                    else settings.wp_pay_extra_template
                ),
                apate_url=apate_url or settings.wp_apate_url,
            )
            payload["content"] = (content.rstrip() + "\n\n" + fallback).strip()
            response = await client.post(endpoint, json=payload)
        if response.status_code >= 400:
            raise WordPressError(f"发布文章失败：{_detail(response)}")
        data = response.json()
    if not isinstance(data, dict) or not data.get("id"):
        raise WordPressError("WordPress 未返回文章 ID")
    return data


def parse_category_input(raw: str | list[int] | None, fallback: list[int]) -> list[int]:
    if isinstance(raw, list):
        values = []
        for item in raw:
            try:
                values.append(int(item))
            except (TypeError, ValueError):
                continue
        return values or list(fallback)
    parsed = parse_int_list(str(raw or ""))
    return parsed or list(fallback)


def parse_tag_input(raw: str | list[str] | None, fallback: list[str]) -> list[str]:
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
        return values or list(fallback)
    parsed = split_csv(str(raw or ""))
    return parsed or list(fallback)
