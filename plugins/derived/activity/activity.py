"""活跃采样采集器（M10）：键鼠在不在 + 窗口集合变没变。语义见 INTERFACE.md §4、§4.15。

只回答两件事，别的都不猜：
①「现在人在不在」—— mutter 空闲时长（复用 notify 的探针）；
②「窗口集合变没变」—— 应用名集合的哈希，用来数切换次数。

拿不到「哪个窗口在前台」（GNOME 50 Wayland 下 AT-SPI 没有焦点信息，实测各窗口 STATE_ACTIVE 全 0），
所以「切换」只能按集合变化计数，分不清「切到同一应用的另一个窗口」。见 INTERFACE.md §4.15。

标题**不进哈希**：浏览器/编辑器标题常带计时器（视频进度、字数、秒数），
把它算进去会把「根本没换窗口」记成一次次切换，把专注度算成 0。
"""

from __future__ import annotations

import hashlib
import time

from infohub import events as E
from infohub.notify import Env, ProbeError
from infohub.sources import CollectorError
from infohub import atspi as W

NODE = "activity"
KIND = "activity.sample"


def app_hash(frames: list[dict]) -> str:
    """应用名集合的哈希（排序后，不含标题）。同 §5.2 的 frame_hash。"""
    apps = sorted({str(f.get("app") or "?") for f in frames})
    return hashlib.sha256("\x1f".join(apps).encode("utf-8")).hexdigest()[:12]


def collect(config, store) -> list[dict]:
    interval = float(config.collector_get(NODE, "interval_s", 60) or 0)
    idle_active_s = float(config.get("behavior.idle_active_s", 60) or 60)
    heartbeat_s = float(config.get("behavior.heartbeat_s", 900) or 900)

    state = store.state_get(NODE) or {}
    cursor = dict(state.get("cursor") or {})
    now = time.time()
    last_sample = cursor.get("sample_epoch")
    if interval > 0 and isinstance(last_sample, (int, float)):
        if now - float(last_sample) < interval:
            return []

    try:
        idle_s = float(Env().idle_s())
    except ProbeError as exc:                      # 探不到 ≠ 不在场：按失败处理，不落半条
        raise CollectorError(f"活跃度探针失败（不落采样）: {exc}") from exc

    active = idle_s < idle_active_s
    frames = W.frames(config) if active else []
    digest = app_hash(frames)
    apps = sorted({str(f.get("app") or "?") for f in frames})

    prev_active = cursor.get("active")
    last_emit = cursor.get("emit_epoch")
    must_emit = (
        active                                            # 在场必落
        or bool(prev_active)                              # 在场→离开的边界（否则算不出结束时刻）
        or not isinstance(last_emit, (int, float))        # 第一条
        or now - float(last_emit) >= heartbeat_s          # 离开期心跳
    )

    store.state_put(NODE, cursor={"sample_epoch": now, "at": E.iso(),
                                  "emit_epoch": now if must_emit else last_emit,
                                  "active": active, "frame_hash": digest}, ok=True)
    if not must_emit:
        return []

    payload = {"idle_s": round(idle_s, 3), "active": active, "frame_hash": digest,
               "frame_count": len(frames), "apps": apps[:10]}
    return [E.make_event(NODE, KIND, payload, ts=E.iso(), privacy="normal")]
