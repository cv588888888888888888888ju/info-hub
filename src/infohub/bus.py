"""事件 spool（JSONL）。语义见 INTERFACE.md §6「spool 读取后入库失败」：先入库、成功后才截断。"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from .events import canonical


class Bus:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, events: list[dict]) -> int:
        if not events:
            return 0
        with self.path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                for ev in events:
                    fh.write(canonical(ev) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return len(events)

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        with self.path.open("r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return out

    def truncate(self) -> None:
        """只在 spool 内容全部入库（含重复计数）后调用。"""
        with self.path.open("w", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.truncate(0)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0
