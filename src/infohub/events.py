"""事件构造与指纹。字段语义见 INTERFACE.md §5。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

SEP = "\x1f"


def canonical(obj: Any) -> str:
    """规范化 JSON：键排序、无多余空白、保留中文。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(source: str, kind: str, payload: Any) -> str:
    raw = SEP.join([source, kind, canonical(payload)]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def iso(ts_ms: int | float | None = None) -> str:
    """毫秒时间戳 → ISO8601（带本地时区）；缺省取当前时刻。"""
    if ts_ms is None:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    return datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def iso_from_epoch(ts_s: float) -> str:
    return datetime.fromtimestamp(float(ts_s)).astimezone().isoformat(timespec="seconds")


def make_event(source: str, kind: str, payload: dict, ts: str | None = None,
               privacy: str = "normal") -> dict:
    payload = payload if isinstance(payload, dict) else {"value": payload}
    return {
        "ts": ts or iso(),
        "source": source,
        "kind": kind,
        "payload": payload,
        "fingerprint": fingerprint(source, kind, payload),
        "privacy": privacy,
    }


def head_from_content(content: Any, limit: int) -> str:
    """从 DSH 的 content 数组里取文本首段（INTERFACE.md §5.2 的 head）。"""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        if sum(len(p) for p in parts) >= limit:
            break
    return " ".join(parts)[:limit]


def brief(value: Any, limit: int) -> str:
    """兜底摘要：把任意结构压成一行短文本。"""
    if isinstance(value, str):
        return value[:limit]
    try:
        return canonical(value)[:limit]
    except (TypeError, ValueError):
        return str(value)[:limit]
