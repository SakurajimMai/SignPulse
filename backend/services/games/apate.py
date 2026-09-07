from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import GamesSettings

logger = logging.getLogger("backend.games.apate")


class ApateError(RuntimeError):
    pass


def resolve_apate_bin(settings: GamesSettings) -> str | None:
    configured = str(settings.apate_bin or "").strip() or "apate"
    if Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser())
    return shutil.which(configured)


def apate_available(settings: GamesSettings) -> bool:
    return bool(resolve_apate_bin(settings))


def _run(binary: str, args: list[str], *, timeout: int = 120) -> dict[str, Any]:
    result = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    stdout = (result.stdout or "").strip()
    parsed: dict[str, Any] = {}
    if stdout:
        try:
            loaded = json.loads(stdout)
            if isinstance(loaded, dict):
                parsed = loaded
        except json.JSONDecodeError:
            parsed = {"raw": stdout}
    if result.returncode != 0:
        message = (
            parsed.get("error")
            or parsed.get("message")
            or result.stderr.strip()
            or stdout
            or "apate 执行失败"
        )
        raise ApateError(str(message))
    return parsed


def probe_apate(settings: GamesSettings) -> dict[str, Any]:
    binary = resolve_apate_bin(settings)
    if not binary:
        return {"ok": False, "error": "未找到 apate 可执行文件"}
    try:
        _run(binary, ["masks", "--json"], timeout=20)
        return {"ok": True, "bin": binary}
    except Exception as exc:
        return {"ok": False, "bin": binary, "error": str(exc)}


def disguise_to_mp4(settings: GamesSettings, source: Path, dest: Path) -> Path:
    """复制 7z 后再伪装成 mp4，保留原 7z。"""
    if not source.is_file():
        raise ApateError("待伪装文件不存在")
    binary = resolve_apate_bin(settings)
    if not binary:
        raise ApateError("未找到 apate，请安装 https://github.com/SakurajimMai/apate")
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.with_suffix(".7z")
    if work.exists():
        work.unlink()
    shutil.copy2(source, work)
    try:
        payload = _run(
            binary,
            ["disguise", "--input", str(work), "--one-key", "--json"],
            timeout=300,
        )
    except Exception:
        work.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    output = dest
    if results:
        first = results[0] if isinstance(results[0], dict) else {}
        if first.get("ok") is False:
            raise ApateError(
                str(first.get("error") or first.get("code") or "apate 伪装失败")
            )
        candidate = first.get("output") or first.get("path") or first.get("renamed")
        if candidate:
            output = Path(str(candidate))
    if not output.exists():
        renamed = work.with_suffix(".mp4")
        if renamed.exists():
            output = renamed
    if output != dest:
        if dest.exists():
            dest.unlink()
        shutil.move(str(output), str(dest))
        output = dest
    if not dest.is_file():
        work.unlink(missing_ok=True)
        raise ApateError("apate 未生成 mp4")
    return dest
