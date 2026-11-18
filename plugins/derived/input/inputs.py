"""M13 输入节奏：从 /dev/input 读键盘与鼠标的**计数**（不读内容）。

契约见 INTERFACE.md §4.18 / §5.12。

为什么读原始设备：Wayland 下拿不到「窗口焦点」（M10 已封闭这条路），也拿不到
「这一分钟有没有在动键鼠」。mutter 的 GetIdletime 只能区分「动/不动」，
读 evdev 能补上「动得多不多、点了几下、光标跑了多远」—— 这是专注度里
「手上在动」这一维，也是文献里唯一有硬指标的信号。

隐私边界（硬约束）：
- **只读事件计数**，按键只数「按下」次数，不记 code、不记内容、不记时间戳序列；
- payload 里永远不出现 keycode，验收脚本会断言这一点；
- 不落原文、不落屏幕、不出本机。
"""

from __future__ import annotations

import fcntl
import glob
import os
import select
import struct
import time
from pathlib import Path

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
EVENT_SIZE = 24                      # struct input_event（64 位：timeval 16 + 2 + 2 + 4）
BUTTONS = {0x110: "left", 0x111: "right", 0x112: "middle"}   # BTN_LEFT/RIGHT/MIDDLE
REL_X, REL_Y = 0, 1


def _ioc(ev: int, length: int = 8) -> int:
    """_IOC(_IOC_READ, 'E', 0x20 + ev, length) —— EVIOCGBIT(ev, length) 的请求码。"""
    return 0x80000000 | (length << 16) | (ord("E") << 8) | (0x20 + ev)


def capabilities(path: str) -> set[int]:
    """设备支持的事件类型（EV_KEY/EV_REL/...）；读不到返回空集。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return set()
    try:
        buf = bytearray(8)
        fcntl.ioctl(fd, _ioc(0, 8), buf)                            # EVIOCGBIT(0)：支持哪些事件类型
        bits = int.from_bytes(buf, "little")
        return {ev for ev in range(32) if bits >> ev & 1}
    except OSError:
        return set()
    finally:
        os.close(fd)


def devices() -> list[dict]:
    """可读的事件设备 + 能干什么（键盘/鼠标）。"""
    out = []
    for path in sorted(glob.glob("/dev/input/event*")):
        caps = capabilities(path)
        if not caps:
            continue
        name_path = Path("/sys/class/input") / Path(path).name / "device" / "name"
        try:
            name = name_path.read_text(encoding="utf-8").strip()
        except OSError:
            name = "?"
        out.append({
            "path": path,
            "name": name,
            "has_key": EV_KEY in caps,
            "has_rel": EV_REL in caps,
            "has_abs": EV_ABS in caps,
        })
    return out


BACKSPACE = 14                       # KEY_BACKSPACE：只在内存里比较，绝不进 payload
IKI_MAX_MS = 3000.0                  # 超过它算停顿，不进节奏统计
BURST_GAP_MS = 500.0                 # 连击的相邻间隔上限
PAUSE_MS = 2000.0                    # 算「停顿」的阈值


def _rhythm(press_times: list[float]) -> dict:
    """键盘节奏五指标。纯函数；press_times 是单调时钟秒。"""
    if len(press_times) < 2:
        return {"iki_median_ms": 0, "iki_cv_ms": 0.0, "pauses": 0, "burst_max": 0}
    gaps = [(press_times[i + 1] - press_times[i]) * 1000.0 for i in range(len(press_times) - 1)]
    rhythm = [g for g in gaps if 0 < g <= IKI_MAX_MS]
    pauses = sum(1 for g in gaps if g > PAUSE_MS)
    # 连击：相邻间隔 <= BURST_GAP_MS 的最长连续段（段长 = 按键数）
    best = cur = 1
    for g in gaps:
        if 0 < g <= BURST_GAP_MS:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    if not rhythm:
        return {"iki_median_ms": 0, "iki_cv_ms": 0.0, "pauses": pauses, "burst_max": best}
    base = sorted(rhythm)
    mid = base[len(base) // 2] if len(base) % 2 else (base[len(base) // 2 - 1] + base[len(base) // 2]) / 2
    mean = sum(rhythm) / len(rhythm)
    var = sum((g - mean) ** 2 for g in rhythm) / len(rhythm)
    cv = (var ** 0.5) / mean if mean else 0.0
    return {"iki_median_ms": int(round(mid)), "iki_cv_ms": round(cv, 3),
            "pauses": pauses, "burst_max": best}


def aggregate(events, window_s: float = 60.0) -> dict:
    """把 (type, code, value[, t]) 序列压成计数 + 键盘节奏。纯函数，便于测试。"""
    presses: list[float] = []
    backs = 0
    keys = clicks = wheel = 0
    dist = 0.0                      # 净位移（起点到终点）
    path = 0.0                      # 路径长度（每一步的模长累加）—— 绕路时远大于 dist
    dx = dy = 0.0
    for ev in events:
        typ, code, val = ev[0], ev[1], ev[2]
        when = ev[3] if len(ev) > 3 else None
        if typ == EV_KEY:
            if val != 1:                      # 只数「按下」，忽略抬起与自动重复
                continue
            if code in BUTTONS:
                clicks += 1
            elif 0 <= code < 0x100:           # KEY_* 键盘码
                keys += 1
                if code == BACKSPACE:
                    backs += 1
                if when is not None:
                    presses.append(float(when))
        elif typ == EV_REL:
            if code == 0:                     # REL_X
                dx += val
                path += abs(val)
            elif code == 1:                   # REL_Y
                dy += val
                path += abs(val)
            elif code == 8:                   # REL_WHEEL
                wheel += abs(val)
    dist += (dx * dx + dy * dy) ** 0.5
    straightness = round(dist / path, 3) if path > 0 else 0.0
    out = {"secs": round(float(window_s), 1), "keys": keys, "clicks": clicks,
           "wheel": wheel, "dist_px": int(round(dist)), "path_px": int(round(path)),
           "straightness": straightness,
           "bs_ratio": round(backs / keys, 3) if keys else 0.0}
    out.update(_rhythm(presses))
    return out


class Sampler:
    """打开所有可读设备，按窗口读取并聚合。"""

    def __init__(self, paths: list[str] | None = None):
        self.paths = paths if paths is not None else [d["path"] for d in devices()
                                                      if d["has_key"] or d["has_rel"]]
        self.fds: list[int] = []
        for p in self.paths:
            try:
                self.fds.append(os.open(p, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass
        if not self.fds:
            raise PermissionError("没有可读的输入设备（需要 input 组；见 INTERFACE.md §4.18）")

    def read_window(self, seconds: float = 60.0) -> dict:
        events = []
        deadline = time.monotonic() + float(seconds)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select(self.fds, [], [], min(0.2, left))
            for fd in r:
                try:
                    data = os.read(fd, EVENT_SIZE * 128)
                except OSError:
                    continue
                now = time.monotonic()
                for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _s, _us, typ, code, val = struct.unpack_from("llHHi", data, i)
                    events.append((typ, code, val, now))
        return aggregate(events, window_s=seconds)

    def close(self) -> None:
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
