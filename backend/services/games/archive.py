from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from backend.services.manga.hmw.archive import ArchiveError
from backend.services.manga.hmw.archive import extract_archive as hmw_extract

from .paths import ARCHIVE_SUFFIXES

logger = logging.getLogger("backend.games.archive")

SKIP_FILES = {"desktop.ini", "thumbs.db", ".ds_store", "__macosx"}


def find_7z_bin() -> str | None:
    for name in ("7z", "7zz", "7za", "7zr"):
        path = shutil.which(name)
        if path:
            return path
    return None


def is_archive_name(name: str) -> bool:
    lowered = Path(name).name.casefold()
    return any(
        lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES
    ) or lowered.endswith(".7z.001")


def password_candidates(*groups: str | list[str] | None) -> list[str]:
    values: list[str] = []
    for group in groups:
        if group is None:
            continue
        items = group if isinstance(group, list) else [group]
        for item in items:
            text = str(item or "").strip()
            if text and text not in values:
                values.append(text)
    return values


def is_ad_entry(name: str, keywords: list[str]) -> bool:
    raw = str(name or "").strip()
    if not raw:
        return False
    lowered = raw.casefold()
    base = Path(raw).name.casefold()
    if base in SKIP_FILES or base.endswith(".url"):
        return True
    for keyword in keywords:
        token = str(keyword or "").strip()
        if len(token) < 2:
            continue
        if token.casefold() in lowered:
            return True
    return False


def strip_ads(root: Path, keywords: list[str]) -> list[str]:
    removed: list[str] = []
    if not root.exists():
        return removed
    # 先目录后文件，从深到浅
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        for filename in filenames:
            path = current / filename
            if is_ad_entry(filename, keywords) or is_ad_entry(
                str(path.relative_to(root)), keywords
            ):
                try:
                    path.unlink()
                    removed.append(str(path.relative_to(root)))
                except OSError:
                    logger.warning("无法删除广告文件 %s", path)
        for dirname in dirnames:
            path = current / dirname
            if is_ad_entry(dirname, keywords):
                try:
                    shutil.rmtree(path)
                    removed.append(str(path.relative_to(root)))
                except OSError:
                    logger.warning("无法删除广告目录 %s", path)
    # 清掉空目录
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        if current != root and not any(current.iterdir()):
            try:
                current.rmdir()
            except OSError:
                pass
    return removed


