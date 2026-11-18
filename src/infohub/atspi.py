"""AT-SPI 窗口枚举（无障碍总线）：给 window 来源插件与 activity 共用（INTERFACE.md §4.15）。

这是本体的**环境能力**，跟 notify.Env 的 mutter 空闲探针同类，不是采集器：
只枚举「应用名 + 窗口标题」，不落库、不看游标、不发事件 —— 落库条件由调用方决定。

边界（实测，见 PROJECT.md §4.1）：只能拿标题级信息，拿不到应用内文本；Chromium/Electron 默认不暴露。
"""

from __future__ import annotations

from .sources import CollectorError

NODE = "window"


def _frames(max_frames: int, head_chars: int) -> list[dict]:
    try:
        import pyatspi
    except ImportError as exc:
        raise CollectorError("pyatspi 不可用（无障碍栈缺失）") from exc
    try:
        desktop = pyatspi.Registry.getDesktop(0)
        apps = list(desktop)
    except Exception as exc:  # D-Bus 不通等
        raise CollectorError(f"无障碍总线不可用: {type(exc).__name__}: {exc}") from exc

    frames: list[dict] = []
    for app in apps:
        try:
            app_name = app.name
            children = list(app)
        except Exception:
            continue
        for frame in children:
            try:
                title = (frame.name or "").strip()
            except Exception:
                continue
            if title:
                frames.append({"app": app_name or "?", "title": title[:head_chars]})
            if len(frames) >= max_frames:
                return frames
    return frames


def frames(config) -> list[dict]:
    """公开入口（INTERFACE.md §4.15）：当前窗口集合，给 activity 采集器复用。

    只取集合，不落库、不看游标 —— 落库条件由调用方决定。
    """
    max_frames = int(config.collector_get(NODE, "max_frames", 40))
    head_chars = int(config.collector_get(NODE, "head_chars", 120))
    return _frames(max_frames, head_chars)
