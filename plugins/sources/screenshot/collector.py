"""静默截屏采集器（M7）：**键鼠活跃**时按间隔取一帧，存缩放图 + 元数据。语义见 INTERFACE.md §4.13。

四条边界都落在机制上（①–③ 用户 2026-10-16 定；④ 2026-11-13 定）：
①不活跃不反应 —— `require_active: true` 时先问 mutter 空闲时长，超过 `idle_max_s` 直接返回（不取帧、不发事件）；
②无闪光无快门声 —— 取帧走 ScreenCast 视频流（见 `infohub/screencast.py`），不走会闪的截图接口；
③探不到活跃也不取 —— 空闲探针失败按采集器失败处理（fail-soft：记 `run:screenshot`、不落任何图）；
④不在打字时取 —— 空闲 < `idle_min_s`（正在敲键 / 候选窗开着）时先**等一次停顿**（最多 `pause_wait_s` 秒）再取，
   等不到就这轮不取、`cursor.last_skip="typing"`（**不推进 `epoch`**）。用户 2026-11-13「屏幕监测会打断我的输入法」：
   活跃门 `idle < idle_max_s` 恰好把镜头对在「正在打字」上，所以另加这道门；语义见 INTERFACE.md §4.13。
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from pathlib import Path

from infohub import events as E
from infohub.sources import CollectorError

NODE = "screenshot"
SHOT_KIND = "shot.saved"
POLL_STEP_S = 0.5


def _active_frame(head_chars: int = 120) -> dict:
    """当前活动窗口的 {app, title}；拿不到就回空（元数据是附加信息，不是取帧前提）。"""
    try:
        import pyatspi
    except ImportError:
        return {}
    try:
        desktop = pyatspi.Registry.getDesktop(0)
        apps = list(desktop)
    except Exception:
        return {}
    for app in apps:
        try:
            frames = list(app)
        except Exception:
            continue
        for frame in frames:
            try:
                if not frame.getState().contains(pyatspi.STATE_ACTIVE):
                    continue
                title = (frame.name or "").strip()
                return {"app": app.name or "?", "title": title[:head_chars]}
            except Exception:
                continue
    return {}


def _wait_pause(env, idle_s: float, idle_min: float, wait_s: float) -> float | None:
    """④ 打字保护：等到一次自然停顿（空闲 ≥ `idle_min` 秒）。语义见 INTERFACE.md §4.13。

    返回停顿那一刻的空闲秒数；**探针中途失败 ⇒ None（放行）**：探不到空闲时长不等于「在打字」，
    不能因为探针坏了就停采（与活跃门的 fail-closed 相反，别合并）。轮询步长 ≤ `POLL_STEP_S`，总时长 ≤ `wait_s`。
    """
    from infohub.notify import ProbeError

    deadline = time.monotonic() + max(0.0, float(wait_s))
    while idle_s < idle_min:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(POLL_STEP_S, left))
        try:
            idle_s = env.idle_s()
        except ProbeError:
            return None
    return idle_s


def _target_path(config, now: datetime) -> Path:
    from infohub import derived

    day = derived.module(config, "screencast").shots_dir(config) / now.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    base = now.strftime("%H%M%S")
    path = day / f"{base}.jpg"
    seq = 1
    while path.exists():
        path = day / f"{base}-{seq}.jpg"
        seq += 1
    return path


def collect(config, store) -> list[dict]:
    from infohub import derived

    SC = derived.module(config, "screencast")   # S17：截屏实现在 plugins/derived/screencast/
    now_dt = datetime.now().astimezone()
    interval = float(config.collector_get(NODE, "interval_s", 300) or 0)
    state = store.state_get(NODE) or {}
    cursor = dict(state.get("cursor") or {})

    last = cursor.get("epoch")
    if interval > 0 and isinstance(last, (int, float)):
        if time.time() - float(last) < interval:
            return []

    idle_s = None
    if bool(config.collector_get(NODE, "require_active", True)):
        from infohub.notify import Env, ProbeError

        env = Env()
        try:
            idle_s = env.idle_s()
        except ProbeError as exc:
            raise CollectorError(f"活跃度探针失败（不取帧）: {exc}") from exc
        limit = float(config.collector_get(NODE, "idle_max_s", 60) or 60)
        if idle_s > limit:
            store.state_put(NODE, cursor=dict(cursor, last_skip="idle", idle_s=idle_s), ok=True)
            return []
        idle_min = float(config.collector_get(NODE, "idle_min_s", 0) or 0)
        if idle_min > 0 and idle_s < idle_min:
            wait_s = float(config.collector_get(NODE, "pause_wait_s", 0) or 0)
            idle_s = _wait_pause(env, idle_s, idle_min, wait_s)
            if idle_s is not None and idle_s < idle_min:
                store.state_put(NODE, cursor=dict(cursor, last_skip="typing", idle_s=idle_s), ok=True)
                return []

    target = _target_path(config, now_dt)
    try:
        # 无人值守：绝不弹同意框（没令牌就如实失败，错误进 run:screenshot）
        info = SC.grab(config, target, allow_consent=False)
    except SC.ScreencastError as exc:
        raise CollectorError(str(exc)) from exc

    try:
        rel = str(Path(info["path"]).relative_to(config.root))
    except ValueError:
        rel = str(info["path"])
    frame = _active_frame(int(config.collector_get(NODE, "head_chars", 120) or 120))
    payload = {"path": info["path"], "rel": rel, "bytes": info["bytes"],
               "sha256": info["sha256"], "width": info["width"], "height": info["height"],
               "idle_s": idle_s, "backend": info["backend"],
               "app": frame.get("app"), "title": frame.get("title")}

    store.state_put(NODE, cursor={"epoch": time.time(), "at": E.iso(), "path": rel,
                                  "sha256": info["sha256"], "last_skip": None}, ok=True)
    return [E.make_event(NODE, SHOT_KIND, payload, ts=E.iso(), privacy="sensitive")]


def digest_of(path) -> str:
    """文件内容哈希（供测试/对账；与事件里的 `sha256` 同口径）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
