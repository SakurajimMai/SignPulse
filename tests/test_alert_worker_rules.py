from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import backend.services.games.keepalive as games_keepalive
import backend.services.games.worker as games_worker
import backend.services.manga.ehentai.worker as ehentai_worker
import backend.services.manga.hmw.worker as hmw_worker
from backend.services.games.config import GamesSettings
from backend.services.games.paths import games_dirs, relative_to_games
from backend.services.games.worker import GamesCloudUploadError, GamesJobRunner, new_job
from backend.services.manga.config import MangaSettings
from backend.services.manga.ehentai.parser import GalleryMeta, GalleryRef
from backend.services.manga.ehentai.worker import EhentaiWorker
from backend.services.manga.hmw.paths import hmw_dirs
from backend.services.manga.hmw.worker import HmwJobRunner, HmwJobSnapshot
from backend.services.manga.publisher import PublishedChapter


def _capture_alerts(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def capture(rule_id: str, **kwargs: Any) -> None:
        calls.append({"rule_id": rule_id, **kwargs})

    monkeypatch.setattr(module, "schedule_alert", capture)
    return calls


def _games_settings(tmp_path: Path) -> GamesSettings:
    return GamesSettings(data_dir=str(tmp_path), cleanup_after_publish=False)


def _packed_job(settings: GamesSettings, **extra: Any) -> dict[str, Any]:
    packed = games_dirs(settings).packed / "alert-job" / "game.7z"
    packed.parent.mkdir(parents=True, exist_ok=True)
    packed.write_bytes(b"packed")
    return new_job(
        settings,
        title="告警测试游戏",
        source_key="channel:m:42",
        source_url="https://t.me/channel/42",
        packed_parts=[
            {
                "path": relative_to_games(settings, packed),
                "name": packed.name,
                "size": packed.stat().st_size,
            }
        ],
        **extra,
    )


def test_cloud_keepalive_distinguishes_auth_from_transport_failures() -> None:
    assert games_keepalive._is_cloud_auth_failure(
        RuntimeError("refresh token invalid; login failed")
    )
    assert games_keepalive._is_cloud_auth_failure(
        RuntimeError("Cookie 失效，请重新填写 BDUSS")
    )
    assert not games_keepalive._is_cloud_auth_failure(
        RuntimeError("temporary failure in name resolution")
    )


@pytest.mark.parametrize(
    ("stage", "expected_rule"),
    [
        ("pulling", "games_source_pull_fail"),
        ("extracting", "games_archive_fail"),
        ("packing", "games_archive_fail"),
        ("disguise", "games_archive_fail"),
        ("publishing", "games_site_publish_fail"),
    ],
)
def test_games_pipeline_stage_selects_canonical_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_rule: str,
) -> None:
    settings = _games_settings(tmp_path)
    job = new_job(settings, source_key="channel:m:stage", title="Stage Test")
    alerts = _capture_alerts(monkeypatch, games_worker)

    games_worker._alert_pipeline_fail(job, "failed", stage=stage)

    assert [item["rule_id"] for item in alerts] == [expected_rule]


