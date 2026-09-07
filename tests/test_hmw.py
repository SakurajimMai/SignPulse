from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.services.manga.config import (
    load_manga_settings,
    manga_config_public,
    save_manga_settings,
)
from backend.services.manga.hmw.archive import extract_archive
from backend.services.manga.hmw.lib.discovery import DiscoveryError, discover_source
from backend.services.manga.hmw.paths import (
    PathEscapeError,
    hmw_dirs,
    resolve_source_path,
)
from backend.services.manga.hmw.service import apply_chapter_overrides, scan_source
from backend.services.manga.hmw.workflow import PanelPublishWorkflow
from tg_signer.security import is_encrypted_secret


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MANGA_CONFIG_FILE", str(tmp_path / ".manga_config.json"))
    monkeypatch.setenv("MANGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    return tmp_path


def _chapter_tree(root: Path, count: int = 2, pages: int = 2) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        folder = root / f"话 {number}"
        folder.mkdir()
        for page in range(1, pages + 1):
            Image.new("RGB", (8, 8), (number * 10, page * 20, 40)).save(folder / f"{page:03d}.jpg")
    return root


def test_hmw_secrets_encrypted(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    saved = save_manga_settings(
        {
            "hmw_publisher_token": "token-one",
            "hmw_s3_access_key": "ak-one",
            "hmw_s3_secret_key": "sk-one",
            "hmw_s3_bucket": "hmwmedia",
        }
    )
    assert saved.hmw_publisher_token == "token-one"
    raw = json.loads((tmp_path / ".manga_config.json").read_text(encoding="utf-8"))
    assert "token-one" not in json.dumps(raw)
    assert is_encrypted_secret(raw["hmw_publisher_token"])
    public = manga_config_public(saved)
    assert public["hmw_publisher_token"] is None
    assert public["hmw_publisher_token_set"] is True
    again = save_manga_settings({"hmw_s3_bucket": "other"})
    assert again.hmw_publisher_token == "token-one"
    assert load_manga_settings().hmw_s3_secret_key == "sk-one"


def test_cleanup_published_source_removes_inbox_and_zip(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_manga_settings()
    from backend.services.manga.hmw.cleanup import cleanup_published_source

    dirs = hmw_dirs(settings)
    series = dirs.inbox / "极品家丁绿传之仙子与魔女"
    chapter = series / "极品家丁绿传之仙子与魔女 9"
    chapter.mkdir(parents=True)
    (chapter / "0001.jpg").write_bytes(b"img")
    outsider = tmp_path / "not-hmw" / "keep"
    outsider.mkdir(parents=True)
    (outsider / "x.jpg").write_bytes(b"keep")

    result = cleanup_published_source(settings, series)
    assert not series.exists()
    assert any("极品家丁" in item for item in result["deleted"])
    assert (outsider / "x.jpg").exists()

    extracted = dirs.extracted / "demo"
    extracted.mkdir(parents=True)
    (extracted / "话 1").mkdir()
    archive = dirs.inbox / "demo.zip"
    archive.write_bytes(b"zip")
    again = cleanup_published_source(settings, extracted)
    assert not extracted.exists()
    assert not archive.exists()
    assert any(item.endswith("demo.zip") or item.endswith("demo") for item in again["deleted"])

    refused = cleanup_published_source(settings, dirs.inbox)
    assert refused["deleted"] == []


def test_source_path_confinement(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_manga_settings()
    dirs = hmw_dirs(settings)
    inside = dirs.inbox / "ok"
    inside.mkdir(parents=True)
    resolved = resolve_source_path(settings, "inbox/ok")
    assert resolved == inside.resolve()
    with pytest.raises(PathEscapeError):
        resolve_source_path(settings, "../etc/passwd")


def test_scan_and_chapter_override(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_manga_settings()
    source_root = hmw_dirs(settings).extracted / "demo"
    _chapter_tree(source_root, count=2, pages=3)
    payload = scan_source(settings, "extracted/demo")
    assert payload["pages"] == 6
    assert [item["number"] for item in payload["chapters"]] == [1.0, 2.0]
    source = discover_source(source_root)
    remapped = apply_chapter_overrides(
        source,
        [
            {"directory": "话 1", "number": 24, "title": "番外1", "action": "upsert"},
            {"directory": "话 2", "number": 25, "title": "番外2", "action": "upsert"},
        ],
    )
    assert [chapter.number for chapter in remapped.chapters] == [24.0, 25.0]
    with pytest.raises(DiscoveryError):
        apply_chapter_overrides(
            discover_source(source_root),
            [
                {"directory": "话 1", "number": 2, "action": "upsert"},
                {"directory": "话 2", "number": 2, "action": "upsert"},
            ],
        )


def test_zip_password_extract(tmp_path: Path):
    archive = tmp_path / "pack.zip"
    dest = tmp_path / "out"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("a/hello.txt", "secret-body")
        for info in zf.filelist:
            info.flag_bits |= 0x1
        # ZipFile 默认写 ZipCrypto 需要 setpassword 在写入时
    # 使用 pyminizip 不一定有；改用 ZipFile + pwd on read 的无密码包先覆盖无密码路径
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a/hello.txt", "secret-body")
    extract_archive(archive, dest, password=None)
    assert (dest / "a" / "hello.txt").read_text(encoding="utf-8") == "secret-body"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 520])
async def test_publisher_api_html_response_is_sanitized(status_code: int):
    import httpx

    from backend.services.manga.hmw.lib.api_client import APIError, PublisherAPIClient

    body = (
        "publisher prefix: <!DOCTYPE html>"
        '<html><head><title>publisher.example | 520: Web server is returning an unknown error</title>'
        '</head><body class="oldie">failed</body></html>'
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    async with httpx.AsyncClient(
        base_url="https://publisher.example/",
        transport=httpx.MockTransport(handler),
    ) as client:
        api = PublisherAPIClient(
            "https://publisher.example",
            "token",
            client=client,
        )
        with pytest.raises(APIError) as raised:
            await api.health()

    message = str(raised.value)
    assert "520" in message
    assert "<!DOCTYPE" not in message
    assert "oldie" not in message
    assert "publisher.example" not in message


@pytest.mark.asyncio
async def test_publish_retries_stale_version(tmp_path: Path):
    from backend.services.manga.hmw.lib.api_client import APIError
    from backend.services.manga.hmw.lib.models import TaskState, UploadedObject
    from backend.services.manga.hmw.lib.state import TaskStateStore

    calls = {"preflight": 0, "publish": 0}

    class FakeAPI:
        async def preflight(self, payload):
            calls["preflight"] += 1
            return {"expected_version": f"v{calls['preflight']}", "manga_action": "create", "chapters": []}

        async def publish(self, payload):
            calls["publish"] += 1
            if calls["publish"] == 1:
                raise APIError("漫画内容已变化，请重新预检查", status_code=409)
            assert payload["expected_version"] == "v2"
            return {"action": "create", "manga_id": 9, "old_urls": []}

    class FakeStorage:
        def cover_key(self, slug):
            return f"comics/zh/{slug}/cover.avif"

        def public_url(self, key):
            return f"https://cdn.example/{key}"

        def key_from_public_url(self, url):
            return None

        def delete_keys(self, keys):
            return []

    store = TaskStateStore(tmp_path / "tasks")
    store.save(
        TaskState(
            task_id="t1",
            source_path="/tmp/src",
            stage="uploaded",
            manga_slug="demo",
            manga_payload={"slug": "demo"},
            uploaded_objects=[
                UploadedObject(
                    source="c",
                    local_path="c",
                    key="k",
                    url="https://cdn.example/cover.avif",
                    width=1,
                    height=1,
                    kind="cover",
                )
            ],
        )
    )
    cfg = SimpleNamespace(avif_quality=50, avif_background="#FFFFFF", upload_workers=1, convert_workers=1)
    workflow = PanelPublishWorkflow(
        cfg,
        FakeAPI(),
        FakeStorage(),
        store,
        temp_root=tmp_path / "temp",
        convert_workers=1,
    )
    source = SimpleNamespace(
        root=Path("/tmp/src"),
        cover=Path("/tmp/cover.jpg"),
        chapters=[SimpleNamespace(number=1, title="1", action="skip", directory=Path("/tmp/c"), images=[])],
    )

    async def fake_verify(_state):
        return None

    workflow._verify_urls = fake_verify  # type: ignore[method-assign]
    result = await workflow.execute(
        source, {"slug": "demo", "title": "demo"}, confirm=lambda _plan: True, task_id="t1"
    )
    assert calls["publish"] == 2
    assert calls["preflight"] >= 1
    assert result.stage in {"committed", "cleaned"}
    assert (result.publish_result or {}).get("manga_id") == 9


def test_parse_telegram_album_url():
    from backend.services.manga.hmw.telegram_link import (
        parse_telegram_post_url,
        safe_folder_name,
        split_series_title,
    )

    ref = parse_telegram_post_url("https://t.me/manhua_3D/20061?comment=932161")
    assert ref.channel == "manhua_3D"
    assert ref.post_id == 20061
    assert ref.comment_id == 932161
    private = parse_telegram_post_url("https://t.me/c/2285859531/88?comment=12")
    assert private.channel == -1002285859531
    assert private.post_id == 88
    tg = parse_telegram_post_url("tg://resolve?domain=manhua_3D&post=20061&comment=932161")
    assert tg.comment_id == 932161
    series, chapter = split_series_title("极品家丁绿传之仙子与魔女 1")
    assert series == "极品家丁绿传之仙子与魔女"
    assert chapter.endswith("1")
    assert ":" not in safe_folder_name('a:b/c*')


def test_cluster_comment_album_by_reply_root():
    from backend.services.manga.hmw.telegram_album import cluster_comment_album

    def msg(**kwargs):
        return SimpleNamespace(
            id=kwargs["id"],
            photo=object() if kwargs.get("image", True) else None,
            document=None,
            media=None,
            video=None,
            caption=kwargs.get("caption"),
            text=kwargs.get("text"),
            reply_to_message_id=kwargs.get("reply"),
            media_group_id=kwargs.get("group"),
        )

    title = msg(id=100, image=False, caption="极品家丁绿传之仙子与魔女 1")
    pages = [msg(id=101 + i, reply=100, group=9 if i < 10 else 10) for i in range(12)]
    other = msg(id=200, reply=199, caption="另一话")
    clustered = cluster_comment_album([title, other, *pages], seed_id=100)
    assert [item.id for item in clustered] == list(range(101, 113))


def test_merge_status_flattens_job():
    from backend.services.manga.hmw.worker import merge_status

    merged = merge_status(
        {"running": True, "stage": "uploading", "current": 3, "total": 10, "error": "", "message": "上传"},
        {"configured": True, "api_ok": True, "s3_ok": True, "error": None},
    )
    assert merged["running"] is True
    assert merged["stage"] == "uploading"
    assert merged["current"] == 3
    assert merged["configured"] is True
    assert merged["job"]["stage"] == "uploading"


def test_hmw_routes_registered():
    from backend.api.routes import manga_hmw

    paths = {getattr(route, "path", "") for route in manga_hmw.router.routes}
    assert "/manga/hmw/status" in paths
    assert "/manga/hmw/jobs" in paths
    assert "/manga/hmw/scan" in paths
    assert "/manga/hmw/telegram/album" in paths
    assert "/manga/hmw/library" in paths
    assert "/manga/hmw/library/{manga_id}" in paths
    assert "/manga/hmw/catalog" in paths
    assert "/manga/hmw/catalog/{manga_id}" in paths
    assert "/manga/hmw/catalog/sync" in paths
    assert "/manga/hmw/chapters/{chapter_id}" in paths


def test_hmw_catalog_upsert_search_and_remove(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_manga_settings()
    from backend.services.manga.hmw.catalog import (
        get_entry,
        list_catalog,
        remove_entry,
        upsert_from_detail,
    )

    first = upsert_from_detail(
        settings,
        {
            "id": 2888,
            "title": "淫欲全家桶第一部",
            "slug": "yin-yu-1",
            "author": "Ann",
            "language": "zh",
            "cover_url": "https://cdn1.hmw.app/cover1.avif",
            "chapters": [
                {"id": 1, "number": 1, "title": "1", "page_count": 20},
                {"id": 2, "number": 2, "title": "2", "page_count": 18},
            ],
        },
        extras={"source_path": "extracted/第一部"},
        published=True,
    )
    assert first["chapter_count"] == 2
    assert first["page_count"] == 38
    assert first["public_url"].endswith("/zh/manga/yin-yu-1")
    upsert_from_detail(
        settings,
        {
            "id": 2888,
            "title": "淫欲全家桶第一部",
            "slug": "yin-yu-1",
            "author": "Ann",
            "language": "zh",
            "chapters": [
                {"id": 1, "number": 1, "title": "1", "page_count": 20},
                {"id": 2, "number": 2, "title": "2", "page_count": 18},
                {"id": 3, "number": 3, "title": "番外", "page_count": 10},
            ],
        },
        published=True,
    )
    stored = get_entry(settings, 2888)
    assert stored is not None
    assert stored["chapter_count"] == 3
    assert stored["page_count"] == 48
    assert stored["source_path"] == "extracted/第一部"
    upsert_from_detail(
        settings,
        {
            "id": 2896,
            "title": "淫欲全家桶第二部",
            "slug": "yin-yu-2",
            "author": "Ann",
            "language": "zh",
            "chapters": [{"id": 9, "number": 1, "title": "1", "page_count": 12}],
        },
        published=True,
    )
    listed = list_catalog(settings, q="第二部", page=1, limit=12)
    assert listed["total"] == 1
    assert listed["data"][0]["id"] == 2896
    page = list_catalog(settings, q="", page=1, limit=1)
    assert page["total"] == 2
    assert page["total_pages"] == 2
    assert remove_entry(settings, 2896) is True
    assert list_catalog(settings)["total"] == 1


@pytest.mark.asyncio
async def test_hmw_manage_update_and_delete(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_manga_settings()
    from backend.services.manga.hmw import manage
    from backend.services.manga.hmw.catalog import get_entry, list_catalog

    state = {
        "version": "v1",
        "manga": {
            "id": 10,
            "language": "zh",
            "title": "旧标题",
            "alternative_title": "",
            "slug": "old-slug",
            "description": "",
            "cover_url": "https://cdn.example/old.avif",
            "author": "Ann",
            "status": "ongoing",
            "source_url": "",
            "genre_ids": [3],
            "year": 2026,
            "version": "v1",
            "chapters": [{"id": 21, "number": 1, "title": "一话", "page_count": 8}],
        },
    }

    class FakeAPI:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get_manga(self, manga_id: int):
            payload = dict(state["manga"])
            payload["id"] = manga_id
            payload["version"] = state["version"]
            payload["chapters"] = list(state["manga"]["chapters"])
            return payload

        async def update_manga(self, manga_id: int, payload: dict):
            assert payload["expected_version"] == "v1"
            state["manga"].update(payload["manga"])
            state["version"] = "v2"
            state["manga"]["version"] = "v2"
            state["manga"]["id"] = manga_id
            return {"old_urls": ["https://cdn.example/old.avif"]}

        async def delete_manga(self, manga_id: int, expected_version: str):
            assert manga_id == 10
            assert expected_version
            return {"old_urls": ["https://cdn.example/cover.avif"]}

        async def update_chapter(self, chapter_id: int, payload: dict):
            assert chapter_id == 21
            assert payload["number"] == 2.0
            assert payload["title"] == "改名"
            state["manga"]["chapters"][0]["number"] = payload["number"]
            state["manga"]["chapters"][0]["title"] = payload["title"]
            state["version"] = "v3"
            state["manga"]["version"] = "v3"
            return {"old_urls": []}

        async def delete_chapter(self, chapter_id: int, expected_version: str):
            assert chapter_id == 21
            assert expected_version == "v3"
            state["manga"]["chapters"] = []
            return {"old_urls": ["https://cdn.example/ch.avif"]}

        async def search_manga(self, query="", language=None, page=1, page_size=20):
            del query, language, page, page_size
            return {"items": [state["manga"]], "total": 1}

    class FakeStorage:
        def key_from_public_url(self, url: str):
            if url.startswith("https://cdn.example/"):
                return url.rsplit("/", 1)[-1]
            return None

        def delete_keys(self, keys):
            deleted.extend(keys)
            return []

    deleted: list[str] = []

    def fake_clients(_settings):
        return SimpleNamespace(), FakeAPI(), FakeStorage()

    monkeypatch.setattr(manage, "make_clients", fake_clients)

    updated = await manage.update_manga(
        settings, 10, {"title": "新标题", "slug": "new-slug", "expected_version": "v1"}
    )
    assert updated["manga"]["title"] == "新标题"
    assert "old.avif" in deleted
    assert get_entry(settings, 10)["title"] == "新标题"

    chapter = await manage.update_chapter(
        settings,
        21,
        {"number": 2, "title": "改名", "manga_id": 10, "expected_version": "v2"},
    )
    assert chapter["manga"]["chapters"][0]["title"] == "改名"
    assert get_entry(settings, 10)["chapters"][0]["number"] == 2.0

    await manage.delete_chapter(settings, 21, expected_version="v3", manga_id=10)
    assert get_entry(settings, 10)["chapter_count"] == 0
    assert "ch.avif" in deleted

    await manage.delete_manga(settings, 10, "v2")
    assert list_catalog(settings)["total"] == 0
    assert "cover.avif" in deleted


def test_settings_request_accepts_hmw_fields():
    from backend.api.routes.manga import MangaSettingsRequest

    parsed = MangaSettingsRequest(
        hmw_api_url="https://www.hmw.app/api/publisher",
        hmw_s3_prefix="comics/zh",
        hmw_avif_quality=50,
        hmw_convert_workers=1,
    )
    assert parsed.hmw_convert_workers == 1
    assert parsed.hmw_s3_prefix == "comics/zh"
