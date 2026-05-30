# Z-Library 批量下载工具

从本地书目录文件读取待下载列表，自动搜索并下载 Z-Library 电子书到本地。

## 前置要求

- Python 3.10+
- Z-Library 账号（需 singlelogin 账号）

## 安装

```bash
cd z-lib
pip install -r requirements.txt
cp .env.example .env
cp books.md.example books.md
```

编辑 `.env` 填入账号信息：

```env
ZLIB_EMAIL=your_email@example.com
ZLIB_PASSWORD=your_password
DOWNLOAD_DIR=./downloads
```

## 书目录文件格式

编辑 `books.md`，每行一本书（也支持 Markdown 列表）：

```markdown
# 注释行

# 仅书名
深入理解计算机系统

# 书名 | 作者 | 格式
算法导论 | Thomas H. Cormen | PDF
- 三体 | 刘慈欣 | EPUB

# 逗号分隔
人类简史, 尤瓦尔·赫拉利, EPUB

# 直接指定书籍 ID（从 Z-Library 网页 URL 获取）
id:12345678

# 下载成功后自动加删除线，下次跳过
~~算法导论 | Thomas H. Cormen | PDF~~
```

也支持 CSV 格式（带表头）：

```csv
title,author,extension
深入理解计算机系统,,PDF
三体,刘慈欣,EPUB
```

## 使用

```bash
# 默认读取 books.md，下载到 ./downloads
python downloader.py

# 指定书目录和输出目录
python downloader.py my_books.md -o ~/Books

# 强制重新下载（不跳过已存在文件）
python downloader.py --force

# 调整下载间隔（秒），避免触发限流
python downloader.py --delay 3

# 详细日志
python downloader.py -v
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `catalog` | 书目录文件路径，默认 `books.md` |
| `-o, --output` | 下载目录 |
| `--email` | 账号邮箱（覆盖 .env） |
| `--password` | 账号密码（覆盖 .env） |
| `--domain` | Z-Library 域名，默认 `z-library.sk` |
| `--delay` | 每次下载间隔秒数，默认 2 |
| `--force` | 强制重新下载 |
| `-v, --verbose` | 详细日志 |

## 工作原理

1. 解析书目录文件，生成待下载列表
2. 通过 Z-Library `/eapi/` JSON 接口登录、搜索、下载
3. 对每条记录搜索并智能匹配最佳结果（按书名、作者、格式打分）
4. 下载文件到本地，已存在的文件自动跳过
5. 检测每日下载额度，额度用完时自动停止

## 注意事项

- 请遵守 Z-Library 的使用条款和当地法律法规
- 免费账号有每日下载次数限制
- 若搜索匹配不准确，建议使用 `id:书籍ID` 直接指定
- 下载失败的条目会在结束时汇总显示

## 项目结构

```
z-lib/
├── downloader.py       # CLI 入口
├── books.md.example    # 书目录示例
├── .env.example        # 配置示例
├── requirements.txt
└── src/
    ├── catalog.py      # 书目录解析
    └── client.py       # Z-Library 客户端
```
