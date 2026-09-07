from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from .config import GamesSettings
from .paths import games_dirs, is_relative_to, resolve_games_path

logger = logging.getLogger("backend.games.cleanup")


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


def cleanup_job_files(settings: GamesSettings, job: dict[str, Any]) -> dict[str, Any]:
    dirs = games_dirs(settings)
    deleted: list[str] = []
    for key in ("work_dir", "extracted_dir", "packed_dir", "packed_7z", "packed_mp4"):
        raw = str(job.get(key) or "").strip()
        if not raw:
            continue
        try:
            path = resolve_games_path(settings, raw)
        except Exception:
            continue
        if _safe_rm(path, dirs.root):
            deleted.append(raw)
    for key in ("packed_parts", "disguised_parts"):
        for item in job.get(key) or []:
            raw = str(
                item.get("path") if isinstance(item, dict) else item or ""
            ).strip()
            if not raw:
                continue
            try:
                path = resolve_games_path(settings, raw)
            except Exception:
                continue
            if _safe_rm(path, dirs.root):
                deleted.append(raw)
    for item in job.get("images") or []:
        raw = str(item.get("path") if isinstance(item, dict) else item or "").strip()
        if not raw:
            continue
        try:
            path = resolve_games_path(settings, raw)
        except Exception:
            continue
        if _safe_rm(path, dirs.root):
            deleted.append(raw)
    for item in job.get("archives") or []:
        raw = str(item.get("path") if isinstance(item, dict) else item or "").strip()
        if not raw:
            continue
        try:
            path = resolve_games_path(settings, raw)
        except Exception:
            continue
        if _safe_rm(path, dirs.root):
            deleted.append(raw)
    work = dirs.inbox / str(job.get("id") or "")
    if work.exists() and _safe_rm(work, dirs.root):
        deleted.append(str(work.relative_to(dirs.root)))
    return {"deleted": deleted}


def cleanup_source_archives(
    settings: GamesSettings, job: dict[str, Any]
) -> dict[str, Any]:
    dirs = games_dirs(settings)
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        return {"deleted": []}
    archive_root = dirs.inbox / job_id / "archives"
    if _safe_rm(archive_root, dirs.root):
        return {"deleted": [str(archive_root.relative_to(dirs.root))]}
    return {"deleted": []}
