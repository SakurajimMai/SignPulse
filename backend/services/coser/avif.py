"""把图集转成 AVIF，供 Coser CDN 使用。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PIL import Image, ImageColor, ImageOps

logger = logging.getLogger("backend.coser.avif")

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    pass

AVIF_QUALITY = 55
AVIF_BACKGROUND = "#FFFFFF"
MIN_VALID_BYTES = 32


class CoserAvifError(RuntimeError):
    pass


def avif_is_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        if path.stat().st_size < MIN_VALID_BYTES:
            return False
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            return width > 0 and height > 0
    except Exception:
        return False


def convert_to_avif(source: Path, destination: Path, *, quality: int = AVIF_QUALITY) -> Path:
    if not source.is_file():
        raise CoserAvifError(f"图片不存在：{source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve() and source.suffix.lower() == ".avif":
        if avif_is_valid(source):
            if destination.exists() and destination.resolve() != source.resolve():
                destination.unlink()
            shutil.copy2(source, destination)
            if avif_is_valid(destination):
                return destination
    try:
        with Image.open(source) as opened:
            oriented = ImageOps.exif_transpose(opened)
            has_alpha = oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info
            if has_alpha:
                rgba = oriented.convert("RGBA")
                canvas = Image.new(
                    "RGBA", rgba.size, (*ImageColor.getrgb(AVIF_BACKGROUND), 255)
                )
                canvas.alpha_composite(rgba)
                output = canvas.convert("RGB")
            else:
                output = oriented.convert("RGB")
            output.save(destination, format="AVIF", quality=max(1, min(int(quality), 100)))
    except Exception as exc:
        raise CoserAvifError(f"转 AVIF 失败：{source.name}：{exc}") from exc
    if not avif_is_valid(destination):
        raise CoserAvifError(f"转出的 AVIF 损坏：{destination.name}")
    return destination
