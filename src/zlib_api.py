"""Z-Library EAPI 客户端（JSON 接口，不依赖 HTML 解析）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientResponseError

logger = logging.getLogger(__name__)

DEFAULT_DOMAIN = "z-library.sk"

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class ZLibAPIError(Exception):
    pass


class ZLibAPI:
    def __init__(self, domain: str = DEFAULT_DOMAIN):
        domain = domain.removeprefix("https://").removeprefix("http://").strip("/")
        self.domain = domain
        self.base_url = f"https://{domain}"
        self._session: aiohttp.ClientSession | None = None
        self._logged_in = False

    async def __aenter__(self) -> ZLibAPI:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers=HEADERS,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        self._logged_in = False

    def _session_required(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Session not opened")
        return self._session

    async def _post(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._session_required()
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(3):
            logger.debug("POST %s (attempt %d)", url, attempt + 1)
            try:
                async with session.post(url, data=data or {}) as resp:
                    resp.raise_for_status()
                    result = await resp.json(content_type=None)
                    if not isinstance(result, dict):
                        raise ZLibAPIError(f"Unexpected response from {path}")
                    return result
            except ClientResponseError as exc:
                last_exc = exc
                if exc.status >= 500 and attempt < 2:
                    wait = 2 ** attempt
                    logger.warning("API %s 返回 %s，%ds 后重试", path, exc.status, wait)
                    await asyncio.sleep(wait)
                    continue
                raise
            except aiohttp.ClientError as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

        raise ZLibAPIError(f"请求失败: {last_exc}")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._session_required()
        url = f"{self.base_url}{path}"
        logger.debug("GET %s", url)
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            result = await resp.json(content_type=None)
            if not isinstance(result, dict):
                raise ZLibAPIError(f"Unexpected response from {path}")
            return result

    async def login(self, email: str, password: str) -> None:
        await self.open()
        result = await self._post("/eapi/user/login", {"email": email, "password": password})
        if not result.get("success"):
            error = result.get("error") or result.get("message") or "未知错误"
            raise ZLibAPIError(f"登录失败: {error}")
        self._logged_in = True
        logger.info("登录成功 (%s)", self.domain)

    async def get_downloads_remaining(self) -> int | None:
        try:
            result = await self._get("/eapi/user/profile")
            if not result.get("success"):
                return None
            user = result.get("user", {})
            limit = user.get("downloads_limit", 0)
            used = user.get("downloads_today", 0)
            return max(0, limit - used)
        except Exception as exc:
            logger.warning("无法获取下载额度: %s", exc)
            return None

    async def search(
        self,
        query: str,
        extension: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"message": query, "limit": limit}
        if extension:
            data["extensions[]"] = extension.upper()
        result = await self._post("/eapi/book/search", data)
        if not result.get("success"):
            error = result.get("error") or "搜索失败"
            raise ZLibAPIError(str(error))
        return result.get("books") or []

    async def get_book_by_id(self, book_id: str) -> dict[str, Any] | None:
        """通过书籍 ID 搜索（Z-Library 无直接按 ID 查询接口，用 ID 作为搜索词）。"""
        books = await self.search(book_id, limit=10)
        for book in books:
            if str(book.get("id")) == str(book_id):
                return book
        return None

    async def get_download_info(self, book_id: str | int, book_hash: str) -> dict[str, Any]:
        result = await self._get(f"/eapi/book/{book_id}/{book_hash}/file")
        if not result.get("success"):
            error = result.get("error") or "获取下载链接失败"
            raise ZLibAPIError(str(error))
        file_info = result.get("file")
        if not file_info or not file_info.get("downloadLink"):
            raise ZLibAPIError("下载链接为空")
        return file_info

    async def download_file(self, download_url: str, dest_path: str) -> None:
        session = self._session_required()
        headers = HEADERS.copy()
        parsed = urlparse(download_url)
        if parsed.netloc:
            headers["authority"] = parsed.netloc

        timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)
        async with session.get(download_url, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
