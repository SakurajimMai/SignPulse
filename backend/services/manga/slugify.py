from __future__ import annotations

import re
import unicodedata


def slugify(value: str, *, max_length: int = 80) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip().lower()
    # Keep CJK letters by converting spaces/punct to hyphens without stripping CJK
    value = re.sub(r"[\s_/\\|]+", "-", value)
    value = re.sub(r"[^\w\u4e00-\u9fff\-]+", "", value, flags=re.UNICODE)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    if not value:
        value = "manga"
    return value[:max_length]
