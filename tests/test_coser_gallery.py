from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from backend.services.coser.avif import avif_is_valid, convert_to_avif
from backend.services.coser.catalog import find_existing_entry, upsert_entry
from backend.services.coser.config import load_coser_settings, save_coser_settings
from backend.services.coser.gallery import (
    collect_source_images,
    decide_gallery,
    sync_gallery,
)
from backend.services.coser.keys import (
    gallery_folder,
    object_key,
    path_is_unsafe,
    sanitize_path_segment,
    titles_match,
)


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("COSER_CONFIG_FILE", str(tmp_path / ".coser_config.json"))
    monkeypatch.setenv("COSER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GAMES_CONFIG_FILE", str(tmp_path / ".games_config.json"))
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    return tmp_path


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (12, 64, 200)).save(path, "PNG")
    return path


def test_collect_source_images_allows_hidden_ancestor(tmp_path: Path):
    root = tmp_path / ".cache" / "source"
    image = _png(root / "01.png")

    assert collect_source_images(root) == [image]


def test_collect_source_images_skips_hidden_children(tmp_path: Path):
    root = tmp_path / "source"
    visible = _png(root / "01.png")
    _png(root / ".thumbs" / "02.png")
    _png(root / "__MACOSX" / "03.png")
    _png(root / ".04.png")

    assert collect_source_images(root) == [visible]


class MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        del Bucket, ExtraArgs
        self.objects[Key] = Path(Filename).read_bytes()

    def head_object(self, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise FileNotFoundError(Key)
        return {}

    def list_objects_v2(self, **kwargs):
        prefix = str(kwargs.get("Prefix") or "")
        contents = [{"Key": key} for key in self.objects if key.startswith(prefix)]
        return {"Contents": contents, "IsTruncated": False}

    def head_bucket(self, Bucket):
        del Bucket
        return {}


def test_sanitize_title_strips_unsafe_chars_and_spaces():
    assert sanitize_path_segment("Rem (Cosplay)") == "remcosplay"
    assert sanitize_path_segment("rem_cosplay") == "rem_cosplay"
    assert sanitize_path_segment("A+B#C%D[E]F【G】H（I）J") == "abcdefghij"
    assert sanitize_path_segment("hello world") == "helloworld"
    assert path_is_unsafe("coser/rioko/538Rem (Cosplay)/001.avif")
    assert not path_is_unsafe("coser/rioko/538rem_cosplay/001.avif")
    assert titles_match("Rem (Cosplay)", "Rem(Cosplay)")


def test_object_key_matches_hxsl_template():
    settings = SimpleNamespace(s3_prefix="coser")
    assert (
        gallery_folder(settings, coser_name="Rioko", work_id=538, title="rem_cosplay")
        == "coser/rioko/538rem_cosplay"
    )
    assert (
        object_key(
            settings,
            coser_name="rioko",
            work_id=538,
            title="rem_cosplay",
            index=1,
            total=12,
        )
        == "coser/rioko/538rem_cosplay/001.avif"
    )


def _decision(**overrides):
    expected = [
        "https://cdn1.hxsl.org/coser/rioko/538rem_cosplay/001.avif",
        "https://cdn1.hxsl.org/coser/rioko/538rem_cosplay/002.avif",
    ]
    kwargs = {
        "expected_count": 2,
        "expected_urls": expected,
        "manifest": {
            "status": "success",
            "expected_count": 2,
            "images": [{"url": url} for url in expected],
        },
        "cdn_ok": [True, True],
        "cdn_listed_count": 2,
        "site_images": list(expected),
        "local_avif_ok": [True, True],
        "existing_urls": list(expected),
    }
    kwargs.update(overrides)
    return decide_gallery(**kwargs)


def test_decide_skips_complete_success():
    decision = _decision()
    assert decision.upload is False
    assert decision.update_site_images is False
    assert decision.reason == "reuse"


def test_decide_reruns_when_upload_json_failed_and_cdn_short():
    decision = _decision(
        manifest={"status": "failed", "expected_count": 2, "images": []},
        cdn_ok=[True, False],
        cdn_listed_count=1,
    )
    assert decision.upload is True
    assert "upload_json_failed" in decision.reasons
    assert "count_short" in decision.reasons


def test_decide_reruns_when_path_has_special_chars():
    unsafe = [
        "https://cdn1.hxsl.org/coser/rioko/538Rem (Cosplay)/001.avif",
        "https://cdn1.hxsl.org/coser/rioko/538Rem (Cosplay)/002.avif",
    ]
    decision = _decision(
        manifest={"status": "success", "expected_count": 2, "images": [{"url": u} for u in unsafe]},
        site_images=unsafe,
        existing_urls=unsafe,
        cdn_ok=[False, False],
        cdn_listed_count=0,
    )
    assert decision.upload is True
    assert decision.reason == "unsafe_path"


def test_decide_patches_site_without_reupload_when_cdn_ok():
    expected = [
        "https://cdn1.hxsl.org/coser/rioko/538rem_cosplay/001.avif",
        "https://cdn1.hxsl.org/coser/rioko/538rem_cosplay/002.avif",
    ]
    decision = _decision(
        site_images=[
            "https://cdn1.hxsl.org/coser/rioko/538Rem (Cosplay)/001.avif",
            "https://cdn1.hxsl.org/coser/rioko/538Rem (Cosplay)/002.avif",
        ],
        existing_urls=[
            "https://cdn1.hxsl.org/coser/rioko/538Rem (Cosplay)/001.avif",
        ],
        expected_urls=expected,
        cdn_ok=[True, True],
        cdn_listed_count=2,
    )
    assert decision.upload is False
    assert decision.update_site_images is True
    assert decision.reason == "site_urls"


def test_convert_avif_roundtrip(tmp_path: Path):
    source = _png(tmp_path / "cover.png")
    dest = tmp_path / "001.avif"
    convert_to_avif(source, dest)
    assert avif_is_valid(dest)


def test_sync_gallery_uploads_then_skips_rerun(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = save_coser_settings(
        {
            "s3_endpoint": "https://s3.example",
            "s3_access_key": "AKIA",
            "s3_secret_key": "secret",
            "s3_bucket": "coser",
            "s3_public_url": "https://cdn1.hxsl.org",
            "s3_prefix": "coser",
        }
    )
    store = MemoryS3()
    monkeypatch.setattr("backend.services.coser.s3._client", lambda _settings: store)
    monkeypatch.setattr("backend.services.coser.gallery.verify_public_url", lambda _url, timeout=15.0: True)
    monkeypatch.setattr("backend.services.coser.s3.verify_public_url", lambda _url, timeout=15.0: True)
    sources = [
        _png(tmp_path / "src" / "a.png"),
        _png(tmp_path / "src" / "b.png"),
    ]
    first = sync_gallery(
        settings,
        sources=sources,
        work_id=538,
        title="rem_cosplay",
        coser_name="rioko",
        site_images=[],
    )
    assert first.uploaded is True
    assert first.urls[0] == "https://cdn1.hxsl.org/coser/rioko/538rem_cosplay/001.avif"
    assert first.urls[1].endswith("002.avif")
    assert "coser/rioko/538rem_cosplay/001.avif" in store.objects
    assert first.manifest["status"] == "success"

    second = sync_gallery(
        settings,
        sources=sources,
        work_id=538,
        title="rem_cosplay",
        coser_name="rioko",
        site_images=first.urls,
    )
    assert second.uploaded is False
    assert second.skipped is True
    assert second.reason == "reuse"
    assert second.urls == first.urls


def test_catalog_finds_existing_coser_work(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_coser_settings()
    upsert_entry(
        settings,
        {
            "id": 538,
            "title": "Rem (Cosplay)",
            "coser_id": 3,
            "coser_name": "rioko",
            "job_id": "abc",
        },
    )
    found = find_existing_entry(
        settings, coser_id=3, title="Rem(Cosplay)", source_url=""
    )
    assert found is not None
    assert found["id"] == 538
