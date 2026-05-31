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

# macOS 单文件名上限 255 字节，留余量给扩展名
MAX_FILENAME_LEN = 180
MIN_MATCH_SCORE = 75


@dataclass
class DownloadResult:
    entry: BookEntry
    success: bool
    filepath: Path | None = None
    message: str = ""


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "unknown"


def _truncate_filename(filename: str, max_len: int = MAX_FILENAME_LEN) -> str:
    """按字符截断，避免超出文件系统限制。"""
    if len(filename) <= max_len:
        return filename
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        return filename[:max_len]
    room = max_len - len(ext) - 1
    if room < 10:
        return filename[:max_len]
    return f"{stem[:room]}.{ext}"


def _first_author(author: str) -> str:
    if not author:
        return ""
    return re.split(r"[,、;/]", author)[0].strip()


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


def _title_score(entry_title: str, book_title: str) -> float:
    et = _normalize_match_text(entry_title)
    bt = _normalize_match_text(book_title)
    if not et:
        return 0.0
    if et == bt:
        return 100.0
    if et in bt or bt in et:
        return 80.0
    # 中文书名：前缀匹配
    if any("\u4e00" <= c <= "\u9fff" for c in et):
        probe = et[: min(len(et), 12)]
        if len(probe) >= 2 and probe in bt:
            return 75.0
    words = [w for w in et.split() if len(w) >= 3]
    if not words:
        return 50.0 if et in bt else 0.0
    matched = sum(1 for w in words if w in bt)
    ratio = matched / len(words)
    if ratio >= 0.5:
        return 70.0 * ratio
    return 0.0


def _author_score(entry_author: str, book_author: str) -> float:
    author_text = book_author.lower()
    for part in re.split(r"[,、/]", entry_author):
        part = part.strip().lower()
        if not part or len(part) <= 2:
            continue
        if part in author_text:
            return 40.0
        for word in part.split():
            if len(word) >= 3 and word in author_text:
                return 35.0
    return 0.0


def _score_match(entry: BookEntry, book: dict[str, Any]) -> float:
    book_title = book.get("title") or book.get("name") or ""
    book_author = book.get("author") or ""

    title_pts = _title_score(entry.title, book_title) if entry.title else 0.0
    author_pts = _author_score(entry.author, book_author) if entry.author else 0.0

    # 有书名时，作者分仅在书名有一定匹配时才计入（避免仅作者同名误匹配）
    if entry.title and title_pts < 30:
        author_pts = 0.0

    score = title_pts + author_pts

    ext = (book.get("extension") or "").upper()
    if entry.extension and ext == entry.extension.upper().lstrip("."):
        score += 20

    try:
        score += float(book.get("qualityScore") or 0) * 2
    except (ValueError, TypeError):
        pass

    return score


def _is_acceptable_match(entry: BookEntry, book: dict[str, Any], score: float) -> bool:
    if score < MIN_MATCH_SCORE:
        return False
    if entry.title:
        title_pts = _title_score(entry.title, book.get("title") or book.get("name") or "")
        if title_pts < 30:
            return False
    return True


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

    async def _search_books(self, entry: BookEntry) -> list[dict[str, Any]]:
        """尝试多种搜索词，合并去重结果。"""
        extension = entry.extension or ""
        queries: list[str] = []
        if entry.search_query:
            queries.append(entry.search_query)
        if entry.title and entry.title not in queries:
            queries.append(entry.title)

        seen_ids: set[str] = set()
        results: list[dict[str, Any]] = []

        for query in queries:
            books: list[dict[str, Any]] = []
            try:
                books = await self.api.search(query, extension=extension, limit=25)
            except Exception as exc:
                logger.warning("搜索 '%s' 失败: %s", query, exc)
                if entry.title and query != entry.title:
                    try:
                        books = await self.api.search(entry.title, extension=extension, limit=25)
                    except Exception as exc2:
                        logger.warning("书名搜索 '%s' 也失败: %s", entry.title, exc2)
                        continue
                else:
                    continue

            if not books and extension:
                try:
                    books = await self.api.search(query, limit=25)
                except Exception:
                    continue

            for book in books:
                bid = str(book.get("id", ""))
                if bid and bid not in seen_ids:
                    seen_ids.add(bid)
                    results.append(book)

        return results

    async def _find_best_match(self, entry: BookEntry) -> dict[str, Any] | None:
        if entry.book_id:
            book = await self.api.get_book_by_id(entry.book_id)
            return book

        if not entry.search_query and not entry.title:
            return None

        books = await self._search_books(entry)
        if not books:
            return None

        ranked = sorted(
            ((b, _score_match(entry, b)) for b in books),
            key=lambda x: x[1],
            reverse=True,
        )
        best_book, best_score = ranked[0]
        if not _is_acceptable_match(entry, best_book, best_score):
            logger.warning(
                "最佳匹配置信度不足 (score=%.0f): 期望 [%s] -> 命中 [%s]",
                best_score,
                entry.display_name,
                best_book.get("title"),
            )
            return None

        return best_book

    def _build_filepath(
        self,
        entry: BookEntry,
        book: dict[str, Any],
        file_info: dict[str, Any] | None = None,
    ) -> Path:
        ext = (
            (file_info or {}).get("extension")
            or book.get("extension")
            or entry.extension
            or "bin"
        )
        ext = str(ext).lower().lstrip(".")

        # 优先用书目录中的短书名/作者，避免 API 元数据过长
        name = entry.title or (file_info or {}).get("description") or book.get("title") or "unknown"
        author = entry.author or _first_author((file_info or {}).get("author") or book.get("author") or "")

        name = _sanitize_filename(name)[:80]
        author = _sanitize_filename(_first_author(author))[:40]

        if author:
            filename = _truncate_filename(f"{name} - {author}.{ext}")
        else:
            filename = _truncate_filename(f"{name}.{ext}")

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
            filepath = self._build_filepath(entry, book, file_info)

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
