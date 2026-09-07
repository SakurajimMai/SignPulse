from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.services.games.archive import (
    extract_tree,
    pack_7z,
    pack_7z_parts,
    strip_ads,
)
from backend.services.games.automation import GamesAutomationService, _default_state
from backend.services.games.catalog import (
    catalog_has_source,
    list_catalog,
    upsert_entry,
)
from backend.services.games.clouds import (
    CloudError,
    _baidu_ensure_folder,
    _baidu_normalize_share_url,
    _baidu_share,
    _baidu_token,
    _check_openlist,
    _cookie_dict,
    _extract_js_token,
    _pikpak_gcid,
    _pikpak_share,
    _quark_share,
    _share_extract_code,
    _terabox_normalize_share_url,
    _terabox_share_payloads,
    _with_share_pwd,
    baidu_title_folder,
    cloud_upload_phases,
    normalize_upload_targets,
    persist_client_cookies,
    random_share_code,
    share_pwd_from_links,
    target_base_dir,
    target_folder_name,
    target_remote_path,
    upload_targets,
)
from backend.services.games.config import (
    games_config_public,
    load_games_settings,
    save_games_settings,
)
from backend.services.games.keepalive import refresh_cloud_sessions
from backend.services.games.parser import (
    clean_game_summary,
    parse_game_caption,
    suggest_categories,
)
from backend.services.games.paths import games_dirs, relative_to_games
from backend.services.games.telegram import (
    DOWNLOAD_SPACE_RESERVE_MAX_BYTES,
    GamesTelegramError,
    _ensure_download_space,
    _is_archive,
    _is_archive_member,
    _is_preview_video,
    _is_primary_archive,
    _visual_download,
    group_channel_history,
    pull_telegram_post,
    telegram_file_links,
)
from backend.services.games.wordpress import (
    build_pay_extra_hide,
    build_post_html,
    build_zibpay_meta,
    extract_image_urls,
    normalize_pay_modo,
    source_post_slug,
)
from backend.services.games.worker import (
    GamesJobRunner,
    finalize_published_job,
    new_job,
)
from backend.services.manga.hmw.telegram_link import parse_telegram_post_url
from tg_signer.security import is_encrypted_secret


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GAMES_CONFIG_FILE", str(tmp_path / ".games_config.json"))
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    return tmp_path


