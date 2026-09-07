from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from .paths import ARCHIVE_SUFFIXES


class ArchiveError(ValueError):
    pass


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    raw_flag = bool(info.flag_bits & 0x800)
    name = info.filename
    if raw_flag:
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except (UnicodeDecodeError, UnicodeEncodeError):
        try:
            return name.encode("cp437").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return name


def _safe_join(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ArchiveError(f"压缩包内路径不安全：{name}")
    return target


def _extract_zip(source: Path, dest: Path, password: str | None) -> None:
    pwd = password.encode("utf-8") if password else None
    try:
        with zipfile.ZipFile(source) as archive:
            if pwd:
                archive.setpassword(pwd)
            for info in archive.infolist():
                name = _decode_zip_name(info)
                if name.endswith("/"):
                    _safe_join(dest, name).mkdir(parents=True, exist_ok=True)
                    continue
                target = _safe_join(dest, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
            return
    except RuntimeError as exc:
        message = str(exc)
        if "password" in message.casefold() or "Bad password" in message:
            raise ArchiveError("压缩包密码错误") from exc
        raise ArchiveError(f"解压 zip 失败：{exc}") from exc
    except zipfile.BadZipFile as exc:
        raise ArchiveError("不是有效的 zip 文件") from exc

    try:
        import pyzipper
    except ImportError as exc:
        raise ArchiveError("AES zip 需要 pyzipper") from exc
    try:
        with pyzipper.AESZipFile(source) as archive:
            if pwd:
                archive.pwd = pwd
            archive.extractall(dest)
    except RuntimeError as exc:
        raise ArchiveError("压缩包密码错误或解压失败") from exc


def _extract_7z(source: Path, dest: Path, password: str | None) -> None:
    try:
        import py7zr
    except ImportError as exc:
        raise ArchiveError("解压 7z 需要 py7zr") from exc
    try:
        with py7zr.SevenZipFile(source, mode="r", password=password or None) as archive:
            archive.extractall(path=dest)
    except Exception as exc:
        message = str(exc).casefold()
        if "password" in message:
            raise ArchiveError("压缩包密码错误") from exc
        raise ArchiveError(f"解压 7z 失败：{exc}") from exc


def extract_archive(source: Path, dest: Path, password: str | None = None) -> Path:
    if not source.is_file():
        raise ArchiveError("压缩包不存在")
    suffix = source.suffix.casefold()
    if suffix not in ARCHIVE_SUFFIXES:
        raise ArchiveError("仅支持 zip / 7z")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if suffix == ".zip":
        try:
            _extract_zip(source, dest, password)
        except ArchiveError:
            if password:
                raise
            raise
        except Exception:
            # ZipCrypto 失败后再试 AES
            try:
                import pyzipper
            except ImportError as exc:
                raise ArchiveError("解压 zip 失败") from exc
            try:
                with pyzipper.AESZipFile(source) as archive:
                    if password:
                        archive.pwd = password.encode("utf-8")
                    archive.extractall(dest)
            except Exception as exc:
                raise ArchiveError(f"解压 zip 失败：{exc}") from exc
    else:
        _extract_7z(source, dest, password)
    return dest
