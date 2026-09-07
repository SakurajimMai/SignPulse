from __future__ import annotations

from typing import Any

import httpx

from backend.utils.http_error_text import looks_like_html, safe_response_snippet
from backend.utils.outbound import httpx_async_client_kwargs


class APIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PublisherAPIClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        timeout: float = 30,
        client: httpx.AsyncClient | None = None,
    ):
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            **httpx_async_client_kwargs(
                base_url=f"{api_url.rstrip('/')}/",
                timeout=timeout,
                follow_redirects=True,
            )
        )
        self.headers = {"X-Publisher-Token": token}

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        try:
            response = await self.client.request(method, path.lstrip("/"), headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise APIError(f"网络请求失败：{exc}") from exc
        if response.is_error:
            if looks_like_html(response.text):
                detail = safe_response_snippet(response, limit=500)
            else:
                try:
                    body = response.json()
                    detail = body.get("detail", body)
                except ValueError:
                    detail = safe_response_snippet(response, limit=500)
            raise APIError(str(detail), status_code=response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            detail = safe_response_snippet(response, limit=500)
            raise APIError(
                f"Publisher API 响应无效：{detail}",
                status_code=response.status_code,
            ) from exc

    async def health(self):
        return await self._request("GET", "health")

    async def genres(self, language: str):
        return await self._request("GET", "genres", params={"language": language})

    async def resolve_genres(self, language: str, names: list[str]):
        return await self._request(
            "POST",
            "genres/resolve",
            json={"language": language, "names": names},
        )

    async def search_manga(
        self,
        query: str = "",
        language: str | None = None,
        *,
        page: int = 1,
        page_size: int = 20,
    ):
        params = {"query": query, "page": page, "page_size": page_size}
        if language:
            params["language"] = language
        return await self._request("GET", "manga", params=params)

    async def get_manga(self, manga_id: int):
        return await self._request("GET", f"manga/{manga_id}")

    async def preflight(self, payload: dict):
        return await self._request("POST", "preflight", json=payload)

    async def publish(self, payload: dict):
        return await self._request("POST", "publish", json=payload)

    async def update_manga(self, manga_id: int, payload: dict):
        return await self._request("PUT", f"manga/{manga_id}", json=payload)

    async def delete_manga(self, manga_id: int, expected_version: str):
        return await self._request(
            "DELETE",
            f"manga/{manga_id}",
            params={"expected_version": expected_version},
        )

    async def update_chapter(self, chapter_id: int, payload: dict):
        return await self._request("PUT", f"chapters/{chapter_id}", json=payload)

    async def delete_chapter(self, chapter_id: int, expected_version: str):
        return await self._request(
            "DELETE",
            f"chapters/{chapter_id}",
            params={"expected_version": expected_version},
        )
