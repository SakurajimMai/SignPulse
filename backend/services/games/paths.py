from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import GamesSettings, load_games_settings


class PathEscapeError(ValueError):
    pass


@dataclass(frozen=True)
class GamesDirs:
    root: Path
    inbox: Path
    extracted: Path
    packed: Path
    tasks: Path
    temp: Path

    def ensure(self) -> "GamesDirs":
        for path in (
            self.root,
            self.inbox,
            self.extracted,
            self.packed,
            self.tasks,
            self.temp,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


def games_dirs(settings: GamesSettings | None = None) -> GamesDirs:
    current = settings or load_games_settings()
    root = Path(current.data_dir).expanduser().resolve() / "games"
    return GamesDirs(
        root=root,
        inbox=root / "inbox",
        extracted=root / "extracted",
        packed=root / "packed",
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


def resolve_games_path(settings: GamesSettings, user_path: str) -> Path:
    dirs = games_dirs(settings)
    raw = str(user_path or "").strip()
    if not raw:
        raise PathEscapeError("路径不能为空")
    return resolve_under(dirs.root, raw)


def relative_to_games(settings: GamesSettings, path: Path) -> str:
    dirs = games_dirs(settings)
    resolved = path.expanduser().resolve()
    try:
        return str(resolved.relative_to(dirs.root.resolve()))
    except ValueError:
        return str(resolved)


ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".iso", ".gz", ".tgz", ".tar"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
