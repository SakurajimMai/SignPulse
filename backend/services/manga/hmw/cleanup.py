"""发布成功后清理 inbox / extracted 里的本地章节文件。

只删漫画图片目录和对应压缩包，不动 catalog.json、tasks 和 HMW 根目录。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from backend.services.manga.config import MangaSettings

from .paths import ARCHIVE_SUFFIXES, hmw_dirs, is_relative_to
from .service import relative_to_hmw

logger = logging.getLogger("backend.manga.hmw.cleanup")


def _allowed_bases(settings: MangaSettings) -> tuple[Path, Path]:
    dirs = hmw_dirs(settings)
    return dirs.inbox, dirs.extracted


def _is_protected(settings: MangaSettings, path: Path) -> bool:
    dirs = hmw_dirs(settings)
    protected = {dirs.root, dirs.inbox, dirs.extracted, dirs.tasks, dirs.temp}
    return path.resolve() in {item.resolve() for item in protected}


def _under_allowed(settings: MangaSettings, path: Path) -> bool:
    resolved = path.resolve()
    if _is_protected(settings, resolved):
        return False
    return any(resolved == base.resolve() or is_relative_to(resolved, base) for base in _allowed_bases(settings))


def _prune_empty_parents(settings: MangaSettings, start: Path) -> list[str]:
    dirs = hmw_dirs(settings)
    deleted: list[str] = []
    current = start
    stops = {dirs.inbox.resolve(), dirs.extracted.resolve(), dirs.root.resolve()}
    while current.resolve() not in stops and _under_allowed(settings, current):
        if not current.exists():
            current = current.parent
            continue
        try:
            next(current.iterdir())
            break
        except StopIteration:
            rel = relative_to_hmw(settings, current)
            current.rmdir()
            deleted.append(rel)
            current = current.parent
        except OSError:
            logger.warning("无法删除空目录 %s", current, exc_info=True)
            break
    return deleted


def cleanup_published_source(settings: MangaSettings, source_root: str | Path) -> dict[str, Any]:
    """整次发布成功后删除本地源目录，以及 extracted 对应的 inbox 压缩包。"""
    dirs = hmw_dirs(settings)
    root = Path(source_root).expanduser().resolve()
    deleted: list[str] = []
    if not _under_allowed(settings, root):
        logger.warning("拒绝清理超出 inbox/extracted 的路径：%s", root)
        return {"deleted": [], "skipped": "path not under inbox/extracted"}
    if root.exists():
        rel = relative_to_hmw(settings, root)
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            deleted.append(rel)
            logger.info("已删除本地发布源 %s", rel)
        else:
            logger.warning("删除本地发布源失败：%s", root)

    if is_relative_to(root, dirs.extracted) or root.parent.resolve() == dirs.extracted.resolve():
        for suffix in sorted(ARCHIVE_SUFFIXES):
            archive = dirs.inbox / f"{root.name}{suffix}"
            if archive.is_file() and _under_allowed(settings, archive):
                archive.unlink(missing_ok=True)
                if not archive.exists():
                    deleted.append(relative_to_hmw(settings, archive))

    deleted.extend(_prune_empty_parents(settings, root.parent))
    return {"deleted": deleted}
