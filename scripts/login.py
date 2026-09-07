#!/usr/bin/env python3
"""Deprecated. Manga ingest now uses a SignPulse account from /accounts."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "漫画采集已接入 SignPulse 账号。请打开面板 /accounts 添加并登录采集号，"
        "再到 /manga 选择该账号。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
