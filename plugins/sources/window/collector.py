"""窗口采集器：走无障碍总线（pyatspi）取应用名 + 窗口标题。语义见 INTERFACE.md §4、§5.2。

枚举本身在本体 `src/infohub/atspi.py`（跟 activity 共用，INTERFACE.md §4.15）；本文件只做落库条件。
边界（实测，见 PROJECT.md §4.1）：只能拿标题级信息，拿不到应用内文本；Chromium/Electron 默认不暴露。
"""

from __future__ import annotations

import hashlib

from infohub import atspi as W
from infohub import events as E

NODE = "window"


def frames(config) -> list[dict]:
    """公开入口（INTERFACE.md §4.15）：当前窗口集合，给 activity 采集器复用。

    只取集合，不落库、不看游标 —— 落库条件由调用方决定。
    """
    return W.frames(config)


def collect(config, store) -> list[dict]:
    frames_ = frames(config)
    digest = hashlib.sha256(E.canonical(frames_).encode("utf-8")).hexdigest()
    state = store.state_get(NODE) or {}
    if (state.get("cursor") or {}).get("hash") == digest:
        return []

    store.state_put(NODE, cursor={"hash": digest}, ok=True)
    payload = {"frames": frames_, "count": len(frames_)}
    return [E.make_event(NODE, "window.snapshot", payload, ts=E.iso(), privacy="sensitive")]
