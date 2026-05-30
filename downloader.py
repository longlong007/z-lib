#!/usr/bin/env python3
"""Z-Library 批量下载工具。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.catalog import load_catalog
from src.client import ZLibClient


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从书目录文件批量下载 Z-Library 电子书",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
书目录文件格式示例:

  # 注释行以 # 开头
  深入理解计算机系统
  算法导论 | Thomas H. Cormen | PDF
  三体, 刘慈欣, EPUB
  id:12345678
  id:12345678 | EPUB

CSV 格式（带表头）:
  title,author,extension
  深入理解计算机系统,,PDF
  三体,刘慈欣,EPUB
        """,
    )
    parser.add_argument(
        "catalog",
        nargs="?",
        default="books.md",
        help="书目录文件路径（默认: books.md）",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="下载目录（默认读取 .env 中 DOWNLOAD_DIR 或 ./downloads）",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Z-Library 邮箱（默认读取 .env 中 ZLIB_EMAIL）",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Z-Library 密码（默认读取 .env 中 ZLIB_PASSWORD）",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="Z-Library 域名（默认读取 .env 中 ZLIB_DOMAIN 或 z-library.sk）",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="每次下载间隔秒数（默认: 2.0）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新下载，不跳过已存在文件",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细日志",
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    load_dotenv()

    email = args.email or os.getenv("ZLIB_EMAIL", "")
    password = args.password or os.getenv("ZLIB_PASSWORD", "")
    if not email or not password:
        logging.error("请配置 Z-Library 账号：复制 .env.example 为 .env 并填写 ZLIB_EMAIL / ZLIB_PASSWORD")
        return 1

    catalog_path = Path(args.catalog)
    try:
        catalog = load_catalog(catalog_path)
    except FileNotFoundError as exc:
        logging.error("%s", exc)
        logging.info("可参考 books.md.example 创建书目录文件")
        return 1

    if not catalog.entries:
        logging.error("书目录为空: %s", catalog_path)
        return 1

    download_dir = Path(
        args.output or os.getenv("DOWNLOAD_DIR", "./downloads")
    ).resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    logging.info("书目录: %s（共 %d 本）", catalog_path, len(catalog.entries))
    logging.info("下载目录: %s", download_dir)

    client = ZLibClient(
        email=email,
        password=password,
        download_dir=download_dir,
        domain=args.domain or os.getenv("ZLIB_DOMAIN", "z-library.sk"),
        delay_seconds=args.delay,
    )

    try:
        await client.login()
        results = await client.download_all(
            catalog.entries,
            skip_existing=not args.force,
            catalog_path=catalog_path,
        )
    finally:
        await client.close()

    success = sum(1 for r in results if r.success)
    failed = len(results) - success
    logging.info("=" * 40)
    logging.info("完成: 成功 %d, 失败 %d, 共 %d", success, failed, len(results))

    if failed:
        logging.info("失败列表:")
        for r in results:
            if not r.success:
                logging.info("  - [%d] %s: %s", r.entry.line_no, r.entry.display_name, r.message)

    return 0 if failed == 0 else 2


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)
    try:
        exit_code = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logging.info("用户中断")
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
