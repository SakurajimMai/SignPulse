from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from backend.services.manga.filters import (
    is_blocked_document,
    is_image_message,
    is_video_message,
    parse_caption,
    parse_filter_keywords,
    should_accept_outbound_video,
    should_skip_user_comment,
    video_duration_seconds,
)


def make_message(
    media,
    *,
    text: str = "",
    reply_to_msg_id: int | None = None,
    sender=None,
    post: bool = False,
    grouped_id: int | None = None,
):
    return SimpleNamespace(
        media=media,
        document=getattr(media, "document", None),
        message=text,
        raw_text=text,
        reply_to_msg_id=reply_to_msg_id,
        sender_id=123,
        sender=sender,
        post=post,
        from_id=None,
        grouped_id=grouped_id,
    )


def document_message(
    *,
    mime_type: str,
    filename: str,
    attributes=None,
):
    doc = Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=datetime.now(timezone.utc),
        mime_type=mime_type,
        size=1,
        dc_id=1,
        attributes=[DocumentAttributeFilename(filename), *(attributes or [])],
    )
    return make_message(MessageMediaDocument(document=doc))


class CaptionTests(unittest.TestCase):
    def test_author_and_hashtags_are_not_part_of_title(self):
        parsed = parse_caption(
            "[Ryx]浮乱看护士-臭脚控治疗篇 01（上）\n"
            "#女护士 #丝袜 #白丝\n"
            "内容和压缩包放在评论区，无法滑动翻页请使用telegram X"
        )

        self.assertEqual(parsed.author, "Ryx")
        self.assertEqual(parsed.title, "浮乱看护士-臭脚控治疗篇 01（上）")
        self.assertEqual(parsed.tags, ["女护士", "丝袜", "白丝"])

    def test_fullwidth_author_brackets_are_supported(self):
        parsed = parse_caption("【Ryx】作品名称 #标签")

        self.assertEqual(parsed.author, "Ryx")
        self.assertEqual(parsed.title, "作品名称")

    def test_telegram_boilerplate_lines_are_not_title(self):
        parsed = parse_caption(
            "[Ryx]作品名称\n"
            "更新\n"
            "已整理，来源于 3D漫画 聊天\n"
            "从第一张内容开始这些都是不需要的"
        )

        self.assertEqual(parsed.title, "作品名称")

    def test_telegraph_channel_caption_keeps_title_and_hash_author(self):
        parsed = parse_caption(
            "我妻子接受的特别教育\n"
            "作者：#chihel\n"
            "男性：#大根 #黑皮 #屈辱 #sph\n"
            "女性：#巨乳 #受孕 #NTR #送妻\n"
            "E站源\n"
            "♠在telegraph观看♠\n"
            "BBC在评论区观看\n"
            "#媚黑 #漫画"
        )

        self.assertEqual(parsed.title, "我妻子接受的特别教育")
        self.assertEqual(parsed.author, "chihel")
        self.assertIn("NTR", parsed.tags)
        self.assertIn("媚黑", parsed.tags)

    def test_empty_original_and_character_labels_are_not_title(self):
        parsed = parse_caption("kei SPH/BBC\n原作:\n角色:")
        self.assertEqual(parsed.title, "kei SPH/BBC")

        parsed = parse_caption("kei SPH/BBC 原作: 角色：")
        self.assertEqual(parsed.title, "kei SPH/BBC")

        parsed = parse_caption("Rio NTR 原作： 角色： 艺术家：")
        self.assertEqual(parsed.title, "Rio NTR")

        from backend.services.manga.filters import clean_display_title

        self.assertEqual(
            clean_display_title("光之女神帕露蒂娜的堕落 原作: 角色:"),
            "光之女神帕露蒂娜的堕落",
        )
        self.assertEqual(
            clean_display_title("早乙女乱马 铃鹿御前 BBC 原作： Order"),
            "早乙女乱马 铃鹿御前 BBC",
        )
        parsed = parse_caption(
            "早乙女乱马 铃鹿御前 BBC\n"
            "原作： Order\n"
            "作者：#HellToYou\n"
            "#媚黑 #漫画"
        )
        self.assertEqual(parsed.title, "早乙女乱马 铃鹿御前 BBC")
        self.assertEqual(parsed.author, "HellToYou")

    def test_book_cover_title_keeps_extra_and_skips_author(self):
        parsed = parse_caption(
            "🔥《补习老师的勾心斗角》 +幕后图 共 338 P\n"
            "\n"
            "你看着吧!早晚有一天我要打烂她的肥屁股,让她知道怎么做人!\n"
            "\n"
            "#R18 #pixiv #3D #凌辱 #巨乳 #3D #剧情 #调教 #补习老师的勾心斗角 #SM #制服 #Lula"
        )
        self.assertEqual(parsed.title, "补习老师的勾心斗角 +幕后图")
        self.assertIsNone(parsed.author)
        self.assertIn("巨乳", parsed.tags)
        self.assertIn("Lula", parsed.tags)
        self.assertEqual(parsed.tags.count("3D"), 1)

        with_brackets = parse_caption(
            "🔥《补习老师的勾心斗角》 +幕后图 +视频 共 338 P\n"
            "[Lula]\n"
            "#Lula #师生"
        )
        self.assertEqual(with_brackets.title, "补习老师的勾心斗角 +幕后图 +视频")
        self.assertIsNone(with_brackets.author)
        self.assertEqual(with_brackets.tags, ["Lula", "师生"])

        named = parse_caption(
            "🔥《催眠淫纹系统》 共 84 P\n"
            "作者：#糖果的骑士\n"
            "#18R #催眠淫纹系统"
        )
        self.assertEqual(named.title, "催眠淫纹系统")
        self.assertEqual(named.author, "糖果的骑士")

        sequel = parse_caption(
            "🔥《我妻柒柒》改 第十话 共 100 P\n"
            "\n"
            "#18R #AI #pixiv #我妻柒柒"
        )
        self.assertEqual(sequel.title, "我妻柒柒 改 第十话")
        self.assertIsNone(sequel.author)
        self.assertIn("我妻柒柒", sequel.tags)

        inside = parse_caption("🔥《我妻柒柒改》第十话\n#18R")
        self.assertEqual(inside.title, "我妻柒柒改 第十话")

        chaptered = parse_caption("🔥《催眠淫纹系统》 13.1 共 84 P\n#18R")
        self.assertEqual(chaptered.title, "催眠淫纹系统 13.1")

        long_blurb = parse_caption(
            "🔥《哈莉奎茵》 +我把老板性感的妻子变成了婊子随心所欲地训练和玩弄她 共 100 P\n#AI"
        )
        self.assertEqual(long_blurb.title, "哈莉奎茵")

        from backend.services.manga.filters import merge_reader_title

        self.assertEqual(
            merge_reader_title("催眠大师", "催眠大师 29"),
            "催眠大师 29",
        )
        self.assertEqual(
            merge_reader_title("我妻柒柒 改 第十话", "我妻柒柒改十"),
            "我妻柒柒 改 第十话",
        )
        self.assertEqual(
            merge_reader_title("ronna", "火线营救"),
            "火线营救",
        )
        self.assertEqual(
            merge_reader_title("远房弟弟 +幕后图", "远房弟弟 1+幕后图", "《远房弟弟》1+幕后图"),
            "远房弟弟 1+幕后图",
        )


