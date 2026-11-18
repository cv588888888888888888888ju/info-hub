"""静默截屏的取帧后端（M7）：ScreenCast portal → PipeWire → 单帧 JPEG。

为什么不走「截图」：`org.gnome.Shell.Screenshot` 对非授权调用者 `Access denied`；
`org.freedesktop.portal.Screenshot` 在无 app-id 的调用者下**不产出 PNG**，且那条路正是
"闪光动画 + 快门声"（用户 2026-10-16 明确否掉）。ScreenCast 是**视频流**，取帧天然无声无动画；
代价是需要用户**一次性**点「共享屏幕」同意（restore_token 存 `<state_dir>/screencast.json`，0600）。

语义见 INTERFACE.md §4.13；这里的函数只负责"取一帧并存成文件"，**不判断该不该取**
（活跃度/间隔在 `collectors/screenshot.py` 里判）。
"""

from __future__ import annotations

import json
import os
import secrets
import struct
import subprocess

from datetime import datetime, timedelta
from pathlib import Path

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"
SOURCE_MONITOR = 1          # 1=MONITOR 2=WINDOW 4=VIRTUAL（真机 AvailableSourceTypes=7）
CURSOR_HIDDEN = 1
PERSIST_MODE = 2  # 0=不持久 1=本次进程内 2=持久到显式撤销（要 restore_token 必须 ≥1）
BACKENDS = ("screencast", "command")
JPEG_SOF = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)


class ScreencastError(Exception):
    """取帧失败（portal 拒答 / 用户没同意 / 管道超时 / 命令非 0）。"""


# --------------------------------------------------------------------------- 配置取值
def _cfg(config, key, default=None):
    return config.collector_get("screenshot", key, default)


def shots_dir(config) -> Path:
    """缩放图落盘根（`screenshot.dir` 为空 ⇒ `<root>/screens`）。"""
    raw = str(_cfg(config, "dir", "") or "").strip()
    return Path(raw).expanduser() if raw else config.root / "screens"


def token_path(config) -> Path:
    raw = str(_cfg(config, "token_file", "") or "").strip()
    return Path(raw).expanduser() if raw else config.state_dir / "screencast.json"


