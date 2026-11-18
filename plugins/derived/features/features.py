"""M16 行为特征层：把散落的事件翻译成「一小时一行」的关于人的指标。

契约见 INTERFACE.md §4.22 / §5.15。

这是 m00113 的落点：原始记录 → 行为特征（何时开始干活 / 专注程度 / 多久）。
本模块**只读事件库**，纯函数 + 一次写表；同输入两次 build 逐字节相同。
缺数据的列写 None（NULL），不写 0 冒充测量值 —— 这是契约。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

VERSION = "feat-1"
COVERAGE_KEYS = ("presence", "facing", "input", "window", "phone", "vision")
PHONE_GAP_S = 300            # 手机通知间隔 ≤5 分钟视为「还在用手机」


def _hour_of(ts: str) -> int:
    return int(str(ts)[11:13])


def _day_events(store, source: str, day: str, limit: int = 200000) -> list[dict]:
    start = f"{day}T00:00:00"
    rows = store.events(source=source, since=start, limit=limit)
    return [r for r in rows if str(r["ts"])[:10] == day]


def _phone_minutes(ts_list: list[str]) -> int:
    """通知时间戳 → 手机使用分钟（下界：间隔 ≤PHONE_GAP_S 算连续，段长取首末差）。"""
    if not ts_list:
        return 0
    stamps = sorted(datetime.fromisoformat(t).timestamp() for t in ts_list)
    total, seg_start, last = 0.0, stamps[0], stamps[0]
    for t in stamps[1:]:
        if t - last <= PHONE_GAP_S:
            last = t
        else:
            total += last - seg_start
            seg_start = last = t
    total += last - seg_start
    return int(round(total / 60.0))


def _weighted_mean(rows: list[dict], key: str, weight_key: str = "keys") -> float | None:
    pairs = [(r[key], r.get(weight_key, 0)) for r in rows if r.get(key) is not None]
    num = sum(v * (w or 1) for v, w in pairs)
    den = sum((w or 1) for _, w in pairs)
    return round(num / den, 3) if den else None


def build(store, day: str, version: str = VERSION) -> list[dict]:
    """算一天 24 行特征。返回写进库的那些行（含 hour）。"""
    acts = _day_events(store, "activity", day)
    cams = _day_events(store, "camera", day)
    ins = _day_events(store, "input", day)
    wins = _day_events(store, "window", day)
    phones = _day_events(store, "phone-hub", day)
    shots = _day_events(store, "screenshot", day)
    usages = _day_events(store, "phone-usage", day)
    # 逐小时游戏事件按「小时键」归属，不按事件自身 ts 归属（它跨零点）
    game_events = [r for r in store.events(source="phone-usage", limit=500)
                   if str(r["kind"]) == "phone.usage.hours"]

    label_by_sha = {r["sha256"]: r["category"] for r in store.shot_labels(day, limit=5000)}

    # M18：逐小时游戏分钟（多次快照取每小时最大值，避免重复累加）
    game_by_hour: dict[int, float] = {}
    for ev in game_events:
        p = ev["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                continue
        if not isinstance(p, dict):
            continue
        for key, apps in (p.get("hours") or {}).items():
            if not str(key).startswith(day):
                continue
            try:
                h = int(str(key)[11:13])
            except ValueError:
                continue
            val = sum(float(v) for v in (apps or {}).values() if isinstance(v, (int, float)))
            game_by_hour[h] = max(game_by_hour.get(h, 0.0), val)

    # M17：手机屏幕日累计（取当天最后一条快照；整天每行同值，没有快照就 NULL）
    phone_day_min = None
    phone_day_top = None
    if usages:
        latest = max(usages, key=lambda r: str(r["ts"]))
        p = latest["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = None
        if isinstance(p, dict) and p.get("total_min") is not None:
            phone_day_min = int(round(float(p["total_min"])))
            pk = p.get("packages") or []
            if pk and isinstance(pk[0], dict):
                phone_day_top = pk[0].get("app")

    def payloads(rows):
        out = []
        for r in rows:
            p = r["payload"]
            if isinstance(p, str):              # store.events 正常已解析；字符串兜底
                try:
                    p = json.loads(p)
                except Exception:
                    continue
            if isinstance(p, dict):
                out.append((r["ts"], p))
        return out

    buckets = {h: {"activity": [], "camera": [], "input": [], "window": 0,
                   "phone": [], "vision": []} for h in range(24)}
    for ts, p in payloads(acts):
        buckets[_hour_of(ts)]["activity"].append(p)
    for ts, p in payloads(cams):
        buckets[_hour_of(ts)]["camera"].append(p)
    for ts, p in payloads(ins):
        buckets[_hour_of(ts)]["input"].append(p)
    for ts, p in payloads(phones):
        buckets[_hour_of(ts)]["phone"].append(ts)
    for r in wins:
        buckets[_hour_of(r["ts"])]["window"] += 1
    for ts, p in payloads(shots):
        cat = label_by_sha.get(p.get("sha256"))
        if cat:
            buckets[_hour_of(ts)]["vision"].append(cat)

    episodes = store.episodes(day, store_behavior_version(store))
    focus_by_hour: dict[int, list[int]] = {}
    for ep in episodes:
        focus_by_hour.setdefault(_hour_of(ep["started_at"]), []).append(int(ep["focus"]))

    rows = []
    for h in range(24):
        b = buckets[h]
        cov = 0
        presence = facing = None
        if b["activity"]:
            presence = round(sum(1 for p in b["activity"] if p.get("active")) / len(b["activity"]), 3)
            cov |= 1 << 0
        if b["camera"]:
            facing = round(sum(1 for p in b["camera"] if p.get("state") == "facing") / len(b["camera"]), 3)
            cov |= 1 << 1
        if b["input"]:
            cov |= 1 << 2
        if b["window"]:
            cov |= 1 << 3
        if b["phone"]:
            cov |= 1 << 4
        if b["vision"]:
            cov |= 1 << 5

        rhythm = [p for p in b["input"]                      # 只算「真有打字」的窗口，空窗不进均值
                  if "iki_median_ms" in p and int(p.get("keys", 0) or 0) > 0]
        keys = sum(int(p.get("keys", 0)) for p in b["input"]) if b["input"] else None
        clicks = sum(int(p.get("clicks", 0)) for p in b["input"]) if b["input"] else None
        dist = sum(int(p.get("dist_px", 0)) for p in b["input"]) if b["input"] else None
        rows.append({
            "day": day, "hour": h,
            "presence_ratio": presence,
            "facing_ratio": facing,
            "switch_count": b["window"] if b["window"] else 0,
            "keys": keys, "clicks": clicks, "dist_px": dist,
            "straightness": _weighted_mean(b["input"], "straightness", "path_px"),
            # 这五列只在「真有节奏字段的采样」上算；老事件没有这些键 ⇒ None，不填 0 冒充
            **({"iki_median_ms": int(_weighted_mean(rhythm, "iki_median_ms") or 0),
                "iki_cv_ms": _weighted_mean(rhythm, "iki_cv_ms"),
                "pauses": sum(int(p.get("pauses", 0)) for p in rhythm),
                "burst_max": max(int(p.get("burst_max", 0)) for p in rhythm),
                "bs_ratio": _weighted_mean(rhythm, "bs_ratio")}
               if rhythm else {"iki_median_ms": None, "iki_cv_ms": None, "pauses": None,
                               "burst_max": None, "bs_ratio": None}),
            "phone_day_min": phone_day_min,
            "phone_game_min": (int(round(game_by_hour[h])) if h in game_by_hour else None),
            "phone_day_top": phone_day_top,
            "phone_notif": sum(1 for _ in b["phone"]),
            "phone_notif_min": _phone_minutes(b["phone"]),
            "vision_cat": (Counter(b["vision"]).most_common(1)[0][0] if b["vision"] else None),
            "focus": (round(sum(focus_by_hour[h]) / len(focus_by_hour[h]))
                      if focus_by_hour.get(h) else None),
            "coverage": "".join("1" if cov >> i & 1 else "0" for i in range(len(COVERAGE_KEYS))),
            "feat_version": version,
        })
    return rows


def store_behavior_version(store) -> str:
    """episodes 的口径版本：从库里最近一条片段读，缺省 ep-1。"""
    row = store.conn.execute("SELECT def_version FROM episodes ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else "ep-1"


def table(rows: list[dict]) -> list[str]:
    """白话表格：每小时一行，缺数据打「-」。"""
    out = ["小时  在场   看屏   切换  按键   点击  光标   节奏(中位/CV)  停顿  连击  退格  手机  类别    专注"]
    for r in rows:
        has = any(r[k] is not None for k in ("presence_ratio", "facing_ratio", "keys",
                                              "phone_notif", "vision_cat", "focus"))
        if not has and not r["switch_count"]:
            continue
        def f(v, fmt="{:.2f}", dash="-"):
            return dash if v is None else fmt.format(v)
        out.append(f"{r['hour']:>3}  {f(r['presence_ratio']):>5} {f(r['facing_ratio']):>5} "
                   f"{r['switch_count']:>5} {f(r['keys'], '{}', '-')!s:>5} {f(r['clicks'], '{}', '-')!s:>5} "
                   f"{f(r['dist_px'], '{}', '-')!s:>5} {f(r['iki_median_ms'], '{}', '-')!s:>6}/{f(r['iki_cv_ms']):<4} "
                   f"{f(r['pauses'], '{}', '-')!s:>4} {f(r['burst_max'], '{}', '-')!s:>4} "
                   f"{f(r['bs_ratio']):>4} {r['phone_notif']:>4} {str(r['vision_cat'] or '-'):<7} "
                   f"{f(r['focus'], '{}', '-')!s:>4}")
    return out
