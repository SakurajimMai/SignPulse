from __future__ import annotations

import re
from pathlib import Path

from .models import ChapterCandidate, SourceDiscovery

IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


class DiscoveryError(ValueError):
    pass


def natural_sort_key(value: str | Path):
    text = str(value)
    volume_order = {"上": "1", "中": "2", "下": "3"}
    text = re.sub(
        r"([（(])([上中下])([）)])",
        lambda match: f"{match.group(1)}{volume_order[match.group(2)]}{match.group(3)}",
        text,
    )
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", text)
    ]


def _images(directory: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
        ),
        key=lambda path: natural_sort_key(path.name),
    )


def _default_chapter_number(name: str, index: int) -> float:
    match = re.search(r"\d+(?:\.\d+)?", name)
    return float(match.group(0)) if match else float(index)


def validate_chapter_numbers(numbers) -> list[float]:
    normalized = [float(number) for number in numbers]
    duplicates = sorted({number for number in normalized if normalized.count(number) > 1})
    if duplicates:
        labels = ", ".join(f"{number:g}" for number in duplicates)
        raise DiscoveryError(f"存在重复章节号：{labels}")
    return normalized


def discover_source(source: str | Path) -> SourceDiscovery:
    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise DiscoveryError("发布源必须是普通目录，不支持 ZIP 或单个文件")

    candidates: list[ChapterCandidate] = []
    directories = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: natural_sort_key(path.name),
    )
    for index, directory in enumerate(directories, start=1):
        images = _images(directory)
        if not images:
            continue
        candidates.append(
            ChapterCandidate(
                directory=directory,
                name=directory.name,
                number=_default_chapter_number(directory.name, index),
                title=directory.name,
                images=images,
            )
        )
    if not candidates:
        raise DiscoveryError("目录中没有包含图片的有效章节子目录")
    validate_chapter_numbers(chapter.number for chapter in candidates)

    root_images = _images(root)
    named_cover = next(
        (
            image
            for image in root_images
            if image.stem.casefold() in {"cover", "folder", "封面"}
        ),
        None,
    )
    cover = named_cover or (root_images[0] if root_images else candidates[0].images[0])
    return SourceDiscovery(root=root, cover=cover, chapters=candidates)