def load_token(config) -> dict:
    try:
        data = json.loads(token_path(config).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_token(config, data: dict) -> Path:
    path = token_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- portal 管道
def _gi():
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception as exc:  # pragma: no cover - 本机已实测可用
        raise ScreencastError(f"缺少 python3-gobject/Gio: {type(exc).__name__}: {exc}") from exc
    return Gio, GLib


def _request_path(Gio, conn, handle_token: str) -> str:
    sender = conn.get_unique_name().lstrip(":").replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{handle_token}"


def _call_request(Gio, GLib, conn, method: str, signature: str, params: tuple,
                  timeout_s: float, handle_token: str) -> dict:
    """发一个"返回 request handle"的 portal 调用，等它的 `Response` 信号。

    必须先订阅再发（Response 可能比我们回到主循环还快）；`response != 0` = 用户拒绝/取消。
    """
    path = _request_path(Gio, conn, handle_token)
    box: dict = {}
    loop = GLib.MainLoop()

    def on_signal(_c, _s, _p, _i, _sig, parameters):
        code, results = parameters.unpack()
        box["code"] = code
        box["results"] = results or {}
        loop.quit()

    sub = conn.signal_subscribe(PORTAL_BUS, REQUEST_IFACE, "Response", path, None,
                                Gio.DBusSignalFlags.NONE, on_signal)

    def on_timeout():
        box["timeout"] = True
        loop.quit()
        return False

    timer = GLib.timeout_add(int(max(timeout_s, 1) * 1000), on_timeout)
    try:
        conn.call_sync(PORTAL_BUS, PORTAL_PATH, SCREENCAST_IFACE, method,
                       GLib.Variant(signature, params), None, Gio.DBusCallFlags.NONE, 30000, None)
        loop.run()
    finally:
        # on_timeout 自己 `return False` 已经把源摘了，这里再摘会告警；先看它还在不在。
        if GLib.MainContext.default().find_source_by_id(timer) is not None:
            GLib.source_remove(timer)
        conn.signal_unsubscribe(sub)

    if box.get("timeout"):
        raise ScreencastError(f"{method}: 等用户同意超时（{timeout_s:.0f}s）")
    code = box.get("code")
    if code is None:
        raise ScreencastError(f"{method}: 没收到 Response 信号")
    if code != 0:
        raise ScreencastError(f"{method}: 用户拒绝或取消（response={code}）")
    return box.get("results") or {}


def _open_session(Gio, GLib, conn, timeout_s: float, restore_token: str = "") -> dict:
    """CreateSession → SelectSources（带 restore_token）→ Start；返回本次流的 node/size/token。"""
    session_token = "infohub" + secrets.token_hex(4)
    tok = "c" + secrets.token_hex(4)
    # 本机 portal v5 的签名是 CreateSession(a{sv})（**没有** parent_window；见 gdbus introspect 实测）
    res = _call_request(Gio, GLib, conn, "CreateSession", "(a{sv})",
                        ({"handle_token": GLib.Variant("s", tok),
                          "session_handle_token": GLib.Variant("s", session_token)},),
                        timeout_s, tok)
    session = res.get("session_handle")
    if not session:
        raise ScreencastError("CreateSession 没给 session_handle")

    tok2 = "s" + secrets.token_hex(4)
    options = {"handle_token": GLib.Variant("s", tok2),
               "types": GLib.Variant("u", SOURCE_MONITOR),
               "multiple": GLib.Variant("b", False),
               "cursor_mode": GLib.Variant("u", CURSOR_HIDDEN),
               # 2 = 权限持久到显式撤销；**只有设了它，portal 才会在 Start 的结果里给 restore_token**
               "persist_mode": GLib.Variant("u", PERSIST_MODE)}
    if restore_token:
        options["restore_token"] = GLib.Variant("s", restore_token)
    selected = _call_request(Gio, GLib, conn, "SelectSources", "(oa{sv})",
                             (session, options), timeout_s, tok2)

    tok3 = "t" + secrets.token_hex(4)
    started = _call_request(Gio, GLib, conn, "Start", "(osa{sv})",
                            (session, "", {"handle_token": GLib.Variant("s", tok3)}),
                            timeout_s, tok3)
    streams = list(started.get("streams") or [])
    if not streams:
        raise ScreencastError("Start 没给 streams（没有可共享的屏幕）")
    node_id, props = streams[0][0], (streams[0][1] or {})
    size = props.get("size")
    # restore_token：按规范由 **Start** 的结果返回（不是 SelectSources），且应"单次使用"。
    # 实测本机 GNOME portal 复用时**不再下发新令牌**（`token_from_portal=False`）而旧令牌继续有效，
    # 所以这里既记录"portal 到底有没有给新令牌"，也保留旧令牌兜底 —— 见 INTERFACE §4.13。
    new_token = str(started.get("restore_token") or "")
    return {"session": session, "node_id": int(node_id),
            "size": [int(size[0]), int(size[1])] if size else None,
            "token_from_portal": bool(new_token),
            "restore_token": new_token or restore_token or ""}


def _close_session(Gio, GLib, conn, session: str) -> None:
    try:
        conn.call_sync(PORTAL_BUS, session, SESSION_IFACE, "Close",
                       GLib.Variant("(a{sv})", ({},)), None, Gio.DBusCallFlags.NONE, 5000, None)
    except Exception:
        pass


def consent(config, timeout_s: float | None = None) -> dict:
    """一次性同意：弹「共享屏幕」对话框，成功后把 restore_token 落到 0600 的文件里。"""
    total = float(timeout_s or _cfg(config, "consent_timeout_s", 120) or 120)
    Gio, GLib = _gi()
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    info = _open_session(Gio, GLib, conn, total, load_token(config).get("restore_token") or "")
    _close_session(Gio, GLib, conn, info["session"])
    data = {"restore_token": info["restore_token"], "node_id": info["node_id"],
            "size": info["size"], "at": _now_iso()}
    path = save_token(config, data)
    return dict(data, token_file=str(path), ok=bool(info["restore_token"]))


# --------------------------------------------------------------------------- 取帧
def jpeg_size(path) -> tuple[int, int] | None:
    """从 JPEG 的 SOF 段读像素尺寸（不依赖 Pillow）。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in JPEG_SOF:
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return int(width), int(height)
        i += 2 + length
    return None


def _file_info(path, backend: str) -> dict:
    p = Path(path)
    blob = p.read_bytes()
    import hashlib
    size = jpeg_size(p)
    return {"path": str(p), "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()[:16],
            "width": size[0] if size else None, "height": size[1] if size else None,
            "backend": backend}


def target_size(max_width: int, size) -> tuple[int, int] | None:
    """按 portal 报的流尺寸算等比目标尺寸；算不出回 None。

    只写 `width=[1,N]` 时 videoscale 会保留源高度（实测 2560x1600 缩到 1280 宽变成 1280x1600 变形），
    所以两个维度都要给：`min(max_width, 源宽)` + 按源宽高比算高。见 INTERFACE §4.13。
    """
    try:
        mw = int(max_width)
    except (TypeError, ValueError):
        return None
    if mw <= 0 or not size or len(size) != 2:
        return None
    try:
        sw, sh = int(size[0]), int(size[1])
    except (TypeError, ValueError):
        return None
    if sw <= 0 or sh <= 0:
        return None
    tw = min(mw, sw)
    return tw, max(1, int(round(sh * tw / sw)))


def _gst_desc(fd: int, node_id: int, max_width: int, quality: int, size=None) -> str:
    """取帧管道的描述串（单独成函数：靶心可单测，所以验收能机械判"宽高都钉了"）。"""
    tgt = target_size(max_width, size)
    caps = (f"video/x-raw,width={tgt[0]},height={tgt[1]}" if tgt
            else (f"video/x-raw,width=[1,{int(max_width)}]" if int(max_width) > 0 else "video/x-raw"))
    return (f"pipewiresrc fd={int(fd)} path={int(node_id)} num-buffers=1 ! "
            f"videoconvert ! videoscale add-borders=true ! {caps} ! "
            f"jpegenc quality={int(quality)} ! filesink name=sink")


def _gst_grab(fd: int, node_id: int, out_path: Path, max_width: int, quality: int,
              timeout_s: float, size: list | None = None) -> None:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
    except Exception as exc:  # pragma: no cover
        raise ScreencastError(f"缺少 GStreamer/gi: {type(exc).__name__}: {exc}") from exc

    Gst.init(None)
    desc = _gst_desc(fd, node_id, max_width, quality, size)
    try:
        pipeline = Gst.parse_launch(desc)
    except GLib.Error as exc:
        raise ScreencastError(f"管道建不起来: {exc.message}") from exc
    pipeline.get_by_name("sink").set_property("location", str(out_path))

    box: dict = {}
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, message):
        if message.type == Gst.MessageType.EOS:
            box["ok"] = True
            loop.quit()
        elif message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            box["error"] = err.message
            loop.quit()
        return True

    busy = bus.connect("message", on_message)

    def on_timeout():
        box["error"] = f"取帧超时（{timeout_s:.0f}s）"
        loop.quit()
        return False

    timer = GLib.timeout_add(int(max(timeout_s, 1) * 1000), on_timeout)
    pipeline.set_state(Gst.State.PLAYING)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    if GLib.MainContext.default().find_source_by_id(timer) is not None:
        GLib.source_remove(timer)
    bus.disconnect(busy)
    if not box.get("ok"):
        raise ScreencastError(f"取帧失败: {box.get('error') or '未知'}")


def grab_screencast(config, out_path, timeout_s: float | None = None,
                    allow_consent: bool = True) -> dict:
    """走 portal 取一帧。

    `allow_consent=False`（采集器用）时**绝不弹同意框** —— 没有令牌就直接失败：
    无人值守的一轮里弹一个等 300 秒的对话框，比不取帧坏得多。
    """
    out = Path(out_path)
    total = float(timeout_s or _cfg(config, "consent_timeout_s", 120) or 120)
    capture_t = float(_cfg(config, "capture_timeout_s", 20) or 20)
    max_width = int(_cfg(config, "max_width", 1280) or 0)
    quality = int(_cfg(config, "jpeg_quality", 80) or 80)

    Gio, GLib = _gi()
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    token = load_token(config).get("restore_token") or ""
    if not token and not allow_consent:
        raise ScreencastError("没有共享令牌（先跑一次 bin/hub-screencast consent）；"
                              "无人值守的取帧不会弹同意框")
    info = _open_session(Gio, GLib, conn, total, token)
    fd = None
    try:
        variant, fd_list = conn.call_with_unix_fd_list_sync(
            PORTAL_BUS, PORTAL_PATH, SCREENCAST_IFACE, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (info["session"], {})), None, Gio.DBusCallFlags.NONE,
            30000, None, None)
        index = variant.unpack()[0]
        fd = fd_list.get(index)
        out.parent.mkdir(parents=True, exist_ok=True)
        _gst_grab(fd, info["node_id"], out, max_width, quality, capture_t, size=info.get("size"))
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _close_session(Gio, GLib, conn, info["session"])

    if info["restore_token"] and info["restore_token"] != token:
        save_token(config, {"restore_token": info["restore_token"], "node_id": info["node_id"],
                            "size": info["size"], "at": _now_iso()})
    result = _file_info(out, "screencast")
    result.update({"node_id": info["node_id"], "size": info["size"],
                   "token_from_portal": bool(info.get("token_from_portal")),
                   "token": info["restore_token"]})
    return result


def grab_command(config, out_path) -> dict:
    """验收/替代后端：跑一条命令，由它把图写到 `{out}`（或 $HUB_SHOT_OUT）。"""
    argv = list(_cfg(config, "command", []) or [])
    if not argv:
        raise ScreencastError("backend=command 但 `screenshot.command` 是空的")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [str(a).replace("{out}", str(out)) for a in argv]
    env = dict(os.environ, HUB_SHOT_OUT=str(out),
               HUB_SHOT_MAX_WIDTH=str(int(_cfg(config, "max_width", 1280) or 0)),
               HUB_SHOT_QUALITY=str(int(_cfg(config, "jpeg_quality", 80) or 80)))
    timeout = float(_cfg(config, "capture_timeout_s", 20) or 20)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScreencastError(f"{argv[0]}: {type(exc).__name__}: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()
        raise ScreencastError(f"{argv[0]} exit={done.returncode}: {tail[-1][:120] if tail else ''}")
    if not out.is_file():
        raise ScreencastError(f"{argv[0]} 没写出 {out}")
    return _file_info(out, "command")


def grab(config, out_path, timeout_s: float | None = None, allow_consent: bool = True) -> dict:
    backend = str(_cfg(config, "backend", "screencast") or "screencast")
    if backend not in BACKENDS:
        raise ScreencastError(f"未知 backend: {backend}")
    if backend == "command":
        return grab_command(config, out_path)
    return grab_screencast(config, out_path, timeout_s, allow_consent=allow_consent)


# --------------------------------------------------------------------------- 留存
def prune(config, now: datetime | None = None) -> dict:
    """删掉超过 `keep_days` 的图（留元数据）；空目录一并清掉。"""
    keep_days = int(_cfg(config, "keep_days", 7) or 7)
    root = shots_dir(config)
    cutoff = (now or datetime.now().astimezone()) - timedelta(days=keep_days)
    removed: list[str] = []
    freed = 0
    kept = 0
    if root.is_dir():
        for path in sorted(root.rglob("*.jpg")):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                continue
            if mtime >= cutoff:
                kept += 1
                continue
            try:
                freed += path.stat().st_size
                path.unlink()
                removed.append(str(path.relative_to(root)))
            except OSError:
                pass
        for child in sorted(root.rglob("*"), reverse=True):
            if child.is_dir() and not any(child.iterdir()):
                try:
                    child.rmdir()
                except OSError:
                    pass
    return {"removed": removed, "kept": kept, "freed_bytes": freed, "keep_days": keep_days}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