@pytest.mark.asyncio
async def test_games_manual_pull_failure_uses_source_rule_and_source_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _games_settings(tmp_path)
    runner = GamesJobRunner()
    job = new_job(
        settings,
        title="手动拉取",
        source_url="https://t.me/channel/10",
    )
    alerts = _capture_alerts(monkeypatch, games_worker)

    async def fail_pull(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("telegram unavailable")

    monkeypatch.setattr(games_worker, "pull_telegram_post", fail_pull)

    await runner._run_pull(
        settings,
        job,
        url=str(job["source_url"]),
        account="collector",
    )

    assert job["stage"] == "failed"
    assert [item["rule_id"] for item in alerts] == ["games_source_pull_fail"]
    assert alerts[0]["fingerprint"] == "https://t.me/channel/10"
    assert "telegram unavailable" in alerts[0]["detail"]


@pytest.mark.asyncio
async def test_games_manual_pull_cancel_does_not_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _games_settings(tmp_path)
    runner = GamesJobRunner()
    job = new_job(settings, source_url="https://t.me/channel/11")
    alerts = _capture_alerts(monkeypatch, games_worker)

    async def cancel_pull(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise asyncio.CancelledError()

    monkeypatch.setattr(games_worker, "pull_telegram_post", cancel_pull)

    with pytest.raises(asyncio.CancelledError):
        await runner._run_pull(
            settings,
            job,
            url=str(job["source_url"]),
            account="collector",
        )

    assert job["stage"] == "cancelled"
    assert alerts == []


@pytest.mark.asyncio
async def test_games_retry_cloud_top_level_failure_uses_cloud_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _games_settings(tmp_path)
    runner = GamesJobRunner()
    job = _packed_job(settings)
    packed = games_dirs(settings).packed / "alert-job" / "game.7z"
    alerts = _capture_alerts(monkeypatch, games_worker)

    def fake_disguise(*_args: Any, **_kwargs: Any):
        async def prepare(source: Path, _index: int, _total: int):
            return source

        return prepare, None, []

    async def fail_upload(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("quark upload failed")

    monkeypatch.setattr(runner, "_disguise_prepare", fake_disguise)
    monkeypatch.setattr(games_worker, "upload_targets", fail_upload)

    await runner._run_retry_clouds(
        settings,
        job,
        {},
        ["quark"],
        [packed],
    )

    assert job["stage"] == "failed"
    assert [item["rule_id"] for item in alerts] == ["games_cloud_fail"]
    assert alerts[0]["fingerprint"] == "channel:m:42"
    assert "quark upload failed" in alerts[0]["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["exception", "no_links"])
async def test_games_initial_cloud_failure_is_not_misclassified_as_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    settings = _games_settings(tmp_path)
    runner = GamesJobRunner()
    job = _packed_job(settings)
    alerts = _capture_alerts(monkeypatch, games_worker)

    monkeypatch.setattr(
        games_worker,
        "cloud_upload_phases",
        lambda _settings, _selected: (["quark"], []),
    )

    async def upload(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if failure_mode == "exception":
            raise RuntimeError("cloud transport down")
        return {
            "links": {},
            "errors": {"quark": "login expired"},
            "folders": {},
        }

    monkeypatch.setattr(games_worker, "upload_targets", upload)

    await runner._run_publish(settings, job, {})

    assert job["stage"] == "failed"
    assert [item["rule_id"] for item in alerts] == ["games_cloud_fail"]
    assert alerts[0]["fingerprint"] == "channel:m:42"


@pytest.mark.asyncio
async def test_games_auto_cloud_failure_uses_stable_source_key_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _games_settings(tmp_path)
    runner = GamesJobRunner()
    job = new_job(
        settings,
        source_key="channel:m:99",
        source_url="https://t.me/channel/99",
        automatic=True,
    )
    alerts = _capture_alerts(monkeypatch, games_worker)

    async def pull(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "title": "自动发布",
            "archives": [{"path": "inbox/source.zip"}],
        }

    async def cloud_failure(*_args: Any, **_kwargs: Any) -> None:
        raise GamesCloudUploadError("all cloud targets failed")

    monkeypatch.setattr(games_worker, "pull_telegram_post", pull)
    monkeypatch.setattr(games_worker, "cleanup_job_files", lambda *_args: {})
    monkeypatch.setattr(runner, "_publish", cloud_failure)

    await runner._run_auto_publish(
        settings,
        job,
        url=str(job["source_url"]),
        account="collector",
    )

    assert [item["rule_id"] for item in alerts] == ["games_cloud_fail"]
    assert alerts[0]["fingerprint"] == "channel:m:99"


class _FakeSearchHttp:
    def __init__(self, ref: GalleryRef):
        self.ref = ref

    async def search(self, *_args: Any, **_kwargs: Any) -> list[GalleryRef]:
        return [self.ref]

    async def aclose(self) -> None:
        return None


class _FakeSite:
    enabled = True

    def __init__(self, failure: BaseException | None = None, body: Any = None):
        self.failure = failure
        self.body = body

    async def publish_chapter(self, **_kwargs: Any) -> Any:
        if self.failure is not None:
            raise self.failure
        return self.body

    async def aclose(self) -> None:
        return None


class _FakeChannel:
    enabled = True

    def __init__(self, failure: BaseException | None = None, posted: bool = True):
        self.failure = failure
        self.posted = posted

    async def publish_chapter(self, **_kwargs: Any) -> bool:
        if self.failure is not None:
            raise self.failure
        return self.posted


def _ehentai_settings(tmp_path: Path) -> MangaSettings:
    return MangaSettings(
        data_dir=str(tmp_path),
        temp_dir=str(tmp_path / "tmp"),
        cfbed_upload_url="https://img.example/upload",
        site_publish_url="https://site.example/publish",
        site_publish_secret="secret",
        outbound_enabled=True,
        outbound_channel="-100123",
        ehentai_search="tag:test",
        ehentai_gallery_delay_seconds=-1,
    )


async def _prepare_routed_ehentai_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    site: _FakeSite,
    channel: _FakeChannel,
) -> tuple[EhentaiWorker, GalleryMeta, list[dict[str, Any]]]:
    ref = GalleryRef(123, "abcdef1234", "https://e-hentai.org/g/123/abcdef1234/")
    meta = GalleryMeta(ref=ref, title="Gallery")
    worker = EhentaiWorker(_ehentai_settings(tmp_path))
    await worker.site.aclose()
    worker.site = site
    worker.channel = channel
    worker._http = _FakeSearchHttp(ref)
    alerts = _capture_alerts(monkeypatch, ehentai_worker)

    async def no_existing(_source_key: str) -> int:
        return 0

    async def card(_source_key: str) -> dict[str, Any]:
        return {"title": meta.title}

    async def ingest(_ref: GalleryRef) -> None:
        await worker._outbound(
            meta,
            PublishedChapter(
                manga_id=1,
                chapter_id=2,
                slug="gallery",
                number=1,
                page_count=10,
                created=True,
            ),
            ["https://img.example/1.jpg"],
        )

    monkeypatch.setattr(worker, "_existing_page_count", no_existing)
    monkeypatch.setattr(worker, "_card_fields", card)
    monkeypatch.setattr(worker, "_ingest_gallery", ingest)
    return worker, meta, alerts


@pytest.mark.asyncio
async def test_ehentai_site_failure_uses_site_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, _meta, alerts = await _prepare_routed_ehentai_worker(
        tmp_path,
        monkeypatch,
        site=_FakeSite(failure=RuntimeError("site down")),
        channel=_FakeChannel(),
    )
    try:
        await worker._run_pass(["tag:test"])
    finally:
        await worker.stop()

    assert [item["rule_id"] for item in alerts] == ["manga_site_fail"]
    assert alerts[0]["fingerprint"] == "eh:123/abcdef1234"
    assert "site down" in alerts[0]["detail"]


@pytest.mark.asyncio
async def test_ehentai_outbound_failure_uses_channel_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, _meta, alerts = await _prepare_routed_ehentai_worker(
        tmp_path,
        monkeypatch,
        site=_FakeSite(body={"ok": True}),
        channel=_FakeChannel(failure=RuntimeError("telegram down")),
    )
    try:
        await worker._run_pass(["tag:test"])
    finally:
        await worker.stop()

    assert [item["rule_id"] for item in alerts] == [
        "manga_channel_publish_fail"
    ]
    assert alerts[0]["fingerprint"] == "eh:123/abcdef1234"
    assert "telegram down" in alerts[0]["detail"]


@pytest.mark.asyncio
async def test_ehentai_routed_cancel_does_not_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, _meta, alerts = await _prepare_routed_ehentai_worker(
        tmp_path,
        monkeypatch,
        site=_FakeSite(failure=asyncio.CancelledError()),
        channel=_FakeChannel(),
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await worker._run_pass(["tag:test"])
    finally:
        await worker.stop()

    assert alerts == []


@pytest.mark.asyncio
async def test_ehentai_no_written_pages_alerts_crawl_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = GalleryRef(456, "abcdef5678", "https://e-hentai.org/g/456/abcdef5678/")
    meta = GalleryMeta(
        ref=ref,
        title="Missing pages",
        page_count=1,
        image_pages=["https://e-hentai.org/s/deadbeef00/456-1"],
    )

    class MissingPageHttp(_FakeSearchHttp):
        async def fetch_gallery(self, _ref: GalleryRef) -> GalleryMeta:
            return meta

        async def fetch_showpage(self, _url: str) -> None:
            return None

    worker = EhentaiWorker(_ehentai_settings(tmp_path))
    worker._http = MissingPageHttp(ref)
    alerts = _capture_alerts(monkeypatch, ehentai_worker)

    async def no_existing(_source_key: str) -> int:
        return 0

    monkeypatch.setattr(worker, "_existing_page_count", no_existing)
    try:
        await worker._ingest_gallery(ref)
    finally:
        await worker.stop()

    assert worker.stats["failed"] == 1
    assert [item["rule_id"] for item in alerts] == [
        "manga_ehentai_gallery_fail"
    ]
    assert alerts[0]["fingerprint"] == "eh:456/abcdef5678"
    assert "没有写入页" in alerts[0]["detail"]


class _FakeHmwApi:
    def __init__(self, failure: BaseException):
        self.failure = failure

    async def __aenter__(self):
        raise self.failure

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeHmwStorage:
    def cover_key(self, slug: str) -> str:
        return f"covers/{slug}"

    def public_url(self, key: str) -> str:
        return f"https://cdn.example/{key}"


async def _run_hmw_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> tuple[HmwJobRunner, list[dict[str, Any]], BaseException | None]:
    settings = MangaSettings(data_dir=str(tmp_path), temp_dir=str(tmp_path / "tmp"))
    source_root = hmw_dirs(settings).inbox / "stable-series"
    source_root.mkdir(parents=True, exist_ok=True)
    source = SimpleNamespace(
        root=source_root,
        cover=None,
        chapters=[
            SimpleNamespace(
                action="upsert",
                number=1.0,
                title="第一话",
                directory=source_root,
                images=[],
            )
        ],
    )
    cfg = SimpleNamespace(convert_workers=1)

    class FailingWorkflow:
        def __init__(self, *_args: Any, **_kwargs: Any):
            pass

        async def execute(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("API context failure must happen before execute")

    import backend.services.manga.hmw.lib.discovery as discovery

    monkeypatch.setattr(discovery, "discover_source", lambda _root: source)
    monkeypatch.setattr(
        hmw_worker,
        "make_clients",
        lambda _settings: (cfg, _FakeHmwApi(failure), _FakeHmwStorage()),
    )
    monkeypatch.setattr(
        hmw_worker,
        "apply_chapter_overrides",
        lambda current, *_args, **_kwargs: current,
    )
    monkeypatch.setattr(
        hmw_worker,
        "build_manga_payload",
        lambda *_args, **_kwargs: {
            "slug": "stable-series",
            "title": "Stable Series",
            "language": "zh",
            "cover_url": "https://cdn.example/cover.jpg",
        },
    )
    monkeypatch.setattr(hmw_worker, "PanelPublishWorkflow", FailingWorkflow)
    alerts = _capture_alerts(monkeypatch, hmw_worker)
    runner = HmwJobRunner()
    runner.snapshot = HmwJobSnapshot(
        task_id="temporary-task-id",
        running=True,
        stage="queued",
        source_path="inbox/stable-series",
        title="Stable Series",
    )
    caught: BaseException | None = None
    try:
        await runner._run(
            settings,
            source_path="inbox/stable-series",
            manga={"slug": "stable-series", "title": "Stable Series"},
            chapters=None,
            cover_path=None,
            task_id="temporary-task-id",
            genre_ids=None,
        )
    except BaseException as exc:
        caught = exc
    return runner, alerts, caught


@pytest.mark.asyncio
async def test_hmw_failure_fingerprint_prefers_source_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, alerts, caught = await _run_hmw_failure(
        tmp_path,
        monkeypatch,
        RuntimeError("publisher down"),
    )

    assert caught is None
    assert runner.snapshot.error == "publisher down"
    assert [item["rule_id"] for item in alerts] == ["manga_hmw_source_fail"]
    assert alerts[0]["fingerprint"] == "inbox/stable-series"


@pytest.mark.asyncio
async def test_hmw_cancel_does_not_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _runner, alerts, caught = await _run_hmw_failure(
        tmp_path,
        monkeypatch,
        asyncio.CancelledError(),
    )

    assert isinstance(caught, asyncio.CancelledError)
    assert alerts == []
