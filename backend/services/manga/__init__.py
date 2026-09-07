"""SignPulse 漫画采集子模块。

使用 /accounts 中的 SignPulse 账号采集，目录数据库独立，由面板 `/manga` 配置和启停。
"""

from .config import MangaSettings, load_manga_settings, save_manga_settings
from .runtime import get_manga_runtime

__all__ = [
    "MangaSettings",
    "get_manga_runtime",
    "load_manga_settings",
    "save_manga_settings",
]
