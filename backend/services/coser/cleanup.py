from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from backend.services.games.paths import is_relative_to

from .config import CoserSettings
from .paths import coser_dirs, resolve_coser_path

logger = logging.getLogger("backend.coser.cleanup")


def _safe_rm(path: Path, root: Path) -> bool:
    if not path.exists():
        return False
    resolved = path.resolve()
    if resolved != root.resolve() and not is_relative_to(resolved, root):
        return False
    try:
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
        return True
    except OSError:
        logger.warning("清理失败 %s", resolved)
        return False


def cleanup_job_files(settings: CoserSettings, job: dict[str, Any]) -> dict[str, Any]:
    dirs = coser_dirs(settings)
    deleted: list[str] = []
    for key in ("work_dir", "extracted_dir", "packed_dir", "packed_7z"):
        raw = str(job.get(key) or "").strip()
        if not raw:
            continue
        try:
            path = resolve_coser_path(settings, raw)
        except Exception:
            continue
        if _safe_rm(path, dirs.root):
            deleted.append(raw)
    for key in ("packed_parts", "disguised_parts", "images", "archives"):
        for item in job.get(key) or []:
            raw = str(
                item.get("path") if isinstance(item, dict) else item or ""
            ).strip()
            if not raw:
                continue
            try:
                path = resolve_coser_path(settings, raw)
            except Exception:
                continue
            if _safe_rm(path, dirs.root):
                deleted.append(raw)
    job["cleaned"] = True
    return {"deleted": deleted, "count": len(deleted)}
