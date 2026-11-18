"""浏览器历史采集器（M5b）：本机 Edge/Chrome/Chromium 的 History → 仓库页/视频页事件。

与 `collectors/bilibili.py` 同一套路：游标 = 见过的条目 id（只留最近 `keep_ids` 个），
失败 ⇒ `CollectorError`（不影响其它采集器）。条目形态与事件语义见 INTERFACE.md §4.12。
"""

from __future__ import annotations

from infohub import browserhist as BH
from infohub import events as E
from infohub.sources import CollectorError

NODE = "browser-history"


def collect(config, store) -> list[dict]:
    cfg = config.collector(NODE)
    if not cfg.get("enabled"):
        return []
    try:
        db = BH.db_path(cfg)
        items = BH.collect_items(db=db,
                                 since_days=float(cfg.get("since_days") or 14),
                                 max_items=int(cfg.get("max_items") or 200),
                                 kinds=cfg.get("kinds") or BH.KINDS,
                                 title_limit=int(cfg.get("title_chars") or 120))
    except BH.HistError as exc:
        raise CollectorError(str(exc)) from exc

    keep_ids = int(cfg.get("keep_ids") or 5000)
    key = f"{NODE}:items"
    state = store.state_get(key) or {}
    seen = set((state.get("cursor") or {}).get("ids") or [])

    out: list[dict] = []
    fresh: list[str] = []
    for item in items:
        iid = str(item.get("id") or "").strip()
        if not iid or iid in seen:
            continue
        fresh.append(iid)
        out.append(E.make_event(NODE, f"browser.{item['kind']}", {
            "id": iid,
            "kind": item["kind"],
            "title": str(item.get("title") or "")[:200],
            "url": str(item.get("url") or "")[:500],
            "desc": str(item.get("desc") or "")[:500],
            "site": str(item.get("site") or ""),
            "purpose": str(item.get("purpose") or ""),
            "visits": int(item.get("visits") or 0),
            "source_kind": "history",
        }, ts=item.get("ts"), privacy="sensitive"))

    if fresh or out:
        store.state_put(key, cursor={"ids": (fresh + sorted(seen))[:keep_ids]}, ok=True)
    return out
