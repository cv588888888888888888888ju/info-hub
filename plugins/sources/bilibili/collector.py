"""bilibili 收藏导出采集器（M5，**默认关**）：把收藏/稍后再看里的**新条目**变成事件。

注意：M5b 起实际来源是 `collectors/browser_history.py`（读本机浏览器 History）。
这条通路只在显式配 `collect.bilibili.source` 时才产数据，当前 `enabled: false`。

取数走 `collect.bilibili`（命令或手动导出文件），游标 = 见过的条目 id（只留最近 N 个）。
失败（命令不存在/超时/非 JSON）⇒ `CollectorError`，与本层其它采集器同口径：不影响主路。
语义见 INTERFACE.md §4、§4.11。
"""

from __future__ import annotations

from infohub import bili, events as E
from infohub.sources import CollectorError

NODE = "bilibili"


def collect(config, store) -> list[dict]:
    cfg = config.collector(NODE)
    if not cfg.get("enabled"):
        return []
    try:
        items = bili.fetch(cfg)
    except bili.BiliError as exc:
        raise CollectorError(str(exc)) from exc

    max_items = int(cfg.get("max_items") or 100)
    keep_ids = int(cfg.get("keep_ids") or 1000)
    key = f"{NODE}:items"
    state = store.state_get(key) or {}
    seen = set((state.get("cursor") or {}).get("ids") or [])

    out: list[dict] = []
    fresh: list[str] = []
    for item in items[:max_items]:
        iid = str(item.get("id") or "").strip()
        if not iid or iid in seen:
            continue
        fresh.append(iid)
        out.append(E.make_event(NODE, "bili.item", {
            "id": iid,
            "title": str(item.get("title") or "")[:200],
            "url": str(item.get("url") or "")[:500],
            "desc": str(item.get("desc") or "")[:500],
            "source_kind": str(item.get("kind") or cfg.get("kind") or "unknown"),
        }, ts=item.get("ts"), privacy="normal"))

    if fresh or out:
        store.state_put(key, cursor={"ids": (fresh + sorted(seen))[:keep_ids]}, ok=True)
    return out
