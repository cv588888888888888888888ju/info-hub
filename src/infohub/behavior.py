"""转译层（M10）：把 activity 采样翻成「片段」。语义见 INTERFACE.md §4.15、§5.9。

这里是纯函数：同一批采样 + 同一套口径 ⇒ 同一批片段（逐字节相同），所以可以随时全量重算，
算错也不怕 —— events 是原账本，片段只是派生视图。

只回答三个问题：什么时候在干活、这一段多久、这一段专注度多少。
**专注度不是心理学判断**：它是「在场比例 / 窗口稳定度 / 空档」三项的加权，不读屏幕内容，
所以它分不清「写代码」和「刷视频」。对外必须带上这句。
"""

from __future__ import annotations

from datetime import datetime, timedelta

WEIGHTS = {"presence": 0.5, "stability": 0.3, "continuity": 0.2}


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def samples(events: list[dict], idle_active_s: float | None = None) -> list[dict]:
    """从 events 里挑出 activity 采样，按时间升序（同 ts 保持原序，保证可重算）。

    `idle_active_s` 给了就用**采样里存的 `idle_s` 重新判定在场** —— 这样改门槛是「重算」而不是
    「重新采一天」（契约见 INTERFACE.md §4.15）。老事件没有 `idle_s` 时退回读 payload 里的 `active`。
    """
    out = []
    for ev in events:
        if ev.get("source") != "activity" or ev.get("kind") != "activity.sample":
            continue
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            continue
        idle = payload.get("idle_s")
        if idle_active_s is not None and isinstance(idle, (int, float)):
            active = float(idle) < float(idle_active_s)
        else:
            active = bool(payload.get("active"))
        out.append({"ts": ev["ts"], "epoch": _epoch(ev["ts"]),
                    "active": active,
                    "frame_hash": payload.get("frame_hash")})
    out.sort(key=lambda s: s["epoch"])
    return out


def segment(items: list[dict], *, interval_s: int = 60, gap_s: int = 300,
            min_active_s: int = 60, switch_per_min_full: float = 1.0,
            weights: dict | None = None, def_version: str = "ep-1") -> list[dict]:
    """切片段。规则与公式逐条对应 INTERFACE.md §4.15，改公式必须换 def_version。"""
    w = dict(WEIGHTS)
    w.update({k: float(v) for k, v in (weights or {}).items() if k in WEIGHTS})

    groups: list[list[dict]] = []
    cur: list[dict] = []
    prev = None
    for item in items:
        if prev is not None and item["epoch"] - prev["epoch"] > gap_s:
            if cur:
                groups.append(cur)
            cur = []
        cur.append(item)
        if not item["active"]:        # 离开的采样直接收段
            groups.append(cur)
            cur = []
        prev = item
    if cur:
        groups.append(cur)

    rows: list[dict] = []
    for group in groups:
        actives = sum(1 for s in group if s["active"])
        active_s = actives * interval_s
        if active_s < min_active_s:   # 太短的段不落库（开机、瞥一眼）
            continue
        span_s = int(round(group[-1]["epoch"] - group[0]["epoch"])) + interval_s
        gaps = [group[i + 1]["epoch"] - group[i]["epoch"] for i in range(len(group) - 1)]
        gap_max_s = int(round(max(gaps))) if gaps else 0
        switches = sum(1 for i in range(len(group) - 1)
                       if group[i + 1]["frame_hash"] != group[i]["frame_hash"])

        presence = min(1.0, active_s / span_s) if span_s else 0.0
        capacity = (span_s / 60.0) * float(switch_per_min_full)
        stability = 1.0 - min(1.0, switches / capacity) if capacity > 0 else 1.0
        # 连续性分母取「本段长度的一成」与 gap_s 的较大者（ep-3）：
        # 固定 300s 时，一次 5 分钟起身会把 3 小时的一段直接判成 0，与「专注」相反。
        denom = max(float(gap_s), span_s * 0.1)
        continuity = 1.0 - min(1.0, gap_max_s / denom) if denom > 0 else 1.0
        focus = int(round(100 * (w["presence"] * presence + w["stability"] * stability
                                 + w["continuity"] * continuity)))

        started = datetime.fromtimestamp(group[0]["epoch"]).astimezone()
        ended = datetime.fromtimestamp(group[-1]["epoch"] + interval_s).astimezone()
        rows.append({
            "day": started.strftime("%Y-%m-%d"),
            "started_at": started.isoformat(timespec="seconds"),
            "ended_at": ended.isoformat(timespec="seconds"),
            "active_s": active_s, "span_s": span_s,
            "presence": round(presence, 4), "stability": round(stability, 4),
            "continuity": round(continuity, 4), "focus": focus,
            "switch_count": switches, "gap_max_s": gap_max_s,
            "sample_count": len(group), "def_version": def_version,
        })
    return rows


def day_bounds(day: str) -> tuple[str, str]:
    """本地日 [00:00, 次日00:00) 的 ISO 边界（带时区，供 events 范围查询）。"""
    start = datetime.fromisoformat(day).astimezone()
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def hhmm(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone().strftime("%H:%M")


def minutes(seconds: float) -> int:
    return int(round(seconds / 60.0))


def summarize(rows: list[dict], *, day_start_s: int = 600) -> dict:
    """白话汇总（`bin/hub-activity day` 用）：第一次开始干活、在场合计、平均专注度。"""
    ordered = sorted(rows, key=lambda r: r["started_at"])
    total_active = sum(r["active_s"] for r in ordered)
    start_at = None
    acc = 0
    for row in ordered:
        acc += row["active_s"]
        if acc >= day_start_s:
            start_at = ordered[0]["started_at"]
            break
    avg = (int(round(sum(r["focus"] for r in ordered) / len(ordered))) if ordered else None)
    return {"episodes": len(ordered), "active_s": total_active, "active_min": minutes(total_active),
            "start_at": start_at, "avg_focus": avg,
            "longest": max(ordered, key=lambda r: r["active_s"]) if ordered else None}

def clean_minutes(rows: list[dict], game_by_hour: dict[int, float], *,
                  threshold: float = 30.0) -> dict:
    """把「手机游戏在前台够久」的那部分时间，从电脑前的在场分钟里扣掉（M18 排除口径）。

    `game_by_hour` 是 {小时: 该小时游戏在前台分钟}；`threshold` 以下不算「在玩」，只按比例扣，
    超过阈值就整小时按比例扣 —— 这样既不会因为挂 2 分钟误杀，也不会漏掉「挂一整小时」。
    """
    game_by_hour = {int(k): float(v) for k, v in (game_by_hour or {}).items() if v is not None}
    total = overlap = 0.0
    for r in rows:
        total += r["active_s"] / 60.0
        start = datetime.fromisoformat(str(r["started_at"])).timestamp()
        end = datetime.fromisoformat(str(r["ended_at"])).timestamp()
        cur = start
        while cur < end:
            hour_start = datetime.fromtimestamp(cur).replace(minute=0, second=0, microsecond=0)
            seg_end = min(end, hour_start.timestamp() + 3600)
            g = game_by_hour.get(hour_start.hour, 0.0)
            if g > 0:
                overlap += (seg_end - cur) / 60.0 * min(1.0, g / 60.0)
            cur = seg_end
    return {"active": int(round(total)), "game": int(round(overlap)),
            "clean": int(round(total - overlap)), "threshold": threshold}
