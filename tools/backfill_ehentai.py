"""把被首页 20 张截断的 E-Hentai 章节从断页补全，并重发主站。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from backend.services.manga.config import load_manga_settings
    from backend.services.manga.db import close_db, init_db
    from backend.services.manga.ehentai.worker import EhentaiWorker

    host_data = ROOT / "data"
    settings = load_manga_settings()
    settings = replace(
        settings,
        data_dir=str(host_data),
        database_url=f"sqlite:///{host_data / 'tg_manga.db'}",
        temp_dir=str(host_data / "tmp"),
    )
    settings.ensure_dirs()
    await init_db(settings)
    worker = EhentaiWorker(settings, lambda: None)
    try:
        report = await worker.backfill_incomplete()
    finally:
        await worker.stop()
        await close_db()
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
