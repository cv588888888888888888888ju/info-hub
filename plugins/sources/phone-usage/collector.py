"""M17 手机屏幕使用时间：读 `dumpsys usagestats` 的日桶（契约见 INTERFACE.md §4.23）。

为什么是这条路：手机自己就在算「每个 app 今天被看见多久」，我们不用猜前台、不用无障碍、
也不用 Shizuku（实测那层执行不了）。代价是必须插线 —— 所以 enabled 默认 false。
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timedelta

from infohub.events import iso, make_event
from infohub.sources import CollectorError

NODE = "phone-usage"
KIND = "phone.usage"
HOURS_KIND = "phone.usage.hours"

RE_RANGE = re.compile(r'^\s+timeRange="([^"]+)"\s*$')
RE_EVT = re.compile(r'time="([^"]+)" type=(ACTIVITY_RESUMED|ACTIVITY_PAUSED|ACTIVITY_STOPPED) package=(\S+)')
RE_PKG = re.compile(r'package=(\S+)\s+totalTimeUsed="([^"]*)"')
RE_VISIBLE = re.compile(r'totalTimeVisible="([^"]*)"')


def to_min(text: str) -> float | None:
    """`MM:SS` / `H:MM:SS` → 分钟；看不懂返回 None（不猜）。

    格式与 `android.text.format.DateUtils.formatElapsedTime` 一致：不足 1 小时只有两段（分:秒），
    超过 1 小时才是三段。实测 `totalTimeVisible="53:31"` ＝ 53 分 31 秒，`"2:21:39"` ＝ 2 小时 21 分 39 秒。
    """
    parts = str(text or "").strip().split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:                       # `MM:SS`（DateUtils.formatElapsedTime：不足 1 小时不带时位）
        h, m, s = 0, nums[0], nums[1]
    else:                                    # `H:MM:SS`
        h, m, s = nums
    return round(h * 60 + m + s / 60.0, 2)


def day_of(label: str) -> str | None:
    """桶标签里最后一个日期 = 桶结束日（`2026/10/27 17:04 – 2026/10/28 10:41` ⇒ 2026-10-28）。"""
    found = re.findall(r"(\d{4})/(\d{1,2})/(\d{1,2})", label or "")
    if not found:
        return None
    y, m, d = found[-1]
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def parse(text: str, top_n: int = 15) -> dict | None:
    """从 dumpsys usagestats 全文里取**第一个聚合日桶**，返回 payload。"""
    lines = (text or "").splitlines()
    label = None
    start = None
    for i, line in enumerate(lines):
        if RE_RANGE.match(line):
            label, start = RE_RANGE.match(line).group(1), i + 1
            break
    if start is None:
        return None

    packages: list[dict] = []
    total = 0.0
    for line in lines[start:]:
        if RE_RANGE.match(line) or line.startswith("user=") or "Database Summary" in line:
            break                                    # 下一个桶 / 下一个用户 / 库汇总，日桶结束
        m = RE_PKG.search(line)
        if not m:
            continue
        mv = RE_VISIBLE.search(line)
        if not mv:                                   # 没有「可见时长」就不算，不拿 used 冒充
            continue
        minutes = to_min(mv.group(1))
        if minutes is None:
            continue
        total += minutes
        packages.append({"app": m.group(1), "min": minutes})

    if not packages:
        return None
    packages.sort(key=lambda p: p["min"], reverse=True)
    return {
        "day": day_of(label),
        "bucket": label,
        "total_min": round(total, 1),
        "count": len(packages),
        "packages": packages[: max(1, int(top_n))],
    }


def _adb(config, *args: str) -> str:
    prog = config.collector_get(NODE, "adb", "adb")
    serial = str(config.collector_get(NODE, "serial", "") or "").strip()
    cmd = [prog] + (["-s", serial] if serial else []) + list(args)
    timeout = float(config.collector_get(NODE, "timeout_s", 60) or 60)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise CollectorError(f"找不到 adb（{prog}）") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(f"adb 超时 {timeout:.0f}s") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise CollectorError(f"adb 失败（rc={proc.returncode}）：{err[-1] if err else '无输出'}")
    return proc.stdout


def collect(config, store) -> list[dict]:
    if not config.collector_get(NODE, "enabled", False):
        return []

    state = store.state_get(NODE) or {}
    cursor = state.get("cursor") or {}       # state_get 返回整行，游标在 cursor 键里
    interval = float(config.collector_get(NODE, "interval_s", 1800))
    interval = 1800.0 if interval is None else interval   # 0 是合法值（测试用），别被 or 吃掉
    now = time.time()
    if cursor.get("epoch") and now - float(cursor["epoch"]) < interval:
        return []

    text = _adb(config, "shell", "dumpsys", "usagestats")
    payload = parse(text, int(config.collector_get(NODE, "top_n", 15) or 15))  # top_n=0 无意义，保留默认
    if payload is None:
        raise CollectorError("usagestats 里没有可用的日桶（解析失败或屏用过统计被清空）")

    stamp = iso()
    events = [make_event(NODE, KIND, payload, ts=stamp)]
    games = config.collector_get(NODE, "games", []) or []
    hours = parse_hours(text, list(games))
    if hours["hours"]:
        events.append(make_event(NODE, HOURS_KIND, {"day": payload["day"], **hours}, ts=stamp))
    store.state_put(NODE, cursor={"epoch": now, "day": payload["day"]}, ok=True)
    return events


# ---- M18：逐小时「游戏在前台」的时段 -----------------------------------------


def parse_hours(text: str, games: list[str], now=None) -> dict:
    """把 24 小时事件段里「游戏在前台」的时段摊到小时桶，返回 {小时键: {包名: 分钟}}。

    只统计 `games` 里列出的包（前缀匹配）—— 不猜哪个是游戏，配置说了算，说错了改配置。
    事件段只有最近 24 小时，所以这里只回答「昨天现在到现在的游戏时间」。
    """
    _dt = datetime

    lines = (text or "").splitlines()
    open_at: dict[str, "_dt"] = {}
    segs: list[tuple[str, "_dt", "_dt"]] = []
    for line in lines:
        m = RE_EVT.search(line)
        if not m:
            continue
        stamp, typ, pkg = m.groups()
        try:
            ts = _dt.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if typ == "ACTIVITY_RESUMED":
            open_at[pkg] = ts
        elif pkg in open_at:
            start = open_at.pop(pkg)
            if ts > start:
                segs.append((pkg, start, ts))
    tail = now or (_dt.now().replace(microsecond=0))
    for pkg, start in open_at.items():          # 还没退出的：算到当前时刻
        if tail > start:
            segs.append((pkg, start, tail))

    prefixes = [g for g in (games or []) if g]
    if not prefixes:
        return {"hours": {}, "games": []}

    buckets: dict[str, dict[str, float]] = {}
    for pkg, start, end in segs:
        if not any(pkg == p or pkg.startswith(p) for p in prefixes):
            continue
        cur = start
        while cur < end:
            nxt = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            piece_end = min(nxt, end)
            key = cur.strftime("%Y-%m-%dT%H")
            buckets.setdefault(key, {})
            buckets[key][pkg] = round(buckets[key].get(pkg, 0.0) + (piece_end - cur).total_seconds() / 60.0, 1)
            cur = piece_end
    return {"hours": buckets, "games": prefixes}
