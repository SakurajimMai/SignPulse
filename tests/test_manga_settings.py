from __future__ import annotations

from pathlib import Path

from backend.services.manga.config import (
    load_manga_settings,
    manga_config_public,
    save_manga_settings,
)
from backend.services.manga.sources import parse_source_bindings
from tg_signer.security import is_encrypted_secret


def test_save_manga_settings_encrypts_secrets_once(tmp_path: Path, monkeypatch):
    config_file = tmp_path / ".manga_config.json"
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")

    saved = save_manga_settings(
        {
            "enabled": True,
            "tg_source_chats": "-100123",
            "telegram_api_hash": "hash-one",
            "site_publish_secret": "publish-one",
        }
    )

    assert saved.telegram_api_hash == "hash-one"
    assert saved.site_publish_secret == "publish-one"
    raw = config_file.read_text(encoding="utf-8")
    assert "hash-one" not in raw
    assert "publish-one" not in raw
    stored = __import__("json").loads(raw)
    assert is_encrypted_secret(stored["telegram_api_hash"])
    assert is_encrypted_secret(stored["site_publish_secret"])

    loaded = load_manga_settings()
    assert loaded.telegram_api_hash == "hash-one"
    assert loaded.site_publish_secret == "publish-one"

    public = manga_config_public(loaded)
    assert public["telegram_api_hash"] is None
    assert public["telegram_api_hash_set"] is True
    assert public["site_publish_secret_set"] is True
    revealed = manga_config_public(loaded, reveal=True)
    assert revealed["telegram_api_hash"] == "hash-one"
    assert revealed["site_publish_secret"] == "publish-one"

    again = save_manga_settings({"tg_source_chats": "-100999"})
    assert again.telegram_api_hash == "hash-one"
    assert again.tg_source_chats == "-100999"
    reloaded = load_manga_settings()
    assert reloaded.telegram_api_hash == "hash-one"
    assert reloaded.site_publish_secret == "publish-one"


def test_parse_source_bindings_can_skip_ingest():
    pairs = parse_source_bindings(
        [
            {"channel": "-1003298673411", "discussion": "", "skip_ingest": True},
            {"channel": "-1001", "discussion": "-1002", "ingest_enabled": True},
        ]
    )
    assert pairs[0].ingest_enabled is False
    assert pairs[0].is_telegraph() is True
    assert pairs[1].ingest_enabled is True


def test_parse_source_bindings_pairs_and_legacy():
    pairs = parse_source_bindings(
        [{"channel": "@chan1", "discussion": "-1002"}, {"channel": "@chan2", "discussion": ""}],
    )
    assert [(item.channel, item.discussion, item.resolved_ingest_mode()) for item in pairs] == [
        ("@chan1", "-1002", "discussion"),
        ("@chan2", "", "telegraph"),
    ]
    legacy = parse_source_bindings(None, legacy_chats="-1001, @old")
    assert [item.channel for item in legacy] == ["-1001", "@old"]
    assert parse_source_bindings("not-json", legacy_chats="@fallback")[0].channel == "@fallback"


def test_save_source_bindings_rewrites_legacy_chat_list(tmp_path: Path, monkeypatch):
    config_file = tmp_path / ".manga_config.json"
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")

    saved = save_manga_settings(
        {
            "source_bindings": [
                {"channel": "@collect", "discussion": "@talk"},
                {"channel": "-1009", "discussion": ""},
            ]
        }
    )
    assert saved.binding_list[0].channel == "@collect"
    assert saved.binding_list[0].discussion == "@talk"
    assert "@collect" in saved.tg_source_chats
    assert "@talk" in saved.tg_source_chats
    public = manga_config_public(saved)
    assert public["source_bindings"] == [
        {"channel": "@collect", "discussion": "@talk", "ingest_mode": "auto", "forward_videos": True, "ingest_enabled": True},
        {"channel": "-1009", "discussion": "", "ingest_mode": "auto", "forward_videos": True, "ingest_enabled": True},
    ]


def test_settings_request_accepts_long_chapter_and_new_bindings():
    """保存新频道绑定时会带上当前 chapter_max_pages；2000 必须能通过校验。"""
    from pydantic import ValidationError

    from backend.api.routes.manga import MangaSettingsRequest
    from backend.services.manga.config import CHAPTER_MAX_PAGES_CAP

    parsed = MangaSettingsRequest(
        chapter_max_pages=2000,
        outbound_enabled=True,
        outbound_channel="-1002085070183",
        outbound_preview_count=4,
        outbound_forward_videos=False,
        source_bindings=[
            {"channel": "-1002285859531", "discussion": "-1002161690727"},
            {"channel": "-1003298673411", "discussion": "", "ingest_enabled": False},
        ],
    )
    assert parsed.outbound_channel == "-1002085070183"
    assert parsed.outbound_forward_videos is False
    assert parsed.chapter_max_pages == 2000
    assert len(parsed.source_bindings or []) == 2
    assert parsed.source_bindings[1].ingest_enabled is False

    try:
        MangaSettingsRequest(chapter_max_pages=CHAPTER_MAX_PAGES_CAP + 1)
    except ValidationError as exc:
        assert "less than or equal to" in str(exc)
    else:
        raise AssertionError("expected ValidationError for oversized chapter_max_pages")


def test_save_telegram_account_name(tmp_path: Path, monkeypatch):
    config_file = tmp_path / ".manga_config.json"
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")

    saved = save_manga_settings(
        {"telegram_account_name": "collector", "outbound_forward_videos": False}
    )
    assert saved.outbound_forward_videos is False
    assert load_manga_settings().outbound_forward_videos is False
    again = save_manga_settings({"telegram_account_name": "collector"})
    assert again.outbound_forward_videos is False
    enabled = save_manga_settings({"outbound_forward_videos": True})
    assert enabled.outbound_forward_videos is True
    assert saved.telegram_account_name == "collector"
    loaded = load_manga_settings()
    assert loaded.telegram_account_name == "collector"
    public = manga_config_public(loaded)
    assert public["telegram_account_name"] == "collector"


def test_settings_request_accepts_ehentai_fields():
    from backend.api.routes.manga import MangaSettingsRequest

    parsed = MangaSettingsRequest(
        ehentai_enabled=True,
        ehentai_exhentai=True,
        ehentai_search="female:NTR language:Chinese",
        ehentai_cats="704",
        ehentai_max_pages=400,
        ehentai_search_pages=2,
        ehentai_delay_seconds=1.5,
        ehentai_gallery_delay_seconds=8,
        ehentai_poll_seconds=1800,
    )
    assert parsed.ehentai_enabled is True
    assert parsed.ehentai_exhentai is True
    assert parsed.ehentai_search_pages == 2
    assert parsed.ehentai_poll_seconds == 1800
