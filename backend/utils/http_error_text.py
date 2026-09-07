"""把上游 HTTP 失败收成可展示短句，避免 Cloudflare HTML 进面板。"""

from __future__ import annotations

import re
from typing import Any

CF_STATUS_PHRASE = {
    520: "Web server is returning an unknown error",
    521: "Web server is down",
    522: "Connection timed out",
    523: "Origin is unreachable",
    524: "A timeout occurred",
    525: "SSL handshake failed",
    526: "Invalid SSL certificate",
    527: "Railgun error",
}

_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)", re.I)
_CF_CODE_RE = re.compile(r"\b(52[0-7])\b")
_HTML_MARKER_RE = re.compile(
    r"<!doctype\s+html\b|<html\b|<!--\s*\[if\b",
    re.I,
)
_HTML_SCAN_LIMIT = 4096


def looks_like_html(body: str) -> bool:
    """识别完整或被错误前缀包裹的 HTML 响应。"""
    return bool(_HTML_MARKER_RE.search(str(body or "")[:_HTML_SCAN_LIMIT]))


def _title_phrase(body: str) -> str:
    match = _TITLE_RE.search(body or "")
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    if "|" in title:
        title = title.rsplit("|", 1)[-1].strip()
    return title[:80]


def summarize_http_error(
    status: int,
    body: str,
    *,
    action: str = "Request failed",
) -> str:
    """UI / last_error 用的短句；绝不回传 HTML 或主机名。"""
    text = str(body or "")
    html = looks_like_html(text)
    if html or status in CF_STATUS_PHRASE:
        phrase = CF_STATUS_PHRASE.get(status) or ""
        if not phrase:
            title = _title_phrase(text)
            code_match = _CF_CODE_RE.search(title) or _CF_CODE_RE.search(text[:800])
            if code_match:
                code = int(code_match.group(1))
                phrase = CF_STATUS_PHRASE.get(code) or title or "origin HTML error"
                if status < 400:
                    status = code
            elif title:
                phrase = title
            else:
                phrase = "origin HTML error"
        return f"{action} HTTP {status}: {phrase}"
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) > 180:
        compact = compact[:180]
    if not compact:
        return f"{action} HTTP {status}"
    return f"{action} HTTP {status}: {compact}"


def safe_response_snippet(response: Any, limit: int = 180) -> str:
    """从 httpx.Response 取短错误；HTML 页走 summarize_http_error。"""
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    text = str(getattr(response, "text", "") or "")
    if looks_like_html(text) or status in CF_STATUS_PHRASE:
        return summarize_http_error(status, text, action="Upstream")
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) > limit:
        compact = compact[:limit]
    return compact or f"HTTP {status}"
