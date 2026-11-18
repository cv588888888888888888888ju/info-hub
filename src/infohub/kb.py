"""知识库全文检索（M5）：给「我看到的这个工具，整合进 KB 了吗」一个**机械**答案。

纪律（INTERFACE.md §4.11）：
- **只读**，绝不写 KB、不建索引文件（KB 是权威层，见 AGENTS.md §六）；
- 纯 stdlib：按后缀白名单遍历 + 大小写不敏感子串匹配；KB 实测 125 个文本文件 / 1.5 MB，够快；
- **fail-closed**：KB 根不存在/不可读 ⇒ 抛 `KbError`，调用方必须把这次判定当「查不出来」，
  绝不当成「KB 里没有」（否则会把用户已整合的工具误报成没整合）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

#: 只读这些后缀（KB 里其它东西是附件/图片/git 对象）
TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json", ".csv", ".org")
#: 跳过这些目录
SKIP_DIRS = {".git", ".obsidian", "node_modules", "__pycache__", ".venv", "venv",
             ".trash", ".mypy_cache", ".pytest_cache"}
#: 单文件大小上限（超过就不读，避免误读大附件）
MAX_FILE_BYTES = 4_000_000


class KbError(Exception):
    """KB 不可读（≠ KB 里没有）。"""


def text_files(root) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        raise KbError(f"KB 根不存在: {root}")
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.lower().endswith(TEXT_SUFFIXES):
                out.append(Path(dirpath) / name)
    return out


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def search(root, needle: str, limit: int = 5, word: bool = False) -> list[dict]:
    """在 KB 里找 `needle`（大小写不敏感）。返回命中 `[{path, line, text}]`，最多 `limit` 条。

    `word=True` 时要求词边界：两侧都不能紧贴 `[A-Za-z0-9_.-]`（`/` 与空格算边界，
    所以 `owner/tool` 里能命中 `tool`，而 `cat` 不会命中 `category`、`present` 不会命中
    `present-tool`）。
    """
    needle = str(needle or "").strip()
    if len(needle) < 3:
        return []
    root = Path(root)
    if not root.is_dir():
        raise KbError(f"KB 根不存在: {root}")
    pattern = re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(needle) + r"(?![A-Za-z0-9_.-])", re.I) if word \
        else re.compile(re.escape(needle), re.I)
    hits: list[dict] = []
    for path in text_files(root):
        text = _read(path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append({"path": str(path.relative_to(root)),
                             "line": lineno, "text": line.strip()[:200]})
                break                      # 每个文件只报第一条，够判断"有没有"
        if len(hits) >= limit:
            break
    return hits


def contains(root, needle: str, word: bool = False) -> tuple[bool, str | None]:
    """`(有没有, 命中文件相对路径)`。KB 不可读时抛 `KbError`（不返回 False）。"""
    hits = search(root, needle, limit=1, word=word)
    return (True, hits[0]["path"]) if hits else (False, None)