class MediaFilterTests(unittest.TestCase):
    def test_regular_photo_is_an_image(self):
        message = make_message(MessageMediaPhoto())

        self.assertTrue(is_image_message(message))

    def test_archive_is_rejected_even_when_sent_as_a_document(self):
        message = document_message(mime_type="application/octet-stream", filename="chapter.ZIP")

        self.assertTrue(is_blocked_document(message))
        self.assertFalse(is_image_message(message))

    def test_video_is_accepted_for_channel_but_not_as_manga_page(self):
        message = document_message(
            mime_type="video/mp4",
            filename="drama.mp4",
            attributes=[DocumentAttributeVideo(duration=1, w=1, h=1)],
        )

        self.assertFalse(is_blocked_document(message))
        self.assertTrue(is_video_message(message))
        self.assertFalse(is_image_message(message))
        self.assertEqual(video_duration_seconds(message), 1)


class VideoForwardFilterTests(unittest.TestCase):
    def test_parse_keywords_splits_lines_and_commas(self):
        self.assertEqual(
            parse_filter_keywords("啪啪秀\n182体育, 女厕"),
            ["啪啪秀", "182体育", "女厕"],
        )

    def test_block_keyword_and_duration_bounds(self):
        self.assertFalse(
            should_accept_outbound_video(
                text="高三爱情故事 第19集",
                duration=200,
                block_keywords=["高三爱情故事"],
            )
        )
        self.assertTrue(
            should_accept_outbound_video(
                text="诛仙 小白 问心+50分钟剧情视频",
                duration=3002,
                block_keywords=["高三爱情故事"],
                min_seconds=60,
            )
        )
        self.assertFalse(
            should_accept_outbound_video(text="ok", duration=10, min_seconds=60)
        )
        self.assertFalse(
            should_accept_outbound_video(
                text="广告片",
                duration=90,
                allow_keywords=["剧情视频"],
            )
        )
        self.assertFalse(should_accept_outbound_video(text="ok", binding_enabled=False))