def _extract_with_7z(
    source: Path, dest: Path, password: str | None, binary: str
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [binary, "x", "-y", f"-o{dest}", str(source)]
    if password:
        cmd.insert(2, f"-p{password}")
    else:
        cmd.insert(2, "-p")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    blob = f"{result.stdout}\n{result.stderr}".casefold()
    if result.returncode != 0:
        if "wrong password" in blob or "password" in blob and "error" in blob:
            raise ArchiveError("压缩包密码错误")
        raise ArchiveError(
            result.stderr.strip() or result.stdout.strip() or "7z 解压失败"
        )


def extract_game_archive(
    source: Path,
    dest: Path,
    passwords: list[str] | None = None,
) -> Path:
    if not source.is_file():
        raise ArchiveError("压缩包不存在")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    tries: list[str | None] = [None, *password_candidates(passwords)]
    binary = find_7z_bin()
    last_error: Exception | None = None
    for password in tries:
        try:
            if binary:
                _extract_with_7z(source, dest, password, binary)
            else:
                suffix = source.suffix.casefold()
                if suffix not in {".zip", ".7z"}:
                    raise ArchiveError("未安装 7z，仅能解压 zip / 7z")
                hmw_extract(source, dest, password)
            if any(dest.iterdir()):
                return dest
        except ArchiveError as exc:
            last_error = exc
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
                dest.mkdir(parents=True, exist_ok=True)
            message = str(exc).casefold()
            if "密码" not in message and "password" not in message:
                break
        except Exception as exc:
            last_error = ArchiveError(str(exc))
            break
    raise last_error or ArchiveError("解压失败")


def nested_archives(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and is_archive_name(path.name):
            found.append(path)
    return found


def extract_tree(
    source: Path,
    dest: Path,
    passwords: list[str] | None = None,
    *,
    max_depth: int = 3,
) -> Path:
    """解压压缩包；若内部只剩另一个压缩包则继续解开。"""
    extract_game_archive(source, dest, passwords)
    depth = 0
    while depth < max_depth:
        children = [item for item in dest.iterdir() if item.name not in SKIP_FILES]
        archives = [
            item for item in children if item.is_file() and is_archive_name(item.name)
        ]
        dirs = [item for item in children if item.is_dir()]
        if len(archives) == 1 and not dirs:
            inner = archives[0]
            tmp = dest.parent / f"{dest.name}.nested"
            extract_game_archive(inner, tmp, passwords)
            shutil.rmtree(dest)
            tmp.rename(dest)
            depth += 1
            continue
        break
    return dest


def _remove_pack_outputs(dest: Path) -> None:
    dest.unlink(missing_ok=True)
    for part in dest.parent.glob(f"{dest.name}.*"):
        if part.is_file():
            part.unlink(missing_ok=True)


def _pack_with_7z(
    source: Path,
    dest: Path,
    password: str,
    binary: str,
    *,
    volume_bytes: int | None = None,
) -> list[Path]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _remove_pack_outputs(dest)
    cmd = [
        binary,
        "a",
        "-t7z",
        "-mhe=on",
        "-mx=5",
        f"-p{password}",
    ]
    if volume_bytes:
        # 4096 MiB = 7z 的 4g 分卷
        if int(volume_bytes) == 4 * 1024 * 1024 * 1024:
            cmd.append("-v4g")
        else:
            cmd.append(f"-v{int(volume_bytes)}b")
    cmd.extend([str(dest), "."])
    result = subprocess.run(
        cmd,
        cwd=str(source),
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = sorted(dest.parent.glob(f"{dest.name}.*")) if volume_bytes else [dest]
    outputs = [path for path in outputs if path.is_file()]
    if result.returncode != 0 or not outputs:
        _remove_pack_outputs(dest)
        raise ArchiveError(
            result.stderr.strip() or result.stdout.strip() or "7z 打包失败"
        )
    return outputs


def _pack_with_py7zr(source: Path, dest: Path, password: str) -> None:
    try:
        import py7zr
    except ImportError as exc:
        raise ArchiveError("打包 7z 需要系统 7z 或 py7zr") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    kwargs = {"mode": "w", "password": password}
    try:
        archive = py7zr.SevenZipFile(dest, header_encryption=True, **kwargs)
    except TypeError:
        archive = py7zr.SevenZipFile(dest, **kwargs)
    with archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=str(path.relative_to(source)))


def pack_7z(source: Path, dest: Path, password: str) -> Path:
    if not source.exists():
        raise ArchiveError("待打包目录不存在")
    pwd = str(password or "").strip()
    if not pwd:
        raise ArchiveError("7z 打包密码不能为空")
    binary = find_7z_bin()
    if binary:
        _pack_with_7z(source, dest, pwd, binary)
    else:
        _pack_with_py7zr(source, dest, pwd)
    return dest


def pack_7z_parts(
    source: Path,
    dest: Path,
    password: str,
    *,
    volume_bytes: int,
) -> list[Path]:
    """Pack once, selecting a volume archive when source data exceeds the limit."""
    if not source.exists():
        raise ArchiveError("待打包目录不存在")
    pwd = str(password or "").strip()
    if not pwd:
        raise ArchiveError("7z 打包密码不能为空")
    limit = max(int(volume_bytes), 1)
    source_bytes = sum(
        path.stat().st_size for path in source.rglob("*") if path.is_file()
    )
    if source_bytes <= limit:
        return [pack_7z(source, dest, pwd)]
    binary = find_7z_bin()
    if not binary:
        raise ArchiveError("大于分卷阈值的游戏需要系统 7z 执行分卷压缩")
    return _pack_with_7z(
        source,
        dest,
        pwd,
        binary,
        volume_bytes=limit,
    )


def list_images(root: Path) -> list[Path]:
    from .paths import IMAGE_SUFFIXES

    items: list[Path] = []
    if not root.exists():
        return items
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES:
            items.append(path)
    return items


def collect_archives(root: Path) -> list[Path]:
    items: list[Path] = []
    if not root.exists():
        return items
    for path in sorted(root.rglob("*")):
        if path.is_file() and is_archive_name(path.name):
            items.append(path)
    return items
