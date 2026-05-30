"""书目录文件解析模块。"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class BookEntry:
    """一条待下载图书记录。"""

    title: str = ""
    author: str = ""
    extension: str = ""  # PDF, EPUB 等，空则不限
    book_id: str = ""  # 直接指定 Z-Library 书籍 ID
    query: str = ""  # 自定义搜索词，优先级高于 title+author
    line_no: int = 0

    @property
    def search_query(self) -> str:
        if self.query:
            return self.query
        if self.title and self.author:
            return f"{self.title} {self.author}"
        return self.title or self.author

    @property
    def display_name(self) -> str:
        if self.book_id:
            return f"id:{self.book_id}"
        parts = [self.title, self.author]
        label = " - ".join(p for p in parts if p)
        if self.extension:
            label += f" [{self.extension}]"
        return label or self.search_query


@dataclass
class Catalog:
    """解析后的书目录。"""

    entries: list[BookEntry] = field(default_factory=list)
    source: Path | None = None


def _normalize_extension(ext: str) -> str:
    ext = ext.strip().upper().lstrip(".")
    return ext


def _strip_done_marker(line: str) -> tuple[str, bool]:
    """去掉已完成标记，返回 (内容, 是否已完成)。"""
    stripped = line.strip()
    if stripped.startswith("~~") and stripped.endswith("~~") and len(stripped) > 4:
        return stripped[2:-2].strip(), True
    return stripped, False


def is_done_line(line: str) -> bool:
    _, done = _strip_done_marker(line)
    return done


def _normalize_md_line(line: str) -> str:
    """去掉 Markdown 列表、任务列表等常见前缀。"""
    stripped = line.strip()
    stripped = re.sub(r"^[-*+]\s+\[[ xX]\]\s+", "", stripped)
    stripped = re.sub(r"^[-*+]\s+", "", stripped)
    return stripped.strip()


def _parse_line(line: str, line_no: int) -> BookEntry | None:
    content, is_done = _strip_done_marker(line)
    if is_done:
        return None
    content = _normalize_md_line(content)
    if not content or content.startswith("#"):
        return None

    line = content
    # 直接指定书籍 ID: id:12345678
    id_match = re.match(r"^id\s*:\s*(\d+)\s*(?:\|\s*(.+))?$", line, re.I)
    if id_match:
        ext = _normalize_extension(id_match.group(2) or "")
        return BookEntry(book_id=id_match.group(1), extension=ext, line_no=line_no)

    # 管道分隔: 书名 | 作者 | 格式
    if "|" in line:
        parts = [p.strip() for p in line.split("|")]
        title = parts[0] if len(parts) > 0 else ""
        author = parts[1] if len(parts) > 1 else ""
        ext = _normalize_extension(parts[2]) if len(parts) > 2 else ""
        return BookEntry(title=title, author=author, extension=ext, line_no=line_no)

    # 逗号分隔: 书名, 作者, 格式
    if "," in line:
        parts = [p.strip() for p in line.split(",")]
        title = parts[0] if len(parts) > 0 else ""
        author = parts[1] if len(parts) > 1 else ""
        ext = _normalize_extension(parts[2]) if len(parts) > 2 else ""
        return BookEntry(title=title, author=author, extension=ext, line_no=line_no)

    # 单行搜索词
    return BookEntry(title=line, line_no=line_no)


def _parse_csv_row(row: dict[str, str], line_no: int) -> BookEntry | None:
    def get(*keys: str) -> str:
        for key in keys:
            val = row.get(key, "").strip()
            if val:
                return val
        return ""

    title = get("title", "书名", "name", "标题")
    author = get("author", "作者", "authors")
    extension = _normalize_extension(get("extension", "格式", "ext", "type"))
    book_id = get("book_id", "id", "书籍id")
    query = get("query", "搜索", "search")

    if not any([title, author, book_id, query]):
        return None

    return BookEntry(
        title=title,
        author=author,
        extension=extension,
        book_id=book_id,
        query=query,
        line_no=line_no,
    )


def _looks_like_csv_header(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    # 管道分隔格式不走 CSV
    if "|" in stripped:
        return False
    if "," not in stripped:
        return False
    fields = {f.strip().lower() for f in stripped.split(",")}
    keywords = {
        "title", "author", "书名", "作者", "extension", "book_id",
        "query", "搜索", "ext", "type", "name", "标题",
    }
    return bool(fields & keywords)


def _content_lines(lines: list[str]) -> list[str]:
    """跳过空行和注释，返回有效内容行。"""
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            result.append(line)
    return result


def load_catalog(path: Path) -> Catalog:
    """从文本或 CSV 文件加载书目录。"""
    if not path.exists():
        raise FileNotFoundError(f"书目录文件不存在: {path}")

    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    entries: list[BookEntry] = []

    content = _content_lines(lines)
    if content and _looks_like_csv_header(content[0]):
        reader = csv.DictReader(lines)
        for i, row in enumerate(reader, start=2):
            if not row:
                continue
            entry = _parse_csv_row(row, i)
            if entry:
                entries.append(entry)
    else:
        for i, line in enumerate(lines, start=1):
            entry = _parse_line(line, i)
            if entry:
                entries.append(entry)

    return Catalog(entries=entries, source=path)


def iter_entries(catalog: Catalog) -> Iterator[BookEntry]:
    yield from catalog.entries


def mark_entry_done(path: Path, line_no: int) -> None:
    """在书目录文件中为指定行添加 ~~删除线~~ 标记。"""
    if line_no < 1:
        return

    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    idx = line_no - 1
    if idx >= len(lines):
        return

    raw_line = lines[idx]
    content, already_done = _strip_done_marker(raw_line)
    if already_done:
        return

    prefix = raw_line[: len(raw_line) - len(raw_line.lstrip())]
    body = content.strip()
    if not body or body.startswith("#"):
        return

    list_prefix = ""
    rest = body
    list_match = re.match(r"^([-*+]\s+(?:\[[ xX]\]\s+)?)", body)
    if list_match:
        list_prefix = list_match.group(1)
        rest = body[len(list_match.group(0)):].strip()

    lines[idx] = f"{prefix}{list_prefix}~~{rest}~~"
    trailing_newline = "\n" if text.endswith("\n") else ""
    path.write_text("\n".join(lines) + trailing_newline, encoding="utf-8")

