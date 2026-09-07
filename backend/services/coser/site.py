from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.utils.http_error_text import safe_response_snippet
from backend.utils.outbound import httpx_async_client_kwargs

from .config import CoserSettings

logger = logging.getLogger("backend.coser.site")


class CoserSiteError(RuntimeError):
    pass


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            message = data.get("message") or data.get("detail") or data.get("error")
            if isinstance(message, list) and message:
                message = message[0]
            if isinstance(message, dict):
                message = message.get("msg") or message.get("message")
            if message:
                return str(message)
    except Exception:
        pass
    return safe_response_snippet(response, limit=300)


def _unwrap(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data and len(data) <= 4:
        inner = data.get("data")
        if inner is not None:
            return inner
    return data


def coerce_work(payload: Any) -> dict[str, Any]:
    data = _unwrap(payload)
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict) and ("id" in inner or "title" in inner):
            return inner
        return data
    return {}


def coerce_work_list(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        items = data.get("data") or data.get("items") or data.get("works")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


class CoserSiteClient:
    def __init__(self) -> None:
        self._token = ""

    def _client(self, settings: CoserSettings, timeout: float = 60.0) -> httpx.AsyncClient:
        base = str(settings.site_url or "").rstrip("/")
        if not base:
            raise CoserSiteError("请填写 icoser.de 站点地址")
        return httpx.AsyncClient(
            **httpx_async_client_kwargs(
                base_url=base,
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
                headers={"User-Agent": "TG-SignPulse-Coser/1.0"},
            )
        )

    async def login(self, settings: CoserSettings) -> str:
        email = str(settings.site_email or "").strip()
        password = str(settings.site_password or "").strip()
        if not email or not password:
            raise CoserSiteError("请填写 icoser.de 管理员邮箱和密码")
        async with self._client(settings, timeout=30.0) as client:
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": password},
            )
        if response.status_code >= 400:
            raise CoserSiteError(f"登录 icoser.de 失败：{_detail(response)}")
        payload = _unwrap(response.json())
        if not isinstance(payload, dict):
            raise CoserSiteError("icoser.de 登录响应无效")
        token = str(payload.get("access_token") or payload.get("token") or "").strip()
        if not token:
            raise CoserSiteError("icoser.de 登录未返回 access_token")
        self._token = token
        return token

    def _auth(self) -> dict[str, str]:
        if not self._token:
            raise CoserSiteError("尚未登录 icoser.de")
        return {"Authorization": f"Bearer {self._token}"}

    async def probe(self, settings: CoserSettings) -> dict[str, Any]:
        if not settings.site_email or not settings.site_password:
            return {
                "ok": False,
                "configured": False,
                "error": "未填写管理员账号",
            }
        try:
            await self.login(settings)
            return {
                "ok": True,
                "configured": True,
                "user": settings.site_email,
                "error": "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "configured": True,
                "error": str(exc),
            }

    async def list_cosers(self, settings: CoserSettings, query: str = "") -> list[dict[str, Any]]:
        await self.login(settings)
        params: dict[str, Any] = {"page": 1, "per_page": 100}
        if query.strip():
            params["q"] = query.strip()
        async with self._client(settings) as client:
            response = await client.get(
                "/api/v1/cosers", params=params, headers=self._auth()
            )
        if response.status_code >= 400:
            raise CoserSiteError(f"读取 Coser 列表失败：{_detail(response)}")
        payload = _unwrap(response.json())
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def create_coser(self, settings: CoserSettings, name: str) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise CoserSiteError("Coser 名称不能为空")
        await self.login(settings)
        async with self._client(settings) as client:
            response = await client.post(
                "/api/v1/cosers",
                json={"name": name},
                headers=self._auth(),
            )
        if response.status_code >= 400:
            raise CoserSiteError(f"创建 Coser 失败：{_detail(response)}")
        payload = _unwrap(response.json())
        if not isinstance(payload, dict):
            raise CoserSiteError("创建 Coser 响应无效")
        return payload

    async def create_work(
        self, settings: CoserSettings, payload: dict[str, Any]
    ) -> dict[str, Any]:
        await self.login(settings)
        async with self._client(settings, timeout=60.0) as client:
            response = await client.post(
                "/api/v1/works",
                json=payload,
                headers=self._auth(),
            )
        if response.status_code >= 400:
            raise CoserSiteError(f"发布作品失败：{_detail(response)}")
        data = coerce_work(response.json())
        if not data.get("id"):
            raise CoserSiteError("发布作品响应无效")
        return data

    async def update_work(
        self, settings: CoserSettings, work_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        await self.login(settings)
        async with self._client(settings, timeout=60.0) as client:
            response = await client.put(
                f"/api/v1/works/{int(work_id)}",
                json=payload,
                headers=self._auth(),
            )
        if response.status_code >= 400:
            raise CoserSiteError(f"更新作品失败：{_detail(response)}")
        data = coerce_work(response.json())
        if not data.get("id"):
            data["id"] = int(work_id)
        return data

    async def get_work(self, settings: CoserSettings, work_id: int) -> dict[str, Any]:
        await self.login(settings)
        async with self._client(settings) as client:
            response = await client.get(
                f"/api/v1/works/{int(work_id)}",
                params={"track_view": "false"},
                headers=self._auth(),
            )
        if response.status_code == 404:
            raise CoserSiteError(f"作品 {work_id} 不存在")
        if response.status_code >= 400:
            raise CoserSiteError(f"读取作品失败：{_detail(response)}")
        data = coerce_work(response.json())
        if not data.get("id"):
            raise CoserSiteError("作品详情响应无效")
        return data

    async def list_works(
        self,
        settings: CoserSettings,
        *,
        coser_id: int | None = None,
        query: str = "",
        status: str = "approved",
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        await self.login(settings)
        params: dict[str, Any] = {"page": 1, "per_page": max(1, min(int(per_page), 100))}
        if coser_id:
            params["coser_id"] = int(coser_id)
        if query.strip():
            params["q"] = query.strip()
        if status:
            params["status"] = status
        async with self._client(settings) as client:
            response = await client.get(
                "/api/v1/works", params=params, headers=self._auth()
            )
        if response.status_code >= 400:
            raise CoserSiteError(f"读取作品列表失败：{_detail(response)}")
        return coerce_work_list(response.json())


def public_work_url(settings: CoserSettings, work_id: int) -> str:
    return f"{str(settings.site_url).rstrip('/')}/zh/works/{int(work_id)}"
