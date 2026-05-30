"""Z-Library 客户端封装：搜索、匹配、下载。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import BookEntry, mark_entry_done
from .zlib_api import ZLibAPI, ZLibAPIError

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    entry: BookEntry
    success: bool
    filepath: Path | None = None
    message: str = ""


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200] or "unknown"


def _normalize_match_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[<>:"/\\|?*_\-().,、/]', " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_matches(entry_title: str, filename_stem: str) -> bool:
    title = _normalize_match_text(entry_title)
    stem = _normalize_match_text(filename_stem)
    if not title:
        return True
    if title in stem or stem in title:
        return True
    words = [w for w in title.split() if len(w) >= 3]
    if not words:
        return title in stem
    matched = sum(1 for w in words if w in stem)
    return matched >= min(2, len(words))


def _author_matches(entry_author: str, filename_stem: str) -> bool:
    stem = _normalize_match_text(filename_stem)
    for part in re.split(r"[,、/]", entry_author):
        part = _normalize_match_text(part.strip())
        if not part:
            continue
        if len(part) <= 2:
            continue
        if part in stem:
            return True
        # 姓或名至少匹配一个词
        for word in part.split():
            if len(word) >= 3 and word in stem:
                return True
    return False


def find_local_file(entry: BookEntry, download_dir: Path) -> Path | None:
    """在下载目录中查找与书目录条目匹配的本地文件。"""
    if not download_dir.exists() or not entry.title:
        return None

    ext = entry.extension.lower().lstrip(".") if entry.extension else ""
    matches: list[Path] = []

    for path in download_dir.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            continue

        if ext and path.suffix.lower().lstrip(".") != ext:
            continue
        if not _title_matches(entry.title, path.stem):
            continue
        if entry.author and not _author_matches(entry.author, path.stem):
            continue
        matches.append(path)

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # 多个候选时取文件名最短的（通常更接近原始书名）
    return min(matches, key=lambda p: len(p.name))


def _score_match(entry: BookEntry, book: dict[str, Any]) -> float:
    score = 0.0
    book_title = (book.get("title") or book.get("name") or "").lower()
    author_text = (book.get("author") or "").lower()

    if entry.title:
        title_lower = entry.title.lower()
        if title_lower == book_title:
            score += 100
        elif title_lower in book_title or book_title in title_lower:
            score += 60

    if entry.author:
        author_lower = entry.author.lower()
        # 支持多作者分隔符
        for part in re.split(r"[,、/]", author_lower):
            part = part.strip()
            if part and part in author_text:
                score += 40
                break

    ext = (book.get("extension") or "").upper()
    if entry.extension and ext == entry.extension.upper().lstrip("."):
        score += 20

    try:
        score += float(book.get("qualityScore") or 0) * 2
    except (ValueError, TypeError):
        pass

    return score


class ZLibClient:
    def __init__(
        self,
        email: str,
        password: str,
        download_dir: Path,
        domain: str = "z-library.sk",
        delay_seconds: float = 2.0,
    ):
        self.email = email
        self.password = password
        self.download_dir = download_dir
        self.domain = domain
        self.delay_seconds = delay_seconds
        self.api = ZLibAPI(domain=domain)

    async def login(self) -> None:
        try:
            await self.api.login(self.email, self.password)
        except ZLibAPIError as exc:
            raise RuntimeError(str(exc)) from exc

        remaining = await self.api.get_downloads_remaining()
        if remaining is not None:
            logger.info("今日剩余下载: %d", remaining)
        else:
            logger.info("登录成功（未能读取下载额度，将继续尝试下载）")

    async def close(self) -> None:
        await self.api.close()

    async def _find_best_match(self, entry: BookEntry) -> dict[str, Any] | None:
        if entry.book_id:
            book = await self.api.get_book_by_id(entry.book_id)
            return book

        query = entry.search_query
        if not query:
            return None

        extension = entry.extension or ""
        books = await self.api.search(query, extension=extension, limit=25)
        if not books and extension:
            books = await self.api.search(query, limit=25)

        if not books:
            return None

        return max(books, key=lambda b: _score_match(entry, b))

    def _build_filepath(self, book: dict[str, Any], file_info: dict[str, Any] | None = None) -> Path:
        if file_info and file_info.get("description"):
            name = file_info["description"]
            author = file_info.get("author") or book.get("author") or ""
            ext = (file_info.get("extension") or book.get("extension") or "bin").lower().lstrip(".")
        else:
            name = book.get("title") or "unknown"
            author = book.get("author") or ""
            ext = (book.get("extension") or "bin").lower().lstrip(".")

        if author:
            filename = f"{_sanitize_filename(name)} - {_sanitize_filename(author)}.{ext}"
        else:
            filename = f"{_sanitize_filename(name)}.{ext}"

        return self.download_dir / filename

    async def download_entry(self, entry: BookEntry, skip_existing: bool = True) -> DownloadResult:
        try:
            if skip_existing:
                local_file = find_local_file(entry, self.download_dir)
                if local_file:
                    return DownloadResult(
                        entry=entry,
                        success=True,
                        filepath=local_file,
                        message=f"本地已存在，跳过: {local_file.name}",
                    )

            book = await self._find_best_match(entry)
            if not book:
                return DownloadResult(
                    entry=entry,
                    success=False,
                    message=f"未找到匹配书籍: {entry.display_name}",
                )

            book_id = book.get("id")
            book_hash = book.get("hash")
            if not book_id or not book_hash:
                return DownloadResult(
                    entry=entry,
                    success=False,
                    message=f"书籍信息不完整: {book.get('title', entry.display_name)}",
                )

            file_info = await self.api.get_download_info(book_id, book_hash)
            filepath = self._build_filepath(book, file_info)

            if skip_existing and filepath.exists() and filepath.stat().st_size > 0:
                return DownloadResult(
                    entry=entry,
                    success=True,
                    filepath=filepath,
                    message=f"已存在，跳过: {filepath.name}",
                )

            logger.info("正在下载: %s -> %s", book.get("title"), filepath.name)
            await self.api.download_file(file_info["downloadLink"], str(filepath))

            if not filepath.exists() or filepath.stat().st_size == 0:
                return DownloadResult(
                    entry=entry,
                    success=False,
                    message=f"下载失败（文件为空）: {entry.display_name}",
                )

            return DownloadResult(
                entry=entry,
                success=True,
                filepath=filepath,
                message=f"下载完成: {filepath.name}",
            )

        except ZLibAPIError as exc:
            return DownloadResult(
                entry=entry,
                success=False,
                message=f"API 错误: {exc}",
            )
        except Exception as exc:
            logger.exception("下载出错: %s", entry.display_name)
            return DownloadResult(
                entry=entry,
                success=False,
                message=f"下载异常: {exc}",
            )
        finally:
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)

    async def download_all(
        self,
        entries: list[BookEntry],
        skip_existing: bool = True,
        catalog_path: Path | None = None,
    ) -> list[DownloadResult]:
        results: list[DownloadResult] = []
        for i, entry in enumerate(entries, start=1):
            logger.info("[%d/%d] 处理: %s", i, len(entries), entry.display_name)
            result = await self.download_entry(entry, skip_existing=skip_existing)
            results.append(result)
            status = "✓" if result.success else "✗"
            logger.info("%s %s", status, result.message)

            if result.success:
                if catalog_path:
                    mark_entry_done(catalog_path, entry.line_no)
                remaining = await self.api.get_downloads_remaining()
                if remaining is not None and remaining <= 0:
                    logger.warning("今日下载额度已用完，停止后续下载")
                    break

        return results
