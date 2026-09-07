from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.services.games.paths import GamesDirs, PathEscapeError, is_relative_to

from .config import CoserSettings, load_coser_settings


@dataclass(frozen=True)
class CoserDirs:
    root: Path
    inbox: Path
    extracted: Path
    packed: Path
    tasks: Path
    temp: Path
    gallery: Path

    def ensure(self) -> "CoserDirs":
        for path in (
            self.root,
            self.inbox,
            self.extracted,
            self.packed,
            self.tasks,
            self.temp,
            self.gallery,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def as_games_dirs(self) -> GamesDirs:
        return GamesDirs(
            root=self.root,
            inbox=self.inbox,
            extracted=self.extracted,
            packed=self.packed,
            tasks=self.tasks,
            temp=self.temp,
        )


def coser_dirs(settings: CoserSettings | None = None) -> CoserDirs:
    current = settings or load_coser_settings()
    root = Path(current.data_dir).expanduser().resolve() / "coser"
    return CoserDirs(
        root=root,
        inbox=root / "inbox",
        extracted=root / "extracted",
        packed=root / "packed",
        tasks=root / "tasks",
        temp=root / "temp",
        gallery=root / "gallery",
    ).ensure()


def resolve_under(root: Path, user_path: str | Path) -> Path:
    root = root.expanduser().resolve()
    raw = Path(str(user_path).strip() or ".")
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.expanduser().resolve()
    if resolved != root and not is_relative_to(resolved, root):
        raise PathEscapeError("路径超出允许范围")
    return resolved


def resolve_coser_path(settings: CoserSettings, user_path: str) -> Path:
    dirs = coser_dirs(settings)
    raw = str(user_path or "").strip()
    if not raw:
        raise PathEscapeError("路径不能为空")
    return resolve_under(dirs.root, raw)


def relative_to_coser(settings: CoserSettings, path: Path) -> str:
    dirs = coser_dirs(settings)
    resolved = path.expanduser().resolve()
    try:
        return str(resolved.relative_to(dirs.root.resolve()))
    except ValueError:
        return str(resolved)
