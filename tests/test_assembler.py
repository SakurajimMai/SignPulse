from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.services.manga.assembler import (
    ChapterAssembler,
    PendingPage,
    bucket_checkpoint_seconds,
    bucket_idle_seconds,
    collapse_reply_root,
)
from backend.services.manga.config import MangaSettings
from backend.services.manga.peers import same_telegram_peer, telegram_raw_peer_id
from backend.services.manga.publisher import alternate_source_keys


def make_page(
    message_id: int,
    *,
    grouped_id: int,
    reply_to_msg_id: int,
    metadata_caption: str | None = None,
) -> PendingPage:
    return PendingPage(
        message_id=message_id,
        local_path=Path(f"/tmp/{message_id}.jpg"),
        filename=f"{message_id}.jpg",
        caption=None,
        grouped_id=grouped_id,
        date=datetime.now(timezone.utc),
        sender_id=1,
        reply_to_msg_id=reply_to_msg_id,
        metadata_caption=metadata_caption,
    )


class DiscussionAssemblerTests(unittest.IsolatedAsyncioTestCase):
    async def test_discussion_albums_share_root_bucket_and_metadata(self):
        ready = []

        async def on_ready(chapter):
            ready.append(chapter)

        assembler = ChapterAssembler(
            MangaSettings(chapter_idle_seconds=60, chapter_max_pages=10),
            on_ready,
        )
        await assembler.add_page(
            -1001,
            "3D漫画 聊天",
            make_page(
                20,
                grouped_id=200,
                reply_to_msg_id=100,
                metadata_caption="[Ryx]作品名称 #标签\n内容和压缩包放在评论区",
            ),
        )
        await assembler.add_page(
            -1001,
            "3D漫画 聊天",
            make_page(21, grouped_id=201, reply_to_msg_id=100),
        )
        await assembler.stop()

        self.assertEqual(len(ready), 1)
        self.assertEqual([page.message_id for page in ready[0].pages], [20, 21])
        self.assertTrue(ready[0].is_final)
        self.assertEqual(ready[0].title_hint, "作品名称")
        self.assertEqual(ready[0].author_hint, "Ryx")
        self.assertEqual(ready[0].tags, ["标签"])

    async def test_new_discussion_thread_flushes_previous_thread(self):
        ready = []

        async def on_ready(chapter):
            ready.append(chapter)

        assembler = ChapterAssembler(
            MangaSettings(chapter_idle_seconds=60, chapter_max_pages=200),
            on_ready,
        )
        await assembler.add_page(
            -1001,
            "3D漫画 聊天",
            make_page(20, grouped_id=200, reply_to_msg_id=100),
        )
        await assembler.add_page(
            -1001,
            "3D漫画 聊天",
            make_page(40, grouped_id=400, reply_to_msg_id=300),
        )
        self.assertEqual(len(ready), 1)
        self.assertEqual([page.message_id for page in ready[0].pages], [20])
        self.assertTrue(ready[0].is_final)
        await assembler.stop()
        self.assertEqual(len(ready), 2)
        self.assertEqual([page.message_id for page in ready[1].pages], [40])
        self.assertTrue(ready[1].is_final)

    async def test_checkpoint_does_not_mark_thread_final(self):
        ready = []

        async def on_ready(chapter):
            ready.append(chapter)

        assembler = ChapterAssembler(
            MangaSettings(chapter_idle_seconds=60, chapter_max_pages=200),
            on_ready,
        )
        await assembler.add_page(
            -1001,
            "3D漫画 聊天",
            make_page(20, grouped_id=200, reply_to_msg_id=100),
        )
        await assembler._checkpoint("-1001:r:100")
        self.assertEqual(len(ready), 1)
        self.assertFalse(ready[0].is_final)
        self.assertEqual(ready[0].source_key, "-1001:r:100")
        await assembler.stop()
        self.assertEqual(len(ready), 2)
        self.assertTrue(ready[1].is_final)
        self.assertEqual(ready[1].pages, [])
        self.assertEqual(ready[1].source_key, "-1001:r:100")
        self.assertEqual(ready[1].title_hint, None)

    async def test_checkpoint_keeps_title_for_empty_final_flush(self):
        ready = []

        async def on_ready(chapter):
            ready.append(chapter)

        assembler = ChapterAssembler(
            MangaSettings(chapter_idle_seconds=60, chapter_max_pages=200),
            on_ready,
        )
        await assembler.add_page(
            -1001,
            "3D漫画 聊天",
            make_page(
                20,
                grouped_id=200,
                reply_to_msg_id=100,
                metadata_caption="[Can]色欲迷情 20 #标签",
            ),
        )
        await assembler._checkpoint("-1001:r:100")
        await assembler.stop()
        self.assertEqual(len(ready), 2)
        self.assertFalse(ready[0].is_final)
        self.assertTrue(ready[1].is_final)
        self.assertEqual(ready[1].title_hint, "色欲迷情 20")
        self.assertEqual(ready[1].author_hint, "Can")
        self.assertEqual(ready[1].tags, ["标签"])


class IdleAndPeerTests(unittest.TestCase):
    def test_reply_buckets_wait_at_least_15_minutes(self):
        self.assertEqual(bucket_idle_seconds("-1001:r:20", 8), 900.0)
        self.assertEqual(bucket_idle_seconds("-1001:g:20", 8), 8.0)
        self.assertEqual(bucket_checkpoint_seconds("-1001:r:20", 8), 90.0)

    def test_collapse_reply_root_follows_page_chain(self):
        root = collapse_reply_root(
            1076418,
            parent_of={1076418: 1076384, 1076425: 1076418},
            known_roots={1076384},
            page_to_root={1076418: 1076384},
        )
        self.assertEqual(root, 1076384)
        self.assertEqual(
            collapse_reply_root(
                1076425,
                parent_of={1076418: 1076384, 1076425: 1076418},
                known_roots={1076384},
                page_to_root={1076418: 1076384},
            ),
            1076384,
        )

    def test_raw_peer_id_strips_bot_api_prefix(self):
        self.assertEqual(telegram_raw_peer_id(-1001234567890), 1234567890)
        self.assertEqual(telegram_raw_peer_id(1234567890), 1234567890)
        self.assertTrue(same_telegram_peer(-1001234567890, 1234567890))
        self.assertEqual(
            alternate_source_keys("-1001234567890:r:99"),
            ["-1001234567890:r:99", "1234567890:r:99"],
        )


if __name__ == "__main__":
    unittest.main()
