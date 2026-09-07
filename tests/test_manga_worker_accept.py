from __future__ import annotations

from types import SimpleNamespace

from backend.services.manga.config import MangaSettings
from backend.services.manga.telegram_worker import ResolvedBinding, TelegramMangaWorker


def _worker() -> TelegramMangaWorker:
    worker = TelegramMangaWorker(MangaSettings())
    worker._discussion_only = True
    binding = ResolvedBinding(
        channel_id=-1002285859531,
        channel_title="3D漫画",
        discussion_id=-1002161690727,
        discussion_title="3D漫画 聊天",
        ingest_mode="discussion",
    )
    worker._index_binding(-1002285859531, binding)
    worker._index_binding(-1002161690727, binding)
    return worker


def _message(*, video: bool = False, photo: bool = False, post: bool = False, reply_to=None):
    return SimpleNamespace(
        video=object() if video else None,
        photo=object() if photo else None,
        document=None,
        media=None,
        caption=None,
        text=None,
        message=None,
        raw_text=None,
        reply_to_message_id=reply_to,
        reply_to_msg_id=reply_to,
        media_group_id=None,
        grouped_id=None,
        sender_id=None,
        from_user=None,
        sender_chat=SimpleNamespace(id=-1002285859531) if post else None,
        post=post,
        from_id=None,
        sender=None,
    )


def test_discussion_only_keeps_channel_video_and_drops_channel_photo():
    worker = _worker()
    channel = SimpleNamespace(type="channel", id=-1002285859531)
    video = _message(video=True, post=True)
    photo = _message(photo=True, post=True)
    assert worker._should_accept(video, -1002285859531, channel) is True
    assert worker._should_accept(photo, -1002285859531, channel) is False


def test_binding_can_disable_videos():
    worker = _worker()
    binding = ResolvedBinding(
        channel_id=-1002285859531,
        channel_title="3D漫画",
        discussion_id=-1002161690727,
        discussion_title="3D漫画 聊天",
        ingest_mode="discussion",
        forward_videos=False,
    )
    worker._index_binding(-1002285859531, binding)
    worker._index_binding(-1002161690727, binding)
    channel = SimpleNamespace(type="channel", id=-1002285859531)
    video = _message(video=True, post=True)
    assert worker._should_accept(video, -1002285859531, channel) is False


def test_discussion_only_requires_reply_even_for_videos():
    worker = _worker()
    discussion = SimpleNamespace(type="supergroup", id=-1002161690727)
    video = _message(video=True, post=True)
    photo = _message(photo=True, post=True)
    assert worker._should_accept(video, -1002161690727, discussion) is False
    assert worker._should_accept(photo, -1002161690727, discussion) is False
    replied_video = _message(video=True, post=True, reply_to=1073161)
    replied_photo = _message(photo=True, post=True, reply_to=1073161)
    assert worker._should_accept(replied_video, -1002161690727, discussion) is True
    assert worker._should_accept(replied_photo, -1002161690727, discussion) is True
