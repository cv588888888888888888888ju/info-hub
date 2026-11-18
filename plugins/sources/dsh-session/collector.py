"""DSH 会话采集器：读会话存档（只读）。字段与游标语义见 INTERFACE.md §4、§5.2。"""

from __future__ import annotations

import json
from pathlib import Path

from infohub import events as E
from infohub.sources import CollectorError

NODE = "dsh-session"


def _head(data: dict, limit: int) -> str:
    """按事件类型取"前 N 字"（DSH 事件结构可能随版本变化，故逐路径兜底）。"""
    if not isinstance(data, dict):
        return ""
    for path in (("message", "content"), ("content",)):
        node = data
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        text = E.head_from_content(node, limit)
        if text:
            return text
    inserted = data.get("inserted")
    if isinstance(inserted, list):
        parts = [E.head_from_content(ins.get("content"), limit)
                 for ins in inserted if isinstance(ins, dict)]
        text = " ".join(p for p in parts if p)
        if text:
            return text[:limit]
    return ""


def collect(config, store) -> list[dict]:
    root = Path(config.get("sessions_root") or "").expanduser()
    if not root.is_dir():
        raise CollectorError(f"sessions_root 不存在: {root}")
    try:
        from compression import zstd  # Python 3.14+
    except ImportError as exc:  # pragma: no cover
        raise CollectorError("compression.zstd 不可用（需要 Python 3.14+）") from exc

    max_sessions = int(config.collector_get(NODE, "max_sessions", 60))
    tail = int(config.collector_get(NODE, "tail_events_per_session", 400))
    head_chars = int(config.collector_get(NODE, "head_chars", 200))

    try:
        files = sorted(root.glob("*/*/session.v4.jsonl.zstd"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:max_sessions]
    except OSError as exc:
        raise CollectorError(f"枚举会话存档失败: {exc}") from exc

    out: list[dict] = []
    for path in files:
        key = f"{NODE}:{path.parent.name}"
        try:
            stat = path.stat()
            state = store.state_get(key) or {}
            cursor = state.get("cursor") or {}
            if cursor.get("mtime") == stat.st_mtime:
                continue
            lines = zstd.decompress(path.read_bytes()).decode("utf-8", "replace").splitlines()
        except Exception as exc:  # 单文件失败不拖垮其它会话
            store.state_put(key, ok=False, error=f"{type(exc).__name__}: {exc}")
            continue

        header: dict = {}
        events_start = 0
        if lines:
            try:
                header = json.loads(lines[0])
                events_start = 1 if isinstance(header, dict) and header.get("type") == "session" else 0
            except ValueError:
                header, events_start = {}, 0

        prev_lines = int(cursor.get("lines") or 0)
        start = prev_lines if 0 < prev_lines <= len(lines) else max(events_start, len(lines) - tail)
        session_id = header.get("id") or path.parent.name
        cwd = header.get("cwd")

        for line in lines[start:]:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("type") or "unknown"
            head = _head(item.get("data") or {}, head_chars)
            payload = {"session_id": session_id, "seq": item.get("seq"), "cwd": cwd,
                       "event_type": kind, "head": head}
            ts = E.iso(item.get("time")) if item.get("time") else E.iso_from_epoch(stat.st_mtime)
            out.append(E.make_event(NODE, str(kind), payload, ts=ts,
                                    privacy="sensitive" if head else "normal"))

        store.state_put(key, cursor={"mtime": stat.st_mtime, "lines": len(lines)}, ok=True)
    return out