class CommentFilterTests(unittest.TestCase):
    def test_user_reply_image_is_skipped(self):
        message = make_message(
            MessageMediaPhoto(),
            text="w",
            reply_to_msg_id=10,
            sender=SimpleNamespace(bot=False, broadcast=False),
        )

        self.assertTrue(
            should_skip_user_comment(
                message,
                ignore_user_comments=True,
                allowed_sender_ids=set(),
            )
        )

    def test_download_promotion_is_skipped(self):
        message = make_message(
            MessageMediaPhoto(),
            text="⬇️⬇️点击下载",
            post=True,
        )

        self.assertTrue(
            should_skip_user_comment(
                message,
                ignore_user_comments=True,
                allowed_sender_ids=set(),
            )
        )

    def test_title_card_from_user_is_kept(self):
        message = make_message(
            MessageMediaPhoto(),
            text="[Ryx]作品名称",
            sender=SimpleNamespace(bot=False, broadcast=False),
        )

        self.assertFalse(
            should_skip_user_comment(
                message,
                ignore_user_comments=True,
                allowed_sender_ids=set(),
            )
        )

    def test_captionless_non_reply_album_page_is_kept(self):
        message = make_message(
            MessageMediaPhoto(),
            sender=SimpleNamespace(bot=False, broadcast=False),
            grouped_id=99,
        )

        self.assertFalse(
            should_skip_user_comment(
                message,
                ignore_user_comments=True,
                allowed_sender_ids=set(),
            )
        )

    def test_discussion_album_reply_is_kept(self):
        message = make_message(
            MessageMediaPhoto(),
            reply_to_msg_id=10,
            grouped_id=99,
            sender=SimpleNamespace(bot=False, broadcast=False),
        )

        self.assertFalse(
            should_skip_user_comment(
                message,
                ignore_user_comments=True,
                allowed_sender_ids=set(),
            )
        )

    def test_captionless_thread_reply_image_is_kept(self):
        message = make_message(
            MessageMediaPhoto(),
            reply_to_msg_id=10,
            sender=SimpleNamespace(bot=False, broadcast=False),
        )

        self.assertFalse(
            should_skip_user_comment(
                message,
                ignore_user_comments=True,
                allowed_sender_ids=set(),
            )
        )


if __name__ == "__main__":
    unittest.main()
