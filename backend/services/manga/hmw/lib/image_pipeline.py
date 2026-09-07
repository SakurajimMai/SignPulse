from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageColor, ImageOps

from .discovery import natural_sort_key
from .models import ConvertedImage

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    pass


def convert_image(
    source: str | Path,
    destination: str | Path,
    *,
    quality: int,
    background: str = "#FFFFFF",
) -> ConvertedImage:
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source_path) as opened:
        oriented = ImageOps.exif_transpose(opened)
        has_alpha = oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info
        if has_alpha:
            rgba = oriented.convert("RGBA")
            rgb = Image.new("RGBA", rgba.size, (*ImageColor.getrgb(background), 255))
            rgb.alpha_composite(rgba)
            output = rgb.convert("RGB")
        else:
            output = oriented.convert("RGB")
        width, height = output.size
        output.save(destination_path, format="AVIF", quality=quality)

    payload = destination_path.read_bytes()
    return ConvertedImage(
        source=source_path,
        destination=destination_path,
        filename=destination_path.name,
        width=width,
        height=height,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def convert_chapter(
    sources: list[Path],
    destination: str | Path,
    *,
    quality: int,
    background: str = "#FFFFFF",
) -> list[ConvertedImage]:
    output_dir = Path(destination)
    ordered = sorted(sources, key=lambda path: natural_sort_key(path.name))
    return [
        convert_image(
            source,
            output_dir / f"{index:04d}.avif",
            quality=quality,
            background=background,
        )
        for index, source in enumerate(ordered, start=1)
    ]
