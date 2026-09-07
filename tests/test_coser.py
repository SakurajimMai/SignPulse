from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.coser.config import (
    coser_config_public,
    load_coser_settings,
    overlay_games_settings,
    save_coser_settings,
)
from backend.services.coser.gallery import GalleryResult
from backend.services.coser.paths import coser_dirs
from backend.services.coser.s3 import public_object_url, upload_file
from backend.services.coser.site import CoserSiteClient, public_work_url
from backend.services.coser.worker import CoserJobRunner, new_job
from backend.services.games.config import save_games_settings
from tg_signer.security import is_encrypted_secret


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("COSER_CONFIG_FILE", str(tmp_path / ".coser_config.json"))
    monkeypatch.setenv("COSER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GAMES_CONFIG_FILE", str(tmp_path / ".games_config.json"))
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    return tmp_path


def test_coser_secrets_encrypted(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    saved = save_coser_settings(
        {
            "site_email": "admin@icoser.de",
            "site_password": "AdminPass1",
            "s3_access_key": "AKIAEXAMPLE",
            "s3_secret_key": "s3-secret-one",
            "pack_password": "sakuramai",
        }
    )
    assert saved.site_password == "AdminPass1"
    assert saved.s3_secret_key == "s3-secret-one"
    raw = json.loads((tmp_path / ".coser_config.json").read_text(encoding="utf-8"))
    assert "AdminPass1" not in json.dumps(raw)
    assert "s3-secret-one" not in json.dumps(raw)
    assert is_encrypted_secret(raw["site_password"])
    assert is_encrypted_secret(raw["s3_secret_key"])
    public = coser_config_public(saved)
    assert public["site_password"] is None
    assert public["site_password_set"] is True
    assert public["s3_access_key"] is None
    assert public["s3_secret_key_set"] is True
    assert public["pack_password"] == "sakuramai"
    revealed = coser_config_public(saved, reveal=True)
    assert revealed["site_password"] == "AdminPass1"
    assert revealed["s3_access_key"] == "AKIAEXAMPLE"
    assert revealed["s3_secret_key"] == "s3-secret-one"
    again = save_coser_settings({"site_email": "ops@icoser.de"})
    assert again.site_password == "AdminPass1"
    assert load_coser_settings().pack_password == "sakuramai"


def test_coser_overlay_uses_games_credentials_and_own_paths(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    save_games_settings(
        {
            "baidu_cookie": "BDUSS=game-cookie",
            "baidu_remote_dir": "/网站/ixacg/game",
            "pikpak_refresh_token": "game-token",
            "pikpak_remote_dir": "/site/ixacg.top/Game",
        }
    )
    saved = save_coser_settings(
        {
            "baidu_remote_dir": "/网站/icoser.de/coser",
            "pikpak_remote_dir": "/site/icoser.de/coser",
        }
    )
    overlay = overlay_games_settings(saved)
    assert overlay.baidu_cookie == "BDUSS=game-cookie"
    assert overlay.pikpak_refresh_token == "game-token"
    assert overlay.baidu_remote_dir == "/网站/icoser.de/coser"
    assert overlay.pikpak_remote_dir == "/site/icoser.de/coser"
    assert overlay.baidu_remote_dir != "/网站/ixacg/game"


def test_public_work_url():
    settings = SimpleNamespace(site_url="https://icoser.de")
    assert public_work_url(settings, 12) == "https://icoser.de/zh/works/12"


def test_s3_public_object_url():
    settings = SimpleNamespace(
        s3_public_url="https://cdn.icoser.de",
        s3_endpoint="https://s3.example",
        s3_bucket="coser",
    )
    assert (
        public_object_url(settings, "uploads/2026/09/abc.jpg")
        == "https://cdn.icoser.de/uploads/2026/09/abc.jpg"
    )


@pytest.mark.asyncio
async def test_site_login_lists_cosers(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = save_coser_settings(
        {"site_url": "https://icoser.de", "site_email": "a@b.c", "site_password": "Pw123456"}
    )
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, **kwargs):
            calls.append(path)
            if path.endswith("/auth/login"):
                return FakeResponse(200, {"access_token": "tok", "token_type": "bearer"})
            raise AssertionError(path)

        async def get(self, path, **kwargs):
            del kwargs
            calls.append(path)
            return FakeResponse(200, {"data": [{"id": 1, "name": "Mika"}]})

    monkeypatch.setattr(
        "backend.services.coser.site.httpx.AsyncClient", FakeClient
    )
    client = CoserSiteClient()
    token = await client.login(settings)
    assert token == "tok"
    people = await client.list_cosers(settings)
    assert people[0]["name"] == "Mika"
    assert not any("uploads/image" in item for item in calls)


def test_direct_s3_upload(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = save_coser_settings(
        {
            "s3_endpoint": "https://s3.example",
            "s3_access_key": "AKIA",
            "s3_secret_key": "secret",
            "s3_bucket": "coser",
            "s3_public_url": "https://cdn1.hxsl.org",
            "s3_prefix": "uploads",
        }
    )
    assert settings.s3_prefix == "coser"
    put: dict[str, object] = {}

    class FakeS3:
        def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
            put.update(
                {
                    "Filename": Filename,
                    "Bucket": Bucket,
                    "Key": Key,
                    "ExtraArgs": ExtraArgs,
                    "Body": Path(Filename).read_bytes(),
                }
            )

    monkeypatch.setattr("backend.services.coser.s3._client", lambda _settings: FakeS3())
    image = tmp_path / "001.avif"
    image.write_bytes(b"avif-bytes")
    url = upload_file(settings, image, "coser/rioko/538rem_cosplay/001.avif")
    assert put["Bucket"] == "coser"
    assert put["Key"] == "coser/rioko/538rem_cosplay/001.avif"
    assert put["Body"] == b"avif-bytes"
    assert url == "https://cdn1.hxsl.org/coser/rioko/538rem_cosplay/001.avif"


@pytest.mark.asyncio
async def test_coser_runner_publishes_to_site_and_clouds(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = save_coser_settings(
        {
            "site_url": "https://icoser.de",
            "site_email": "a@b.c",
            "site_password": "Pw123456",
            "cleanup_after_publish": False,
        }
    )
    dirs = coser_dirs(settings)
    source = dirs.inbox / "job" / "archives" / "set.zip"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("photo.jpg", b"img")
    image = dirs.inbox / "job" / "images" / "01.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"cover")
    job = new_job(
        settings,
        title="测试套图",
        archives=[{"path": str(source.relative_to(dirs.root)), "name": source.name, "size": 1}],
        images=[{"path": str(image.relative_to(dirs.root)), "name": image.name, "size": 5}],
    )
    runner = CoserJobRunner()

    async def fake_pull(*_args, **_kwargs):
        return {}

    def fake_sync_gallery(_settings, **kwargs):
        work_id = int(kwargs["work_id"])
        name = str(kwargs["coser_name"])
        url = f"https://cdn1.hxsl.org/coser/{name}/{work_id}test/001.avif"
        return GalleryResult(
            urls=[url],
            cover=url,
            keys=[f"coser/{name}/{work_id}test/001.avif"],
            uploaded=True,
            reason="upload",
            work_id=work_id,
            folder=f"coser/{name}/{work_id}test",
        )

    created: dict[str, object] = {}
    updated: dict[str, object] = {}

    async def fake_list_cosers(_settings, query=""):
        del query
        return [{"id": 9, "name": "Mika"}]

    async def fake_list_works(_settings, **_kwargs):
        return []

    async def fake_get_work(_settings, work_id):
        raise AssertionError(f"unexpected get_work {work_id}")

    async def fake_create_work(_settings, payload):
        created.update(payload)
        assert payload["coser_id"] == 9
        assert payload.get("images") == []
        return {"id": 88, "title": payload["title"]}

    async def fake_update_work(_settings, work_id, payload):
        updated.update(payload)
        assert work_id == 88
        assert payload["images"]
        assert payload["download_links"]
        assert payload["cover_image"] == payload["images"][0]
        return {"id": 88, "cover_image": payload["images"][0]}

    async def fake_upload_targets(*_args, **_kwargs):
        return {
            "links": {
                "baidu": "https://pan.baidu.com/s/test",
                "pikpak": "https://mypikpak.com/s/test",
            },
            "errors": {},
            "share_pwd": "abcd",
        }

    def fake_extract(_source, target, _passwords):
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / "photo.jpg").write_bytes(b"img")

    monkeypatch.setattr("backend.services.coser.worker.pull_telegram_post", fake_pull)
    monkeypatch.setattr("backend.services.coser.worker.extract_tree", fake_extract)
    monkeypatch.setattr("backend.services.coser.worker.strip_ads", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.services.coser.worker.pack_7z_parts",
        lambda _source, dest, *_a, **_k: _write_pack(dest),
    )
    monkeypatch.setattr("backend.services.coser.worker.upload_targets", fake_upload_targets)
    monkeypatch.setattr("backend.services.coser.worker.apate_available", lambda _s: True)
    monkeypatch.setattr("backend.services.coser.worker.sync_gallery", fake_sync_gallery)
    runner.site.list_cosers = fake_list_cosers
    runner.site.list_works = fake_list_works
    runner.site.get_work = fake_get_work
    runner.site.create_work = fake_create_work
    runner.site.update_work = fake_update_work

    started = await runner.start_publish(
        settings,
        job_id=job["id"],
        payload={"coser_id": 9, "title": "测试套图", "apate": False},
    )
    result = await runner.wait_for_job(str(started["id"]))
    assert started["id"] == job["id"]
    assert result["stage"] == "done"
    assert result["site_work_id"] == 88
    assert result["public_url"] == "https://icoser.de/zh/works/88"
    assert result["links"]["baidu"]
    assert created["status"] == "pending"
    assert updated["status"] == "approved"
    assert result["image_urls"][0].endswith("88test/001.avif")


@pytest.mark.asyncio
async def test_coser_runner_reuses_existing_work_without_reupload(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    settings = save_coser_settings(
        {
            "site_url": "https://icoser.de",
            "site_email": "a@b.c",
            "site_password": "Pw123456",
            "cleanup_after_publish": False,
        }
    )
    dirs = coser_dirs(settings)
    source = dirs.inbox / "job" / "archives" / "set.zip"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("photo.jpg", b"img")
    image = dirs.inbox / "job" / "images" / "01.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"cover")
    existing_url = "https://cdn1.hxsl.org/coser/rioko/538rem_cosplay/001.avif"
    job = new_job(
        settings,
        title="rem_cosplay",
        site_work_id=538,
        coser_id=3,
        coser_name="rioko",
        archives=[{"path": str(source.relative_to(dirs.root)), "name": source.name, "size": 1}],
        images=[{"path": str(image.relative_to(dirs.root)), "name": image.name, "size": 5}],
    )
    runner = CoserJobRunner()
    created = {"count": 0}

    def fake_sync_gallery(_settings, **kwargs):
        assert int(kwargs["work_id"]) == 538
        return GalleryResult(
            urls=[existing_url],
            cover=existing_url,
            keys=["coser/rioko/538rem_cosplay/001.avif"],
            uploaded=False,
            reason="reuse",
            work_id=538,
            folder="coser/rioko/538rem_cosplay",
            skipped=True,
        )

    async def fake_get_work(_settings, work_id):
        assert work_id == 538
        return {
            "id": 538,
            "title": "rem_cosplay",
            "images": [existing_url],
            "cover_image": existing_url,
        }

    async def fake_create_work(_settings, payload):
        del payload
        created["count"] += 1
        raise AssertionError("should not create a duplicate work")

    async def fake_update_work(_settings, work_id, payload):
        assert work_id == 538
        assert payload["images"] == [existing_url]
        assert payload["cover_image"] == existing_url
        return {"id": 538, "cover_image": existing_url}

    async def fake_upload_targets(*_args, **_kwargs):
        return {
            "links": {"pikpak": "https://mypikpak.com/s/new"},
            "errors": {},
            "share_pwd": "zzzz",
        }

    def fake_extract(_source, target, _passwords):
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / "photo.jpg").write_bytes(b"img")

    monkeypatch.setattr("backend.services.coser.worker.extract_tree", fake_extract)
    monkeypatch.setattr("backend.services.coser.worker.strip_ads", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.services.coser.worker.pack_7z_parts",
        lambda _source, dest, *_a, **_k: _write_pack(dest),
    )
    monkeypatch.setattr("backend.services.coser.worker.upload_targets", fake_upload_targets)
    monkeypatch.setattr("backend.services.coser.worker.apate_available", lambda _s: True)
    monkeypatch.setattr("backend.services.coser.worker.sync_gallery", fake_sync_gallery)
    runner.site.get_work = fake_get_work
    runner.site.create_work = fake_create_work
    runner.site.update_work = fake_update_work

    started = await runner.start_publish(
        settings,
        job_id=job["id"],
        payload={
            "coser_id": 3,
            "coser_name": "rioko",
            "title": "rem_cosplay",
            "apate": False,
        },
    )
    result = await runner.wait_for_job(str(started["id"]))
    assert result["stage"] == "done"
    assert result["site_work_id"] == 538
    assert result["gallery_skipped"] is True
    assert result["links"]["pikpak"]
    assert created["count"] == 0


def _write_pack(dest: Path) -> list[Path]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"7z")
    return [dest]
