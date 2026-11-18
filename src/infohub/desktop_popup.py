"""桌面强弹窗（S13 的电脑腿）：zenity 模态对话框，可带选项、可直接打字回复。

与 `infohub.notify`（GNOME 系统通知）的区别：系统通知是弱提醒，会滑走、可能被勿扰吞掉；
这里是**必须点掉的模态对话框**，与手机上那张全屏卡对应。形状沿用本机既有的
wx-popup-server（zenity 560 宽，--warning / --entry / --forms）。

契约：INTERFACE.md §11.7c。
"""

from __future__ import annotations

import os
import shutil
import subprocess

DEFAULT_TIMEOUT = 300


def available() -> bool:
    return shutil.which("zenity") is not None


def _session_env() -> dict:
    env = dict(os.environ)
    # 从 cron/systemd 里调起来时没有会话变量，补齐到当前图形会话（本机 GNOME Wayland）
    env.setdefault("DISPLAY", ":2")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _labels(options) -> list[str]:
    out = []
    for o in options or []:
        if isinstance(o, dict):
            lab = str(o.get("label") or o.get("key") or "").strip()
        else:
            lab = str(o).strip()
        if lab:
            out.append(lab)
    return out


def show(title: str, body: str, options=None, timeout: int = DEFAULT_TIMEOUT,
         ask_reply: bool = True) -> dict:
    """弹一条桌面强弹窗。返回 {"shown", "reply", "option", "canceled"}。

    三种形态：有选项 ⇒ --forms（下拉选项 + 回复输入）；ask_reply ⇒ --entry；否则 --warning。
    """
    z = shutil.which("zenity")
    if not z:
        raise RuntimeError("zenity 不在 PATH（桌面强弹窗需要 zenity）")
    text = body or ""
    labels = _labels(options)
    if labels:
        text = (text + "\n\n（选一项，或在下框打字回复；回车提交）").strip()
        cmd = [z, "--forms", "--title", title, "--text", text, "--width", "560",
               "--add-combo", "选项", "--combo-values", "|".join(labels),
               "--add-entry", "回复", "--separator", "|", "--modal",
               "--timeout", str(int(timeout))]
    elif ask_reply:
        text = (text + "\n\n（可直接在这里打字回复；回车提交，留空=只确认）").strip()
        cmd = [z, "--entry", "--title", title, "--text", text, "--width", "560",
               "--modal", "--timeout", str(int(timeout))]
    else:
        cmd = [z, "--warning", "--title", title, "--text", text, "--width", "560",
               "--no-markup", "--modal", "--timeout", str(int(timeout))]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=int(timeout) + 10, env=_session_env())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"zenity 超时未关（{timeout}s）") from exc
    if p.returncode not in (0, 1, 5):  # 0=确认/提交 1=取消 5=超时自动关
        detail = (p.stderr or "").strip().splitlines()
        raise RuntimeError(f"zenity rc={p.returncode}: {detail[-1][:200] if detail else ''}")
    raw = (p.stdout or "").strip()
    reply, option = raw or None, None
    if labels and raw:
        parts = [x.strip() for x in raw.split("|")]
        option = parts[0] or None
        typed = parts[1] if len(parts) > 1 else ""
        reply = typed or option
    return {"shown": True, "reply": reply, "option": option,
            "canceled": p.returncode == 1}