def test_games_secrets_encrypted(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    saved = save_games_settings(
        {
            "wp_user": "sakura",
            "wp_app_password": "app-pass-one",
            "pack_password": "sakuramai",
            "baidu_cookie": "BDUSS=cookie",
        }
    )
    assert saved.wp_app_password == "app-pass-one"
    raw = json.loads((tmp_path / ".games_config.json").read_text(encoding="utf-8"))
    assert "app-pass-one" not in json.dumps(raw)
    assert is_encrypted_secret(raw["wp_app_password"])
    public = games_config_public(saved)
    assert public["wp_app_password"] is None
    assert public["wp_app_password_set"] is True
    revealed = games_config_public(saved, reveal=True)
    assert revealed["wp_app_password"] == "app-pass-one"
    assert revealed["baidu_cookie"] == "BDUSS=cookie"
    assert public["pack_password"] == "sakuramai"
    assert public["pack_password_set"] is True
    assert public["extract_passwords"]
    assert "********" not in str(public["pack_password"])
    again = save_games_settings({"wp_user": "sakura2"})
    assert again.wp_app_password == "app-pass-one"
    assert load_games_settings().pack_password == "sakuramai"


def test_games_auto_publish_settings_are_normalized(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    saved = save_games_settings(
        {
            "telegram_source_channels": "@alpha\nbeta,alpha",
            "auto_publish_enabled": True,
            "telegram_poll_seconds": 1,
            "telegram_backfill_limit": 999,
            "auto_retry_limit": 0,
            "split_volume_mb": 9999,
            "baidu_remote_dir": "%2F发布%2F百度",
            "pikpak_remote_dir": "/发布/PikPak",
            "terabox_remote_dir": "%2Fwebsite%2Fixacg.top%2Fgame",
            "quark_remote_dir": "%2F网站%2F夸克",
        }
    )
    assert saved.source_channel_list == ["@alpha", "beta", "alpha"]
    assert saved.auto_publish_enabled is True
    assert saved.telegram_poll_seconds == 10
    assert saved.telegram_backfill_limit == 100
    assert saved.auto_retry_limit == 1
    assert saved.split_volume_mb == 4096
    legacy = save_games_settings({"split_volume_mb": 3800})
    assert legacy.split_volume_mb == 4096
    assert saved.baidu_remote_dir == "/发布/百度"
    assert saved.pikpak_remote_dir == "/发布/PikPak"
    assert saved.terabox_remote_dir == "/website/ixacg.top/game"
    assert saved.quark_remote_dir == "/网站/夸克"
    public = games_config_public(saved)
    assert public["auto_publish_enabled"] is True
    assert public["telegram_source_channels"] == "@alpha,beta,alpha"


def test_games_pay_template_and_vip_prices_persist(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    saved = save_games_settings(
        {
            "wp_vip1_price": 3,
            "wp_vip2_price": 0,
            "wp_vip1_points": 10,
            "wp_vip2_points": None,
            "wp_pay_extra_template": "解压 {pack_password}\n提取 {extract}",
            "wp_apate_url": "ixacg.top/16403.html",
        }
    )
    assert saved.wp_vip1_price == 3
    assert saved.wp_vip2_price == 0
    assert saved.wp_vip1_points == 10
    assert saved.wp_vip2_points is None
    assert "{extract}" in saved.wp_pay_extra_template
    assert saved.wp_apate_url == "https://ixacg.top/16403.html"
    cleared = save_games_settings({"wp_vip1_price": None, "wp_vip2_price": 0})
    assert cleared.wp_vip1_price is None
    assert cleared.wp_vip2_price == 0


def test_pikpak_gcid_is_stable(tmp_path: Path):
    sample = tmp_path / "probe.txt"
    sample.write_bytes(b"hello pikpak")
    digest = _pikpak_gcid(sample)
    assert len(digest) == 40
    assert digest == _pikpak_gcid(sample)
    assert digest == digest.upper()


def test_openlist_cloudflare_html_error_is_sanitized():
    import httpx

    body = (
        "upstream proxy said: <!DOCTYPE html>"
        '<html><head><title>example.invalid | 520: Web server is returning an unknown error</title>'
        '</head><body class="oldie">failed</body></html>'
    )
    response = httpx.Response(520, text=body)

    with pytest.raises(CloudError) as raised:
        _check_openlist(response, "上传")

    message = str(raised.value)
    assert "520" in message
    assert "<!DOCTYPE" not in message
    assert "oldie" not in message
    assert "example.invalid" not in message


def test_cloud_cookie_and_share_helpers():
    assert _cookie_dict("abc123", "BDUSS") == {"BDUSS": "abc123"}
    parsed = _cookie_dict("ndus=token; lang=en", "ndus")
    assert parsed["ndus"] == "token"
    assert _share_extract_code("sakuramai") == "saku"
    generated = _share_extract_code("!!")
    assert len(generated) == 4 and generated.isalnum()
    assert _with_share_pwd("https://pan.baidu.com/s/1x", "saku").endswith("?pwd=saku")
    assert _with_share_pwd("https://pan.baidu.com/s/1x?pwd=abcd", "saku").endswith("pwd=saku")
    first = random_share_code()
    assert len(first) == 4 and first.isalnum()
    assert share_pwd_from_links({"baidu": "https://pan.baidu.com/s/1x?pwd=k7m2"}) == "k7m2"


@pytest.mark.asyncio
async def test_baidu_token_reads_template_variable_not_home():
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "gettemplatevariable" in str(request.url):
            return httpx.Response(
                200,
                json={"errno": 0, "result": {"bdstoken": "token-from-api"}},
            )
        raise AssertionError(f"unexpected {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        token = await _baidu_token(client)
    assert token == "token-from-api"
    assert seen and "gettemplatevariable" in seen[0]
    assert all("/disk/home" not in url for url in seen)


@pytest.mark.asyncio
async def test_pikpak_share_keeps_requested_password():
    import httpx

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "share_id": "VP-abc",
                "share_url": "https://mypikpak.com/s/VP-abc?pwd=mrva",
                "pass_code": "mrva",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        url = await _pikpak_share(client, {"Authorization": "Bearer x"}, "fid-1", "efdx")
    assert bodies[0]["pass_code"] == "efdx"
    assert bodies[0]["pass_code_option"] == "REQUIRED"
    assert url.endswith("pwd=mrva")


def test_persist_client_cookies_keeps_old_and_writes_new(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    original = "__puus=old-token; lang=zh"
    save_games_settings({"quark_cookie": original})

    class _Jar:
        def items(self):
            return [(" __puus".strip(), "rotated-token"), ("tfstk", "abc")]

    class _Client:
        cookies = _Jar()

    assert persist_client_cookies("quark_cookie", original, "__puus", _Client())
    parsed = _cookie_dict(load_games_settings().quark_cookie, "__puus")
    assert parsed["__puus"] == "rotated-token"
    assert parsed["lang"] == "zh"
    assert parsed["tfstk"] == "abc"
    assert persist_client_cookies("quark_cookie", load_games_settings().quark_cookie, "__puus", _Client()) is False


def test_persist_baidu_cookies_keep_stoken(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    original = (
        "BDUSS=longbdussvalue1234567890; "
        "STOKEN=longstokenvalue1234567890; "
        "PANPSC=oldpanpscvalue1234567890"
    )
    save_games_settings({"baidu_cookie": original})

    class _Jar:
        def items(self):
            return [("BDUSS", "x"), ("csrfToken", "abc")]

    class _Client:
        cookies = _Jar()

    assert persist_client_cookies("baidu_cookie", original, "BDUSS", _Client())
    parsed = _cookie_dict(load_games_settings().baidu_cookie, "BDUSS")
    assert parsed["BDUSS"].startswith("longbduss")
    assert parsed["STOKEN"].startswith("longstoken")
    assert parsed["PANPSC"].startswith("oldpanpsc")
    assert parsed["csrfToken"] == "abc"


@pytest.mark.asyncio
async def test_refresh_cloud_sessions_skips_unconfigured(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    state = await refresh_cloud_sessions()
    assert state["interval_hours"] == 6
    assert state["last_run_at"]
    assert state["results"]["baidu"]["skipped"] is True
    assert state["results"]["quark"]["skipped"] is True


@pytest.mark.asyncio
async def test_refresh_cloud_sessions_sanitizes_baidu_html(
    tmp_path: Path, monkeypatch
):
    import httpx

    _isolate(tmp_path, monkeypatch)
    save_games_settings({"baidu_cookie": "BDUSS=test-cookie"})
    body = (
        "gateway prefix: <!DOCTYPE html>"
        '<html><head><title>pan.example | 520: Web server is returning an unknown error</title>'
        '</head><body class="oldie">failed</body></html>'
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr("backend.services.games.keepalive.httpx.AsyncClient", fake_client)
    state = await refresh_cloud_sessions()
    message = state["results"]["baidu"]["message"]

    assert state["results"]["baidu"]["ok"] is False
    assert "520" in message
    assert "<!DOCTYPE" not in message
    assert "oldie" not in message
    assert "pan.example" not in message


@pytest.mark.asyncio
async def test_refresh_cloud_sessions_updates_quark_cookie(tmp_path: Path, monkeypatch):
    import httpx

    _isolate(tmp_path, monkeypatch)
    save_games_settings({"quark_cookie": "__puus=old-token"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "status": 200, "data": {"list": []}},
            headers={"Set-Cookie": "__puus=new-token; Path=/"},
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr("backend.services.games.keepalive.httpx.AsyncClient", fake_client)
    state = await refresh_cloud_sessions()
    assert state["results"]["quark"]["ok"] is True
    assert _cookie_dict(load_games_settings().quark_cookie, "__puus")["__puus"] == "new-token"


def test_terabox_share_uses_path_list_not_just_fid():
    payloads = _terabox_share_payloads("/website/ixacg.top/game/demo", 384013637417221, "saku")
    assert payloads
    assert all("path_list" in item for item in payloads)
    first = payloads[0]
    assert first["path_list"] == '["/website/ixacg.top/game/demo"]'
    assert first["schannel"] == "4"
    assert first["pwd"] == "saku"
    assert "fid_list" not in first
    assert any(item.get("fid_list") == "[384013637417221]" for item in payloads)


def test_terabox_share_url_and_js_token_helpers():
    assert (
        _terabox_normalize_share_url({"shorturl": "1AbCdEf"}, "saku")
        == "https://www.terabox.com/s/1AbCdEf?pwd=saku"
    )
    assert _terabox_normalize_share_url(
        {"link": "https://www.terabox.com/s/1AbCdEf"}
    ).startswith("https://")
    html = (
        '<script>var templateData = {"bdstoken":"tok123",'
        '"jsToken":"fn%28%22ABCDEF0123%22%29"};</script>'
    )
    assert _extract_js_token(html) == "ABCDEF0123"


@pytest.mark.asyncio
async def test_quark_share_uses_password_and_task():
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/share") and request.method == "POST":
            return httpx.Response(200, json={"code": 0, "status": 200, "data": {"task_id": "t1"}})
        if request.url.path.endswith("/task"):
            return httpx.Response(
                200, json={"code": 0, "status": 200, "data": {"share_id": "s1", "status": 2}}
            )
        if request.url.path.endswith("/share/password"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "status": 200,
                    "data": {
                        "share_url": "https://pan.quark.cn/s/abc123",
                        "passcode": "k7m2",
                    },
                },
            )
        return httpx.Response(404, json={"code": 404, "message": request.url.path})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        client._quark_base = "https://drive-pc.quark.cn/1/clouddrive"  # type: ignore[attr-defined]
        url = await _quark_share(client, "fid-1", "测试游戏", "k7m2")
    assert url == "https://pan.quark.cn/s/abc123?pwd=k7m2"
    assert any(item.endswith("/share") for item in seen)
    assert any(item.endswith("/task") for item in seen)
    assert any(item.endswith("/share/password") for item in seen)


@pytest.mark.asyncio
async def test_terabox_share_posts_path_list():
    import httpx

    from backend.services.games.clouds import _terabox_share

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "shorturl": "1ShareOk",
                "link": "https://www.terabox.com/s/1ShareOk",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://www.terabox.com"
    ) as client:
        url = await _terabox_share(
            client, "token", "/website/ixacg.top/game/demo", 123, "sakuramai"
        )
    from urllib.parse import unquote

    body = unquote(seen["body"])
    assert "share/pset" in seen["url"]
    assert "path_list=" in seen["body"]
    assert "/website/ixacg.top/game/demo" in body
    assert "1ShareOk" in url
    assert url.endswith("pwd=saku")


def test_baidu_title_folder_uses_pinyin_and_ascii_initials():
    assert baidu_title_folder("测试游戏 v1.0") == "csyxv10"
    assert baidu_title_folder("康乃馨俱乐部 Pale Carnations") == "knxjlbpc"
    assert "/" not in baidu_title_folder("标题/非法")
    assert target_folder_name("quark", "测试游戏 v1.0") == "csyxv10"
    assert target_folder_name("pikpak", "测试游戏 v1.0") == "测试游戏 v1.0"
    assert target_folder_name("baidu", "与你共度的回忆_Memories with You") == "yngddhymwy"
    assert "/" not in target_folder_name("baidu", "标题/非法")


def test_baidu_remote_path_stays_under_configured_dir(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings({"baidu_remote_dir": "/网站/ixacg/game"})
    assert target_base_dir(settings, "baidu") == "/网站/ixacg/game"
    assert target_remote_path(
        settings, "baidu", "与你共度的回忆_Memories with You"
    ) == "/网站/ixacg/game/yngddhymwy"


@pytest.mark.asyncio
async def test_baidu_ensure_folder_creates_only_missing_child_under_parent():
    from urllib.parse import unquote_plus

    import httpx

    tree: dict[str, list[dict]] = {
        "/": [{"fs_id": 1, "isdir": 1, "server_filename": "网站", "path": "/网站"}],
        "/网站": [
            {"fs_id": 2, "isdir": 1, "server_filename": "ixacg", "path": "/网站/ixacg"}
        ],
        "/网站/ixacg": [],
        "/网站/ixacg/game": [],
    }
    creates: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = request.url
        if parsed.path.endswith("/api/list"):
            directory = parsed.params.get("dir") or "/"
            items = tree.get(directory.rstrip("/") or "/", None)
            if items is None:
                return httpx.Response(200, json={"errno": -9, "list": []})
            return httpx.Response(200, json={"errno": 0, "list": items})
        if parsed.path.endswith("/api/create"):
            assert parsed.params.get("a") == "commit"
            assert parsed.params.get("web") == "1"
            body = unquote_plus(request.content.decode())
            assert "rtype=0" in body
            assert "isdir=1" in body
            path = ""
            for part in body.split("&"):
                if part.startswith("path="):
                    path = unquote_plus(part.split("=", 1)[1])
            creates.append(path)
            parent = path.rsplit("/", 1)[0] or "/"
            name = path.rsplit("/", 1)[-1]
            assert parent in tree, f"must not create {path} under missing {parent}"
            fs_id = 10 + len(creates)
            item = {
                "fs_id": fs_id,
                "isdir": 1,
                "server_filename": name,
                "path": path,
            }
            tree[parent].append(item)
            tree[path] = []
            return httpx.Response(200, json={"errno": 0, "fs_id": fs_id, "path": path})
        if parsed.path.endswith("/api/search"):
            return httpx.Response(200, json={"errno": 0, "list": []})
        return httpx.Response(404, text="missing")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        folder_id = await _baidu_ensure_folder(
            client, "token", "/网站/ixacg/game/yngddhymwy"
        )
    assert creates == [
        "/网站/ixacg/game",
        "/网站/ixacg/game/yngddhymwy",
    ]
    assert folder_id == 12
    assert all(not item.startswith("/game") for item in creates)
    assert all(not item.startswith("/ixacg") for item in creates)


@pytest.mark.asyncio
async def test_baidu_ensure_folder_falls_back_when_list_returns_minus_six():
    from urllib.parse import unquote_plus

    import httpx

    known = {
        "/网站": 1,
        "/网站/ixacg": 2,
        "/网站/ixacg/game": 3,
    }
    creates: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = request.url
        if parsed.path.endswith("/api/list"):
            return httpx.Response(200, json={"errno": -6, "request_id": 1})
        if parsed.path.endswith("/api/search"):
            directory = parsed.params.get("dir") or "/"
            key = parsed.params.get("key") or ""
            path = f"{directory.rstrip('/')}/{key}" if directory != "/" else f"/{key}"
            fs_id = known.get(path)
            if not fs_id:
                return httpx.Response(200, json={"errno": 0, "list": []})
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "list": [
                        {
                            "fs_id": fs_id,
                            "isdir": 1,
                            "server_filename": key,
                            "path": path,
                        }
                    ],
                },
            )
        if parsed.path.endswith("/api/create"):
            body = unquote_plus(request.content.decode())
            path = ""
            for part in body.split("&"):
                if part.startswith("path="):
                    path = unquote_plus(part.split("=", 1)[1])
            creates.append(path)
            fs_id = 99
            known[path] = fs_id
            return httpx.Response(200, json={"errno": 0, "fs_id": fs_id, "path": path})
        if parsed.path.endswith("/api/filemetas"):
            return httpx.Response(200, json={"errno": -6})
        return httpx.Response(404, text="missing")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        folder_id = await _baidu_ensure_folder(
            client, "token", "/网站/ixacg/game/yngddhymwy"
        )
    assert creates == ["/网站/ixacg/game/yngddhymwy"]
    assert folder_id == 99


@pytest.mark.asyncio
async def test_baidu_share_uses_folder_id_and_permanent_flag():
    from urllib.parse import unquote

    import httpx

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/share/set"):
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "expiredType": 0,
                    "shareid": 23330981732,
                    "shorturl": "https://pan.baidu.com/s/1Abc_def",
                    "link": "https://pan.baidu.com/s/1Abc_def",
                },
            )
        if path.endswith("/share/record"):
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "list": [
                        {
                            "typicalPath": "/网站/ixacg/game/yajmmdzyrcv102",
                            "shortlink": "https://pan.baidu.com/s/1Abc_def",
                            "expiredType": 1,
                            "status": 0,
                            "fsIds": [589236248882280],
                            "shareId": 23330981732,
                        }
                    ],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        url = await _baidu_share(client, "tok", ["589236248882280"], "q2xn")
    body = unquote(seen["body"])
    assert "fid_list=" in seen["body"]
    assert "589236248882280" in body
    assert "period=0" in body.replace("%3D", "=") or "period" in body
    assert "expiredType=1" in body.replace("%3D", "=") or "expiredType" in body
    assert url.startswith("https://pan.baidu.com/s/")
    assert url.endswith("pwd=q2xn")
    assert _baidu_normalize_share_url("1Abc_def", "q2xn").endswith("pwd=q2xn")


@pytest.mark.asyncio
async def test_baidu_share_rejects_immediately_expired_folder():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/share/set"):
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "expiredType": 0,
                    "shareid": 1,
                    "shorturl": "https://pan.baidu.com/s/expired",
                    "link": "https://pan.baidu.com/s/expired",
                },
            )
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "typicalPath": "分享已过期",
                        "shortlink": "https://pan.baidu.com/s/expired",
                        "expiredType": -1,
                        "status": 9,
                        "fsIds": [678830627216824],
                        "shareId": 1,
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(CloudError, match="已过期"):
            await _baidu_share(client, "tok", ["678830627216824"], "q2xn")


def test_normalize_upload_targets_filters_and_dedupes():
    assert normalize_upload_targets(None) is None
    assert normalize_upload_targets([]) == []
    assert normalize_upload_targets(["quark", "baidu", "baidu", "ftp"]) == [
        "quark",
        "baidu",
    ]


def test_cloud_upload_phases_defers_baidu(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings(
        {
            "baidu_cookie": "BDUSS=value",
            "pikpak_refresh_token": "refresh",
            "terabox_cookie": "ndus=value",
            "quark_cookie": "__puus=value",
        }
    )
    fast, slow = cloud_upload_phases(settings, None)
    assert fast == ["pikpak", "terabox", "quark"]
    assert slow == ["baidu"]
    only_baidu, deferred = cloud_upload_phases(settings, ["baidu"])
    assert only_baidu == ["baidu"]
    assert deferred == []


@pytest.mark.asyncio
async def test_upload_targets_emits_on_update(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    dummy = tmp_path / "game.7z"
    dummy.write_bytes(b"7zfake")
    settings = save_games_settings(
        {
            "baidu_cookie": "BDUSS=value",
            "pikpak_refresh_token": "refresh",
        }
    )
    seen: list[list[str]] = []

    async def fake_upload_folder(_settings, target, sources, folder_name, **kwargs):
        del _settings, sources, folder_name, kwargs
        return f"https://example.test/{target}?pwd=ab12"

    async def on_update(partial):
        seen.append(sorted(partial.get("links") or {}))

    monkeypatch.setattr(
        "backend.services.games.clouds.upload_folder", fake_upload_folder
    )
    result = await upload_targets(
        settings,
        title="测试游戏",
        archive_7z=dummy,
        only=["pikpak", "baidu"],
        on_update=on_update,
    )
    assert result["links"]["pikpak"]
    assert result["links"]["baidu"]
    assert seen[0] == ["pikpak"]
    assert seen[-1] == ["baidu", "pikpak"]


def test_parse_game_caption_title_summary_password_and_tags():
    text = """
游戏名称：堕落精灵·芙蕾雅V1.1.1
类型：SLG / 汉化
品牌：Honey Rider
简介：这是由Honey Rider推出的一款互动类型SLG游戏
游戏概要
故事发生在一块名为“艾玛”的大陆中。
下载：https://example.com/file.7z
解压密码：abc123
"""
    parsed = parse_game_caption(text)
    assert parsed.title == "堕落精灵·芙蕾雅V1.1.1"
    assert "Honey Rider" in parsed.summary or "艾玛" in parsed.summary
    assert parsed.password == "abc123"
    assert "SLG" in parsed.tags
    assert 640 in parsed.category_ids
    assert 637 in parsed.category_ids
    assert parsed.urls == ["https://example.com/file.7z"]


def test_parse_bracket_title_and_android_category():
    parsed = parse_game_caption("【安卓】某游戏 APK\n简介：手机版")
    assert parsed.title == "某游戏 APK"
    assert 641 in parsed.category_ids


def test_parse_caption_drops_download_banner_and_hashtags():
    parsed = parse_game_caption(
        """
🤩  与傲娇妹妹的治愈日常ver1.0.2 🤩
（ツンデレ妹と私の癒しの日常 ~）

🌟▎简介：
这是由[白箱つるぺた] 推出的一款互动类型SLG游戏

🤍▎游戏概要
父母出差，偌大的宅邸里只剩下了我和妹妹两个人。

🔄🔄🔄🔄🔄🔄
❗️         ❗️
🔄🔄🔄🔄🔄🔄

🌟 入正地址[白箱つるぺた] 🌟

〰️下载地址〰️

#SLG #PC #安卓 #骑兵
"""
    )
    assert parsed.title == "与傲娇妹妹的治愈日常ver1.0.2"
    assert "互动类型SLG" in parsed.summary
    assert "父母出差" in parsed.summary
    assert "入正地址" not in parsed.summary
    assert "下载地址" not in parsed.summary
    assert "#SLG" not in parsed.summary
    assert "🔄" not in parsed.summary
    assert {"SLG", "PC", "安卓", "骑兵"}.issubset(parsed.tags)


def test_parse_caption_drops_translation_promo():
    parsed = parse_game_caption(
        """
巨乳幻想3 IF-阿尔忒弥斯之箭，美杜莎的愿望
“阿尔忒弥斯之箭”
自从乌利纳斯带着雅典娜回到天堂已经有几年了。
巨乳幻想系列
❗️PS:三贤者个人汉化,❗️
(喜欢可支持一下噢)
#ADV #PC #骑兵
"""
    )
    assert parsed.title == "巨乳幻想3 IF-阿尔忒弥斯之箭，美杜莎的愿望"
    assert "乌利纳斯" in parsed.summary
    assert "巨乳幻想系列" in parsed.summary
    assert "个人汉化" not in parsed.summary
    assert "支持一下" not in parsed.summary
    assert "PS:" not in parsed.summary
    assert "喜欢可支持" not in parsed.summary


def test_clean_game_summary_strips_channel_promo_from_dirty_draft():
    cleaned = clean_game_summary(
        "“阿尔忒弥斯之箭”\n巨乳幻想系列\n"
        "❗️PS:三贤者个人汉化,❗️\n(喜欢可支持一下噢)\n"
        "🍀入正地址[waffle]🍀\n🔄下载地址🔄\n#ADV #PC"
    )
    assert "阿尔忒弥斯之箭" in cleaned
    assert "巨乳幻想系列" in cleaned
    assert "个人汉化" not in cleaned
    assert "喜欢可支持" not in cleaned
    assert "入正地址" not in cleaned
    assert "下载地址" not in cleaned
    assert "（乌利纳斯还活着" in clean_game_summary(
        "（乌利纳斯还活着！\n❗️PS:三贤者个人汉化,❗️"
    )


def test_parse_real_zhzbzx_19715_caption_drops_promo():
    parsed = parse_game_caption(
        "❄️  巨乳幻想3 IF-阿尔忒弥斯之箭，美杜莎的愿望  ❄️\n"
        " “阿尔忒弥斯之箭”\n"
        "      自从乌利纳斯带着雅典娜回到天堂已经有几年了。\n"
        "巨乳幻想系列\n"
        "➗➗➗➗➗➗\n"
        "❗️PS:三贤者个人汉化,❗️\n"
        "➗➗➗➗➗➗\n"
        "\n"
        "🍀入正地址[waffle]🍀\n"
        "(喜欢可支持一下噢)\n"
        "\n"
        "🔄下载地址🔄\n"
        "\n"
        "#ADV #PC #骑兵"
    )
    assert parsed.title == "巨乳幻想3 IF-阿尔忒弥斯之箭，美杜莎的愿望"
    assert "乌利纳斯" in parsed.summary
    assert "个人汉化" not in parsed.summary
    assert "喜欢可支持" not in parsed.summary
    assert "入正地址" not in parsed.summary
    assert "下载地址" not in parsed.summary
    assert "➗" not in parsed.summary


def test_extract_image_urls_from_wp_html():
    urls = extract_image_urls(
        '<figure><img alt="1" src="https://s5.ixacg.top/a.avif" /></figure>'
        '<img src="https://s5.ixacg.top/b.avif"/>'
    )
    assert urls == [
        "https://s5.ixacg.top/a.avif",
        "https://s5.ixacg.top/b.avif",
    ]


def test_parse_channel_caption_cleans_title_and_detects_both_platforms():
    parsed = parse_game_caption(
        "🐰 测试游戏 v1.0 🐰\n这是由[Studio X]创作推出的一款SLG游戏\n#SLG #PC #安卓"
    )
    assert parsed.title == "测试游戏 v1.0"
    assert parsed.studio == "Studio X"
    assert {640, 641}.issubset(parsed.category_ids)
    assert {"SLG", "PC", "安卓"}.issubset(parsed.tags)


def test_suggest_native_category():
    assert 639 in suggest_categories("日文原版 原生游戏")


def test_parse_telegram_zhzbzx_url():
    ref = parse_telegram_post_url("https://t.me/Zhzbzx/19706?single")
    assert ref.channel == "Zhzbzx"
    assert ref.post_id == 19706


def test_download_space_reserve_scales_with_small_archive(tmp_path: Path, monkeypatch):
    archive_bytes = 2 * 1024
    monkeypatch.setattr(
        "backend.services.games.telegram.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=archive_bytes * 3),
    )

    _ensure_download_space(tmp_path, archive_bytes)


def test_download_space_reserve_is_capped_for_large_archive(tmp_path: Path, monkeypatch):
    archive_bytes = DOWNLOAD_SPACE_RESERVE_MAX_BYTES * 2
    required = archive_bytes * 2 + DOWNLOAD_SPACE_RESERVE_MAX_BYTES
    monkeypatch.setattr(
        "backend.services.games.telegram.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=required - 1),
    )

    with pytest.raises(GamesTelegramError, match="磁盘空间不足"):
        _ensure_download_space(tmp_path, archive_bytes)


def test_group_channel_history_deduplicates_media_group():
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    image = SimpleNamespace(
        id=101,
        media_group_id=777,
        date=old,
        photo=object(),
        document=None,
        caption="游戏名称：测试游戏",
        text=None,
    )
    archive = SimpleNamespace(
        id=102,
        media_group_id=777,
        date=old,
        photo=None,
        document=SimpleNamespace(
            file_name="game.7z",
            mime_type="application/x-7z-compressed",
            file_size=1024,
            attributes=[],
        ),
        caption=None,
        text=None,
    )
    result = group_channel_history(
        [archive, image],
        channel="Zhzbzx",
        chat_id="-100123",
        username="Zhzbzx",
        settle_before=datetime.now(timezone.utc).timestamp() - 15,
    )
    assert result["latest_message_id"] == 102
    assert len(result["posts"]) == 1
    post = result["posts"][0]
    assert post["source_key"] == "-100123:g:777"
    assert post["post_id"] == 101
    assert post["max_message_id"] == 102
    assert post["has_image"] is True
    assert post["url"] == "https://t.me/Zhzbzx/101"


def _text_link(caption: str, label: str, url: str, *, occurrence: int = 0):
    start = -1
    for _ in range(occurrence + 1):
        start = caption.index(label, start + 1)
    return SimpleNamespace(
        type="text_link",
        url=url,
        offset=len(caption[:start].encode("utf-16-le")) // 2,
        length=len(label.encode("utf-16-le")) // 2,
    )


def test_hidden_download_link_is_discovered_from_caption_entities():
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    caption = "🐰 测试游戏\n旧版\n👻下载地址🎈\n#SLG #PC"
    message = SimpleNamespace(
        id=201,
        media_group_id=888,
        date=old,
        photo=object(),
        document=None,
        caption=caption,
        text=None,
        caption_entities=[
            _text_link(caption, "旧版", "https://t.me/Zhzbzx/100"),
            _text_link(caption, "下载地址", "https://t.me/Hvnkbfc/50"),
        ],
    )
    assert telegram_file_links([message], source_channel="Zhzbzx") == [
        "https://t.me/Hvnkbfc/50"
    ]
    result = group_channel_history(
        [message],
        channel="Zhzbzx",
        chat_id="-100123",
        username="Zhzbzx",
        settle_before=datetime.now(timezone.utc).timestamp() - 15,
    )
    assert result["posts"][0]["file_urls"] == ["https://t.me/Hvnkbfc/50"]


def test_versioned_download_links_keep_latest_only():
    caption = (
        "影色渐染 V1.1.3\n"
        "🔄下载地址[ v1.13]🔄\n"
        "🔄下载地址[02512-20 v1.11]🔄\n"
        "🔄下载地址[v1.10]🔄\n"
        "🔄下载地址🔄\n"
    )
    message = SimpleNamespace(
        caption=caption,
        text=None,
        caption_entities=[
            _text_link(caption, "下载地址", "https://t.me/Hvnkbfc/6466", occurrence=0),
            _text_link(caption, "下载地址", "https://t.me/Hvnkbfc/6456", occurrence=1),
            _text_link(caption, "下载地址", "https://t.me/Hvnkbfc/6355", occurrence=2),
            _text_link(caption, "下载地址", "https://t.me/Hvnkbfc/6240", occurrence=3),
        ],
        entities=None,
    )
    assert telegram_file_links([message], source_channel="Zhzbzx") == [
        "https://t.me/Hvnkbfc/6466"
    ]


def test_variant_download_links_are_all_kept():
    caption = "测试游戏\n🔄通常版本🔄\n🔄ntr版本🔄\n"
    message = SimpleNamespace(
        caption=caption,
        text=None,
        caption_entities=[
            _text_link(caption, "通常版本", "https://t.me/Hvnkbfc/10"),
            _text_link(caption, "ntr版本", "https://t.me/Hvnkbfc/11"),
        ],
        entities=None,
    )
    assert telegram_file_links([message], source_channel="Zhzbzx") == [
        "https://t.me/Hvnkbfc/10",
        "https://t.me/Hvnkbfc/11",
    ]


def test_preview_mp4_is_filtered_but_disguised_volume_stays_archive():
    trailer = SimpleNamespace(
        photo=None,
        animation=None,
        video_note=None,
        video=SimpleNamespace(
            file_name="29岁的人妻想成为职业coser.mp4",
            mime_type="video/mp4",
            file_size=7_538_910,
            duration=18,
            thumbs=[SimpleNamespace(file_id="thumb-id")],
        ),
        document=None,
    )
    assert _is_preview_video(trailer) is True
    assert _is_archive(trailer) is False
    assert _is_archive_member(trailer) is False
    assert _visual_download(trailer) is None

    disguised = SimpleNamespace(
        photo=None,
        animation=None,
        video_note=None,
        video=None,
        document=SimpleNamespace(
            file_name="csyxv10.001.mp4",
            mime_type="video/mp4",
            file_size=200 * 1024 * 1024,
            attributes=[],
        ),
    )
    assert _is_preview_video(disguised) is False
    assert _is_archive(disguised) is True


def test_split_archive_parts_are_downloaded_but_only_root_is_extracted():
    def message(name: str, mime: str = ""):
        return SimpleNamespace(
            document=SimpleNamespace(
                file_name=name,
                mime_type=mime,
                file_size=1024,
                attributes=[],
            ),
            video=None,
        )

    part = message("game.z01")
    root = message("game.zip", "application/zip")
    assert _is_archive_member(part) is True
    assert _is_primary_archive(part) is False
    assert _is_archive_member(root) is True
    assert _is_primary_archive(root) is True


@pytest.mark.asyncio
async def test_pull_post_follows_hidden_file_channel_link(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings({"telegram_account_name": "games-test"})
    caption = "🐰 测试游戏 🐰\n👻下载地址🎈\n#SLG #PC"
    source = SimpleNamespace(
        id=301,
        media_group_id=901,
        date=datetime.now(timezone.utc) - timedelta(minutes=2),
        photo=object(),
        video=None,
        animation=None,
        document=None,
        caption=caption,
        text=None,
        caption_entities=[_text_link(caption, "下载地址", "https://t.me/Hvnkbfc/51")],
    )
    trailer = SimpleNamespace(
        id=300,
        media_group_id=901,
        date=datetime.now(timezone.utc) - timedelta(minutes=2),
        photo=None,
        animation=None,
        video_note=None,
        video=SimpleNamespace(
            file_name="trailer.mp4",
            mime_type="video/mp4",
            file_size=7_538_910,
            duration=18,
            thumbs=[SimpleNamespace(file_id="thumb-id")],
        ),
        document=None,
        caption=None,
        text=None,
        caption_entities=[],
    )

    def archive_message(message_id: int, name: str, mime: str = ""):
        return SimpleNamespace(
            id=message_id,
            media_group_id=902,
            date=datetime.now(timezone.utc) - timedelta(minutes=2),
            photo=None,
            video=None,
            animation=None,
            document=SimpleNamespace(
                file_name=name,
                mime_type=mime,
                file_size=1024,
                attributes=[],
            ),
            caption=None,
            text=None,
            caption_entities=[],
        )

    split_part = archive_message(51, "game.z01")
    split_root = archive_message(52, "game.zip", "application/zip")

    class FakeClient:
        async def get_messages(self, chat, post_id):
            if str(chat) == "Zhzbzx" and post_id == 301:
                return source
            if str(chat) == "Hvnkbfc" and post_id == 51:
                return split_part
            raise AssertionError((chat, post_id))

        async def get_media_group(self, chat, post_id):
            if str(chat) == "Zhzbzx":
                return [trailer, source]
            return [split_part, split_root]

        async def download_media(self, message, file_name):
            name = str(getattr(getattr(message, "video", None), "file_name", "") or "")
            if str(file_name).endswith(".mp4") or name.endswith(".mp4"):
                raise AssertionError("preview mp4 should not be downloaded")
            target = Path(file_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"telegram-data")
            return str(target)

    client = FakeClient()

    async def fake_borrow(*_args, **_kwargs):
        return client, None, False

    async def fake_release(*_args, **_kwargs):
        return None

    monkeypatch.setattr("backend.services.games.telegram._borrow_client", fake_borrow)
    monkeypatch.setattr("backend.services.games.telegram._release_client", fake_release)
    monkeypatch.setattr(
        "backend.services.games.telegram._manga_settings", lambda: SimpleNamespace()
    )

    result = await pull_telegram_post(
        settings,
        "https://t.me/Zhzbzx/301",
        job_id="linked-files",
    )
    assert result["telegram_file_urls"] == ["https://t.me/Hvnkbfc/51"]
    assert len(result["images"]) == 1
    assert all(not str(item["name"]).endswith(".mp4") for item in result["images"])
    assert [item["name"] for item in result["archive_members"]] == [
        "game.z01",
        "game.zip",
    ]
    assert [item["name"] for item in result["archives"]] == ["game.zip"]


def test_strip_ads_removes_ad_folder_and_url(tmp_path: Path):
    root = tmp_path / "game"
    (root / "广告").mkdir(parents=True)
    (root / "广告" / "click.url").write_text("http://ad.example", encoding="utf-8")
    (root / "www.spam.com").mkdir()
    (root / "www.spam.com" / "note.txt").write_text("ad", encoding="utf-8")
    play = root / "RealGame"
    play.mkdir()
    (play / "game.exe").write_bytes(b"MZ")
    (root / "desktop.ini").write_text("junk", encoding="utf-8")
    removed = strip_ads(root, ["广告", "www."])
    assert (play / "game.exe").is_file()
    assert not (root / "广告").exists()
    assert not (root / "www.spam.com").exists()
    assert not (root / "desktop.ini").exists()
    assert removed


def test_extract_zip_and_repack_7z(tmp_path: Path):
    source_dir = tmp_path / "payload"
    game = source_dir / "MyGame"
    game.mkdir(parents=True)
    (game / "start.exe").write_bytes(b"game-bytes")
    (source_dir / "广告").mkdir()
    (source_dir / "广告" / "ad.txt").write_text("ad", encoding="utf-8")

    zip_path = tmp_path / "source.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in source_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir))

    extracted = tmp_path / "extracted"
    extract_tree(zip_path, extracted, passwords=["sakuramai"])
    strip_ads(extracted, ["广告"])
    assert (extracted / "MyGame" / "start.exe").is_file()
    assert not (extracted / "广告").exists()

    packed = tmp_path / "out.7z"
    pack_7z(extracted, packed, "sakuramai")
    assert packed.is_file() and packed.stat().st_size > 0

    again = tmp_path / "again"
    extract_tree(packed, again, passwords=["sakuramai"])
    assert any(again.rglob("start.exe"))


def test_pack_7z_parts_selects_volume_mode(tmp_path: Path, monkeypatch):
    source = tmp_path / "large"
    source.mkdir()
    (source / "game.bin").write_bytes(b"0123456789")
    dest = tmp_path / "game.7z"
    called: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        called.append(list(cmd))
        Path(f"{dest}.001").write_bytes(b"part-1")
        Path(f"{dest}.002").write_bytes(b"part-2")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backend.services.games.archive.find_7z_bin", lambda: "7z")
    monkeypatch.setattr("backend.services.games.archive.subprocess.run", fake_run)
    parts = pack_7z_parts(source, dest, "secret", volume_bytes=5)
    assert [path.name for path in parts] == ["game.7z.001", "game.7z.002"]
    assert "-v5b" in called[0]


def test_wordpress_html_contains_pay_box_and_images():
    html = build_post_html(
        summary="简介：这是一款 SLG",
        image_urls=["https://s5.ixacg.top/cover.avif", "https://s5.ixacg.top/2.avif"],
        title="测试游戏",
        pack_password="sakuramai",
        links={
            "baidu": "https://pan.baidu.com/s/abc?pwd=k7m2",
            "pikpak": "https://mypikpak.com/s/xyz?pwd=k7m2",
            "quark": "https://pan.quark.cn/s/abc?pwd=k7m2",
        },
        apate_used=True,
        pay_enabled=True,
        share_pwd="k7m2",
    )
    assert "<!--paystart-->" in html
    assert "sakuramai" in html
    assert "k7m2" in html
    assert "网盘提取码" in html
    assert "pan.baidu.com" in html
    assert "mypikpak.com" in html
    assert "pan.quark.cn" in html
    assert "wp-block-image" in html
    assert "Apate" in html
    assert "https://www.ixacg.top/16403.html" in html
    assert "7980.html" not in html
    assert "图片格式如何解压" not in html
    assert "本站资源" not in html
    assert "联系站长" not in html


def test_zibpay_meta_balance_and_points():
    links = {
        "baidu": "https://pan.baidu.com/s/abc",
        "pikpak": "https://mypikpak.com/s/xyz",
    }
    cash = build_zibpay_meta(
        pay_modo="0",
        pay_price=5,
        points_price=20,
        links=links,
        pack_password="sakuramai",
        apate_used=True,
    )
    assert cash["pay_type"] == "2"
    assert cash["pay_modo"] == "0"
    assert cash["pay_price"] == "5"
    assert cash["points_price"] == ""
    assert cash["pay_download"][0]["link"].endswith("/abc")
    assert "sakuramai" in cash["pay_extra_hide"]
    assert cash["pay_details"] == ""
    assert "本站资源" not in cash["pay_details"]
    assert "联系站长" not in cash["pay_details"]
    assert "https://www.ixacg.top/16403.html" in cash["pay_extra_hide"]
    assert "图片格式如何解压" not in cash["pay_extra_hide"]
    assert "7980.html" not in cash["pay_extra_hide"]
    assert cash["vip_1_price"] == ""
    assert cash["vip_2_points"] == ""

    vip = build_zibpay_meta(
        pay_modo="0",
        pay_price=5,
        points_price=20,
        links=links,
        pack_password="sakuramai",
        apate_used=True,
        vip1_price=3,
        vip2_price=0,
        vip1_points=8,
        vip2_points=0,
    )
    assert vip["vip_1_price"] == "3"
    assert vip["vip_2_price"] == "0"
    assert vip["vip_1_points"] == "8"
    assert vip["vip_2_points"] == "0"

    points = build_zibpay_meta(
        pay_modo="积分",
        pay_price=5,
        points_price=20,
        links=links,
        pack_password="sakuramai",
        apate_used=False,
    )
    assert normalize_pay_modo("积分") == "points"
    assert points["pay_modo"] == "points"
    assert points["pay_price"] == ""
    assert points["points_price"] == "20"


def test_pay_extra_template_fills_auto_extract_code():
    html = build_pay_extra_hide(
        pack_password="sakuramai",
        apate_used=True,
        links={"baidu": "https://pan.baidu.com/s/abc?pwd=k7m2"},
        share_pwd="q2xn",
        extra_template="解压密码：{pack_password}\n\n网盘提取码：{extract}\n\n请用 {apate} 还原",
        apate_url="https://www.ixacg.top/16403.html",
    )
    assert "sakuramai" in html
    assert "q2xn" in html
    assert "k7m2" not in html
    assert "https://www.ixacg.top/16403.html" in html
    assert "图片格式如何解压" not in html

    skipped = build_pay_extra_hide(
        pack_password="sakuramai",
        apate_used=False,
        links={"pikpak": "https://mypikpak.com/s/xyz?pwd=ab12"},
        share_pwd="ab12",
        extra_template="密码 {pack_password}\n请用 {apate} 还原",
    )
    assert "ab12" in skipped
    assert "Apate" not in skipped
    assert "{extract}" not in skipped


def test_wordpress_source_slug_is_stable_and_source_specific():
    first = source_post_slug("-100123:g:888")
    assert first == source_post_slug("-100123:g:888")
    assert first.startswith("tg-game-")
    assert first != source_post_slug("-100123:g:889")


@pytest.mark.asyncio
async def test_upload_targets_prefers_manual_links(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    dummy = tmp_path / "a.7z"
    dummy.write_bytes(b"7zfake")
    result = await upload_targets(
        load_games_settings(),
        archive_7z=dummy,
        disguised_mp4=None,
        manual={
            "baidu": "https://pan.baidu.com/s/abc",
            "pikpak": "https://mypikpak.com/s/xyz",
            "terabox": "https://www.terabox.com/s/def",
            "quark": "https://pan.quark.cn/s/ghi",
        },
    )
    assert result["links"]["baidu"].endswith("/abc")
    assert "pikpak" in result["links"]["pikpak"]
    assert "terabox" in result["links"]["terabox"]
    assert "quark" in result["links"]["quark"]
    assert result["errors"] == {}
    assert len(result["share_pwd"]) == 4


@pytest.mark.asyncio
async def test_upload_targets_are_serial_and_use_target_folder_names(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    parts = [tmp_path / "游戏.7z.001", tmp_path / "游戏.7z.002"]
    for part in parts:
        part.write_bytes(b"part")
    settings = save_games_settings(
        {
            "baidu_cookie": "BDUSS=value",
            "pikpak_refresh_token": "refresh",
            "terabox_cookie": "ndus=value",
            "quark_cookie": "__puus=value",
        }
    )
    calls: list[tuple[str, str, list[str]]] = []
    active = 0
    max_active = 0

    async def fake_upload_folder(_settings, target, sources, folder_name, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append(
            (target, folder_name, [path.name for path in sources], kwargs.get("share_pwd"))
        )
        await asyncio.sleep(0)
        active -= 1
        if target == "pikpak":
            return "https://mypikpak.com/s/x?pwd=pk01"
        return f"https://example.test/{target}?pwd={kwargs.get('share_pwd')}"

    monkeypatch.setattr(
        "backend.services.games.clouds.upload_folder", fake_upload_folder
    )
    result = await upload_targets(
        settings,
        title="测试游戏 v1.0",
        archive_parts=parts,
    )
    assert max_active == 1
    assert [item[0] for item in calls] == ["pikpak", "baidu", "terabox", "quark"]
    assert calls[0][1] == "测试游戏 v1.0"
    assert calls[1][1] == "csyxv10"
    assert calls[2][1] == "测试游戏 v1.0"
    assert calls[3][1] == "csyxv10"
    assert result["folders"]["baidu"] == "/games/csyxv10"
    assert result["share_pwd"] == "pk01"
    assert result["links"]["quark"].endswith("pk01")


@pytest.mark.asyncio
async def test_upload_targets_only_selected_clouds(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    dummy = tmp_path / "game.7z"
    dummy.write_bytes(b"7zfake")
    settings = save_games_settings(
        {
            "baidu_cookie": "BDUSS=value",
            "pikpak_refresh_token": "refresh",
            "terabox_cookie": "ndus=value",
            "quark_cookie": "__puus=value",
        }
    )
    calls: list[str] = []

    async def fake_upload_folder(_settings, target, sources, folder_name, **kwargs):
        del _settings, sources, folder_name, kwargs
        calls.append(target)
        return f"https://example.test/{target}?pwd=ab12"

    monkeypatch.setattr(
        "backend.services.games.clouds.upload_folder", fake_upload_folder
    )
    result = await upload_targets(
        settings,
        title="测试游戏 v1.0",
        archive_7z=dummy,
        only=["baidu", "quark"],
        manual={"terabox": "https://www.terabox.com/s/skip-me"},
    )
    assert calls == ["baidu", "quark"]
    assert set(result["links"]) == {"baidu", "quark"}
    assert "terabox" not in result["links"]
    assert result["errors"] == {}


def test_catalog_paginates(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_games_settings()
    games_dirs(settings)
    for index in range(15):
        upsert_entry(
            settings,
            {
                "id": index + 1,
                "title": f"游戏 {index}",
                "summary": "概要",
                "job_id": f"job-{index}",
                "tags": ["SLG"],
            },
        )
    page1 = list_catalog(settings, page=1, limit=12)
    page2 = list_catalog(settings, page=2, limit=12)
    assert page1["total"] == 15
    assert len(page1["data"]) == 12
    assert len(page2["data"]) == 3
    found = list_catalog(settings, page=1, limit=12, query="游戏 1")
    assert found["total"] >= 1


def test_catalog_source_deduplication(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_games_settings()
    upsert_entry(
        settings,
        {
            "id": 10,
            "title": "自动发布游戏",
            "source_key": "-100123:m:8",
            "source_url": "https://t.me/channel/8",
            "job_id": "job-8",
        },
    )
    assert catalog_has_source(settings, source_key="-100123:m:8") is True
    assert catalog_has_source(settings, source_url="https://t.me/channel/8") is True
    assert catalog_has_source(settings, source_key="-100123:m:9") is False


@pytest.mark.asyncio
async def test_games_runner_chains_auto_pull_and_publish(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = load_games_settings()
    runner = GamesJobRunner()

    async def fake_pull(*_args, **_kwargs):
        return {
            "source_url": "https://t.me/Zhzbzx/20",
            "title": "自动测试游戏",
            "summary": "概要",
            "tags": ["SLG"],
            "category_ids": [640, 637],
            "images": [],
            "archives": [{"path": "inbox/fake.7z", "name": "fake.7z"}],
        }

    async def fake_publish(_settings, job, _payload):
        job.update(
            {
                "stage": "done",
                "running": False,
                "message": "已发布到网站",
                "public_url": "https://example.test/game",
            }
        )
        runner._touch(settings, job)

    monkeypatch.setattr("backend.services.games.worker.pull_telegram_post", fake_pull)
    monkeypatch.setattr(runner, "_publish", fake_publish)
    started = await runner.start_auto_publish(
        settings,
        url="https://t.me/Zhzbzx/20",
        source_key="-100123:m:20",
    )
    result = await runner.wait_for_job(str(started["id"]))
    assert result["stage"] == "done"
    assert result["automatic"] is True
    assert result["source_key"] == "-100123:m:20"


@pytest.mark.asyncio
async def test_publish_workflow_uploads_disguised_baidu_volumes_one_by_one(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings(
        {
            "cleanup_after_publish": False,
            "apate_enabled": True,
            "pack_password": "sakuramai",
        }
    )
    dirs = games_dirs(settings)
    source = dirs.inbox / "workflow" / "source.zip"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Game/start.exe", b"game")
    job = new_job(
        settings,
        title="测试游戏 v1.0",
        archives=[
            {
                "path": relative_to_games(settings, source),
                "name": source.name,
                "size": source.stat().st_size,
            }
        ],
        images=[],
    )
    uploaded_names: list[str] = []

    def fake_pack(_source, dest, _password, *, volume_bytes):
        assert volume_bytes == 4096 * 1024 * 1024
        parts = [Path(f"{dest}.001"), Path(f"{dest}.002")]
        for part in parts:
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"volume")
        return parts

    def fake_disguise(_settings, source_part, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_part, dest)
        return dest

    async def fake_targets(_settings, **kwargs):
        parts = kwargs["archive_parts"]
        prepare = kwargs["prepare_baidu"]
        for index, part in enumerate(parts, start=1):
            prepared = await prepare(part, index, len(parts))
            assert prepared.path.is_file()
            uploaded_names.append(prepared.remote_name)
            if prepared.temporary:
                prepared.path.unlink()
        return {
            "links": {"baidu": "https://pan.baidu.com/s/test"},
            "errors": {},
            "folders": {"baidu": "csyxv10"},
        }

    async def fake_tag_ids(_settings, _tags):
        return []

    async def fake_create_post(*_args, **_kwargs):
        return {"id": 88, "link": "https://example.test/game-88"}

    monkeypatch.setattr("backend.services.games.worker.pack_7z_parts", fake_pack)
    monkeypatch.setattr("backend.services.games.worker.disguise_to_mp4", fake_disguise)
    monkeypatch.setattr(
        "backend.services.games.worker.apate_available", lambda _settings: True
    )
    monkeypatch.setattr("backend.services.games.worker.upload_targets", fake_targets)
    monkeypatch.setattr("backend.services.games.worker.resolve_tag_ids", fake_tag_ids)
    monkeypatch.setattr("backend.services.games.worker.create_post", fake_create_post)

    runner = GamesJobRunner()
    await runner._publish(settings, job, {})
    assert uploaded_names == ["csyxv10.001.mp4", "csyxv10.002.mp4"]
    assert job["split_archive"] is True
    assert job["apate_used"] is True
    assert job["remote_folders"]["baidu"] == "csyxv10"
    assert job["stage"] == "done"


@pytest.mark.asyncio
async def test_publish_reuses_existing_packed_parts(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings({"cleanup_after_publish": False})
    dirs = games_dirs(settings)
    packed = dirs.packed / "reuse-job" / "game.7z"
    packed.parent.mkdir(parents=True)
    packed.write_bytes(b"packed-bytes")
    job = new_job(
        settings,
        title="复用打包",
        archives=[],
        images=[],
        packed_parts=[
            {
                "path": relative_to_games(settings, packed),
                "name": packed.name,
                "size": packed.stat().st_size,
            }
        ],
        packed_7z=relative_to_games(settings, packed),
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("不应再次解压或打包")

    async def fake_targets(_settings, **_kwargs):
        return {
            "links": {"pikpak": "https://mypikpak.com/s/x?pwd=abcd"},
            "errors": {},
            "folders": {"pikpak": "复用打包"},
            "share_pwd": "abcd",
        }

    async def fake_create_post(*_args, **_kwargs):
        return {"id": 99, "link": "https://example.test/game-99"}

    async def fake_tag_ids(_settings, _tags):
        return []

    monkeypatch.setattr("backend.services.games.worker.extract_tree", boom)
    monkeypatch.setattr("backend.services.games.worker.pack_7z_parts", boom)
    monkeypatch.setattr("backend.services.games.worker.upload_targets", fake_targets)
    monkeypatch.setattr("backend.services.games.worker.resolve_tag_ids", fake_tag_ids)
    monkeypatch.setattr("backend.services.games.worker.create_post", fake_create_post)

    runner = GamesJobRunner()
    await runner._publish(settings, job, {})
    assert job["wp_id"] == 99
    assert packed.is_file()
    assert job["links"]["pikpak"]


@pytest.mark.asyncio
async def test_publish_posts_wordpress_before_baidu(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings(
        {
            "cleanup_after_publish": False,
            "baidu_cookie": "BDUSS=value",
            "pikpak_refresh_token": "refresh",
            "terabox_cookie": "ndus=value",
            "quark_cookie": "__puus=value",
        }
    )
    dirs = games_dirs(settings)
    packed = dirs.packed / "phase-job" / "game.7z"
    packed.parent.mkdir(parents=True)
    packed.write_bytes(b"packed-bytes")
    job = new_job(
        settings,
        title="分阶段上传",
        archives=[],
        images=[],
        packed_parts=[
            {
                "path": relative_to_games(settings, packed),
                "name": packed.name,
                "size": packed.stat().st_size,
            }
        ],
    )
    calls: list[str] = []

    async def fake_targets(_settings, **kwargs):
        only = list(kwargs.get("only") or [])
        calls.append("upload:" + ",".join(only))
        return {
            "links": {target: f"https://example.test/{target}" for target in only},
            "errors": {},
            "folders": {target: target for target in only},
            "share_pwd": "pk01",
        }

    async def fake_create_post(*_args, **_kwargs):
        calls.append("wordpress")
        return {"id": 101, "link": "https://example.test/game-101"}

    async def fake_tag_ids(_settings, _tags):
        return []

    async def fake_get_post(*_args, **_kwargs):
        return {"content": {"raw": ""}}

    monkeypatch.setattr("backend.services.games.worker.upload_targets", fake_targets)
    monkeypatch.setattr("backend.services.games.worker.resolve_tag_ids", fake_tag_ids)
    monkeypatch.setattr("backend.services.games.worker.create_post", fake_create_post)
    monkeypatch.setattr("backend.services.games.worker.get_post", fake_get_post)

    runner = GamesJobRunner()
    await runner._publish(settings, job, {})
    assert calls[0] == "upload:pikpak,terabox,quark"
    assert calls[1] == "wordpress"
    assert calls[2] == "upload:baidu"
    assert job["wp_id"] == 101
    assert "pikpak" in job["links"]
    assert "baidu" in job["links"]


@pytest.mark.asyncio
async def test_publish_strips_promo_summary_before_wordpress(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings({"cleanup_after_publish": False})
    dirs = games_dirs(settings)
    source = dirs.inbox / "promo" / "source.zip"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Game/start.exe", b"game")
    job = new_job(
        settings,
        title="测试游戏",
        summary="剧情简介\n❗️PS:三贤者个人汉化,❗️\n(喜欢可支持一下噢)",
        archives=[
            {
                "path": relative_to_games(settings, source),
                "name": source.name,
                "size": source.stat().st_size,
            }
        ],
        images=[],
    )
    captured: dict[str, Any] = {}

    def fake_pack(_source, dest, _password, *, volume_bytes):
        del volume_bytes
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"packed")
        return [dest]

    async def fake_targets(_settings, **_kwargs):
        return {
            "links": {"pikpak": "https://mypikpak.com/s/x?pwd=abcd"},
            "errors": {},
            "folders": {"pikpak": "test"},
            "share_pwd": "abcd",
        }

    async def fake_create_post(*_args, **kwargs):
        captured.update(kwargs)
        return {"id": 16413, "link": "https://www.ixacg.top/16413.html"}

    async def fake_tag_ids(_settings, _tags):
        return []

    monkeypatch.setattr("backend.services.games.worker.pack_7z_parts", fake_pack)
    monkeypatch.setattr("backend.services.games.worker.upload_targets", fake_targets)
    monkeypatch.setattr("backend.services.games.worker.resolve_tag_ids", fake_tag_ids)
    monkeypatch.setattr("backend.services.games.worker.create_post", fake_create_post)

    runner = GamesJobRunner()
    await runner._publish(settings, job, {})
    assert "个人汉化" not in job["summary"]
    assert "喜欢可支持" not in job["summary"]
    assert "剧情简介" in job["summary"]
    assert "个人汉化" not in str(captured.get("content") or "")
    catalog = list_catalog(settings)
    assert catalog["data"][0]["links"]["pikpak"]
    assert "个人汉化" not in catalog["data"][0]["summary"]


@pytest.mark.asyncio
async def test_republish_existing_job_updates_catalog_and_strips_promo(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings({"cleanup_after_publish": False})
    job = new_job(
        settings,
        title="巨乳幻想3 IF-阿尔忒弥斯之箭，美杜莎的愿望",
        summary="剧情\n❗️PS:三贤者个人汉化,❗️\n(喜欢可支持一下噢)",
        wp_id=16413,
        public_url="https://www.ixacg.top/16413.html",
        source_url="https://t.me/Zhzbzx/19715",
        source_key="Zhzbzx:m:19715",
        links={
            "pikpak": "https://mypikpak.com/s/x?pwd=7yhc",
            "baidu": "https://pan.baidu.com/s/1abc?pwd=7yhc",
        },
        share_pwd="7yhc",
        cloud_errors={},
        apate_used=True,
    )
    captured: dict[str, Any] = {}

    async def fake_get_post(_settings, post_id):
        assert post_id == 16413
        return {
            "id": 16413,
            "content": {
                "raw": '<img src="https://s5.ixacg.top/cover.avif" />'
            },
        }

    async def fake_create_post(*_args, **kwargs):
        captured.update(kwargs)
        return {"id": 16413, "link": "https://www.ixacg.top/16413.html"}

    async def fake_tag_ids(_settings, _tags):
        return []

    monkeypatch.setattr("backend.services.games.worker.get_post", fake_get_post)
    monkeypatch.setattr("backend.services.games.worker.create_post", fake_create_post)
    monkeypatch.setattr("backend.services.games.worker.resolve_tag_ids", fake_tag_ids)

    runner = GamesJobRunner()
    result = await runner.start_publish(settings, job_id=job["id"], payload={})
    assert result["stage"] == "done"
    saved = json.loads(
        (games_dirs(settings).tasks / f"{job['id']}.json").read_text(encoding="utf-8")
    )
    assert "个人汉化" not in saved["summary"]
    assert saved["links"]["baidu"]
    catalog = list_catalog(settings)
    assert catalog["data"][0]["links"]["baidu"]
    assert "个人汉化" not in catalog["data"][0]["summary"]
    assert "个人汉化" not in str(captured.get("content") or "")
    assert "pan.baidu.com" in str(captured.get("links") or captured)


@pytest.mark.asyncio
async def test_finalize_published_job_keeps_cloud_error_message(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings({})
    job = new_job(
        settings,
        title="测试",
        summary="简介\n❗️PS:三贤者个人汉化,❗️",
        wp_id=1,
        public_url="https://www.ixacg.top/1.html",
        links={"pikpak": "https://mypikpak.com/s/x"},
        cloud_errors={"baidu": "All connection attempts failed"},
    )
    saved = finalize_published_job(settings, job)
    assert "个人汉化" not in saved["summary"]
    assert "baidu" in saved["message"]
    catalog = list_catalog(settings)
    assert "baidu" not in catalog["data"][0]["links"]
    assert catalog["data"][0]["links"]["pikpak"]


@pytest.mark.asyncio
async def test_automation_seeds_cursor_then_publishes_only_new_posts(
    tmp_path: Path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings(
        {
            "telegram_source_channels": "Zhzbzx",
            "telegram_backfill_limit": 0,
            "auto_retry_limit": 3,
        }
    )
    service = GamesAutomationService()
    service.settings = settings
    service.state = _default_state()
    calls = 0

    async def fake_discover(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        posts = [
            {
                "source_key": "-100123:m:10",
                "url": "https://t.me/Zhzbzx/10",
                "post_id": 10,
                "max_message_id": 10,
                "caption": "旧帖子",
            }
        ]
        latest = 10
        if calls > 1:
            posts.append(
                {
                    "source_key": "-100123:m:11",
                    "url": "https://t.me/Zhzbzx/11",
                    "post_id": 11,
                    "max_message_id": 11,
                    "caption": "新帖子",
                }
            )
            latest = 11
        return {
            "chat_id": "-100123",
            "username": "Zhzbzx",
            "latest_message_id": latest,
            "latest_settled_id": latest,
            "posts": posts,
        }

    class FakeRunner:
        def status(self):
            return {"running": False}

        async def start_auto_publish(self, _settings, **_kwargs):
            return {"id": "auto-job-11"}

        async def wait_for_job(self, _job_id):
            return {
                "id": "auto-job-11",
                "stage": "done",
                "public_url": "https://example.test/11",
            }

    monkeypatch.setattr(
        "backend.services.games.automation.discover_telegram_posts", fake_discover
    )
    monkeypatch.setattr(
        "backend.services.games.automation.catalog_has_source",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "backend.services.games.automation.get_games_runner", lambda: FakeRunner()
    )

    await service.run_pass()
    assert service.state["channels"]["Zhzbzx"]["cursor"] == 10
    assert service.state["items"] == {}

    result = await service.run_pass()
    assert result["processed"] == 1
    assert service.state["items"]["-100123:m:11"]["status"] == "done"
    assert "-100123:m:10" not in service.state["items"]


def test_automation_stops_retrying_after_limit(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    settings = save_games_settings({"auto_retry_limit": 2})
    service = GamesAutomationService()
    service.settings = settings
    service.state = _default_state()
    item = {"attempts": 1}
    service._mark_failure(settings, item, "first")
    assert item["status"] == "failed"
    assert item["next_retry_at"] > 0
    item["attempts"] = 2
    service._mark_failure(settings, item, "second")
    assert item["status"] == "exhausted"
    assert item["next_retry_at"] == 0
