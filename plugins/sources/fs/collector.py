"""文件系统采集器：按 mtime 发现改动过的文件（只读）。语义见 INTERFACE.md §4、§5.2。"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from infohub import events as E
from infohub.sources import CollectorError

NODE = "fs"


def _walk(root: Path, ignore_dirs: set[str], ignore_globs: list[str]) -> list[tuple[float, str, int]]:
    found: list[tuple[float, str, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith(".")]
        for name in filenames:
            if any(fnmatch.fnmatch(name, pat) for pat in ignore_globs):
                continue
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            found.append((stat.st_mtime, full, stat.st_size))
    return found


def collect(config, store) -> list[dict]:
    roots = config.collector_get(NODE, "roots") or []
    max_files = int(config.collector_get(NODE, "max_files", 50))
    ignore_dirs = set(config.collector_get(NODE, "ignore_dirs", []) or [])
    ignore_globs = list(config.collector_get(NODE, "ignore_globs", []) or [])

    out: list[dict] = []
    for root_str in roots:
        root = Path(str(root_str)).expanduser()
        if not root.is_dir():
            raise CollectorError(f"fs 根目录不存在: {root}")
        key = f"{NODE}:{root}"
        state = store.state_get(key) or {}
        since = float((state.get("cursor") or {}).get("mtime") or 0.0)

        found = _walk(root, ignore_dirs, ignore_globs)
        changed = sorted((f for f in found if f[0] > since), key=lambda f: f[0], reverse=True)
        if not changed:
            store.state_put(key, ok=True)
            continue

        for mtime, full, size in changed[:max_files]:
            payload = {"root": str(root), "path": os.path.relpath(full, root),
                       "mtime": mtime, "size": size}
            out.append(E.make_event(NODE, "fs.change", payload,
                                    ts=E.iso_from_epoch(mtime), privacy="normal"))
        # 游标取"所有改动"的最大 mtime（不是被截断后的那批），避免漏掉文件
        store.state_put(key, cursor={"mtime": max(f[0] for f in changed)}, ok=True)
    return out
