from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.services.manga.config import MangaSettings


class PathEscapeError(ValueError):
    pass


@dataclass(frozen=True)
class HmwDirs:
    root: Path
    inbox: Path
    extracted: Path
    tasks: Path
    temp: Path

    def ensure(self) -> "HmwDirs":
        for path in (self.root, self.inbox, self.extracted, self.tasks, self.temp):
            path.mkdir(parents=True, exist_ok=True)
        return self


def hmw_dirs(settings: MangaSettings) -> HmwDirs:
    root = Path(settings.data_dir).expanduser().resolve() / "hmw"
    return HmwDirs(
        root=root,
        inbox=root / "inbox",
        extracted=root / "extracted",
        tasks=root / "tasks",
        temp=root / "temp",
    ).ensure()


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_under(root: Path, user_path: str | Path) -> Path:
    root = root.expanduser().resolve()
    raw = Path(str(user_path).strip() or ".")
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.expanduser().resolve()
    if resolved != root and not is_relative_to(resolved, root):
        raise PathEscapeError("路径超出允许范围")
    return resolved


def resolve_source_path(settings: MangaSettings, user_path: str) -> Path:
    dirs = hmw_dirs(settings)
    raw = str(user_path or "").strip()
    if not raw:
        raise PathEscapeError("路径不能为空")
    return resolve_under(dirs.root, raw)


ARCHIVE_SUFFIXES = {".zip", ".7z"}


def is_archive(path: Path) -> bool:
    return path.is_file() and path.suffix.casefold() in ARCHIVE_SUFFIXES
