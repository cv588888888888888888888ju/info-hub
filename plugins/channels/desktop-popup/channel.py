"""desktop-popup 通道插件（S7）：把 GNOME 系统通知的下发 / 按钮结算接进投递台账。

单函数入口 `send(request)`；request 形状、op 语义与失败语义见同目录 `README.md`。
gdbus 的调用形状与信号正则**复用** `infohub.notify` 的现行实现（`notify.send` / `notify.parse_dbus_id` /
`notify.MONITOR_ARGS` / `notify.SIGNAL_RE`），本文件不另写一套协议解析。
"""

from __future__ import annotations

import select
import subprocess
import time

from infohub import deliver as D
from infohub import events as E
from infohub import notify

#: 台账里的通道名（INTERFACE.md §11.1 的 `computer`）
CHANNEL = "computer"
OPS = ("dispatch", "act", "listen")
#: 允许就地回写的列；列语义见 INTERFACE.md §11.1
PATCHABLE = ("status", "remote_ref", "last_error", "attempts")


class ChannelError(Exception):
    """通道入参不合法（未知 op / 缺字段 / 想写不该写的列）。"""


def send(request: dict) -> dict:
    """通道入口（plugin.yaml 的 `entry`）。request 字段见 README.md §2。"""
    req = dict(request or {})
    op = str(req.get("op") or "dispatch")
    if op == "dispatch":
        return _dispatch(req)
    if op == "act":
        return _act(req)
    if op == "listen":
        return _listen(req)
    raise ChannelError(f"不认识的 op: {op}（只认 {'/'.join(OPS)}）")


# --------------------------------------------------------------------- 下发
def _options(config) -> list[dict]:
    """通知按钮 → 台账 options：`key` 一律复用 deliver.ACTIONS 的稳定键，`label` 只给界面显示。"""
    out: list[dict] = []
    for label in notify.action_labels(config):
        key = notify.CANON_ACTION.get(label, label)
        if key not in D.ACTIONS:      # 不新造键：映射不到稳定键的按钮不进台账
            continue
        out.append({"key": key, "label": label})
    return out


def _dispatch(req: dict) -> dict:
    """① 先写一条 pending 台账 → ② 让通知显示 → ③ 成功后补 remote_ref / status=delivered。"""
    config, store = req["config"], req["store"]
    summary = str(req.get("summary") or config.get("notify.summary") or "info-hub")
    body = str(req.get("body") or "")
    title = str(req.get("title") or summary)
    kind = req.get("kind")
    todo_id = req.get("todo_id")

    # 无待办不弹（用户 2026-11-14）：电脑弹窗只投真实待办；没有 todo_id、或待办不存在/已收尾 ⇒
    # 不建台账行、不发通知，留给处理端决定要不要建定时任务或即时消息（INTERFACE.md §11.6）。
    if not todo_id:
        return {"ok": True, "skipped": "no-todo", "delivery_id": None}
    row = store.conn.execute("SELECT status FROM todos WHERE id = ?", (str(todo_id),)).fetchone()
    if row is None or row["status"] not in ("open", "doing"):
        return {"ok": True, "skipped": "no-todo", "delivery_id": None}

    row = D.create(store, channel=CHANNEL, title=title, body=body, mode="single",
                   options=_options(config) or None, todo_id=todo_id, kind=kind)
    delivery_id = str(row["id"])
    try:
        backend_reply = notify.send(config, req.get("env") or notify.Env(),
                                    summary, body, kind or "")
    except notify.SendError as exc:
        _patch(store, delivery_id, status="failed", last_error=str(exc)[:300],
               attempts=int((store.delivery(delivery_id) or {}).get("attempts") or 0) + 1)
        return {"ok": False, "delivery_id": delivery_id, "error": str(exc)}

    backend = str(config.get("notify.backend") or "gdbus")
    dbus_id = notify.parse_dbus_id(backend_reply) if backend == "gdbus" else None
    remote_ref = str(dbus_id) if dbus_id is not None else None
    row = _patch(store, delivery_id, status="delivered", remote_ref=remote_ref)
    return {"ok": True, "delivery_id": delivery_id, "dbus_id": dbus_id,
            "remote_ref": remote_ref, "status": (row or {}).get("status"),
            "backend_reply": backend_reply}


def _patch(store, delivery_id: str, **fields) -> dict | None:
    """回写投递行的少数几列（逐列白名单见 `PATCHABLE`）。

    `store.py` 只有「建行 / ack / reply」三个写入口，没有通用更新方法；S7 的写作用域不含 `store.py`，
    所以「先建 pending 行、发送成功后补 remote_ref」这一步只能就地 UPDATE。语义见 INTERFACE.md §11.1。
    """
    sets: list[str] = []
    args: list = []
    for key, value in fields.items():
        if key not in PATCHABLE:
            raise ChannelError(f"不允许回写的列: {key}")
        sets.append(f"{key} = ?")
        args.append(value)
    sets.append("updated_at = ?")
    args.append(E.iso())
    args.append(str(delivery_id))
    store.conn.execute(f"UPDATE deliveries SET {', '.join(sets)} WHERE id = ?", tuple(args))
    store.conn.commit()
    return store.delivery(str(delivery_id))


# --------------------------------------------------------------------- 回执
def _find_delivery(store, dbus_id) -> dict | None:
    """按系统通知 id（= 台账 `remote_ref`）找回本通道的那一行；找不到返回 None（老数据）。"""
    want = str(dbus_id).strip()
    for row in store.deliveries(channel=CHANNEL, limit=0):
        if str(row.get("remote_ref") or "").strip() == want:
            return row
    return None


def _write_action(store, row: dict, key: str, text) -> dict:
    """把动作先写进台账：reply ⇒ `reply_delivery`，ignore-kind ⇒ 再置 `suppressed`，其余 ⇒ `ack_delivery`。"""
    delivery_id = str(row["id"])
    if key == "reply":
        if not (text and str(text).strip()):
            raise notify.ActionError("reply 动作缺少回复文本（空回复库不变）")
        return store.reply_delivery(delivery_id, str(text)) or {}
    updated = store.ack_delivery(delivery_id, action=key)
    if updated is None:
        raise notify.ActionError(f"投递不存在: {delivery_id}")
    if key == "ignore-kind":
        updated = _patch(store, delivery_id, status="suppressed") or updated
    return updated


def _brief(row: dict | None) -> dict | None:
    if not row:
        return None
    return {k: row.get(k) for k in ("id", "channel", "status", "action",
                                    "remote_ref", "todo_id")}


def _act(req: dict) -> dict:
    """① 按 remote_ref 找到台账行并先写回执 → ② 再按现行 notify 语义结算待办。"""
    config, store = req["config"], req["store"]
    dbus_id = req.get("dbus_id")
    action = str(req.get("action") or "").strip()
    key = notify.CANON_ACTION.get(action, action)
    reason = req.get("reason") or "notify-action"

    row = _find_delivery(store, dbus_id)
    ledger = _write_action(store, row, key, req.get("text")) if row is not None else None

    if key == "ack":                       # 纯确认：只写台账，不碰待办（INTERFACE.md §11.4）
        if ledger is None:
            raise notify.ActionError(f"没有 dbus_id={dbus_id} 对应的投递")
        return _plain(dbus_id, key, ledger, reason)
    if key == "reply":                     # 自由文本：台账已留档，待办状态不动
        if ledger is None:
            raise notify.ActionError(f"没有 dbus_id={dbus_id} 对应的投递")
        info = _plain(dbus_id, key, ledger, reason)
        info["reply"] = str(req.get("text"))
        return info

    info = notify.settle_action(config, store, dbus_id, action, reason=reason)
    info["delivery"] = _brief(ledger)
    return info


def _plain(dbus_id, key: str, ledger: dict, reason: str) -> dict:
    return {"at": E.iso(), "dbus_id": int(dbus_id), "action": key, "status": None,
            "todo_id": ledger.get("todo_id"), "notification_id": None,
            "delivery": _brief(ledger), "reason": reason}


# --------------------------------------------------------------------- 监听
def _iter_monitor(proc, timeout_s):
    """按行读子进程输出，`timeout_s` 到点就收工（monitor 是常驻的，不能干等）。"""
    deadline = time.monotonic() + float(timeout_s) if timeout_s else None
    while True:
        remain = None if deadline is None else deadline - time.monotonic()
        if remain is not None and remain <= 0:
            return
        ready, _, _ = select.select([proc.stdout], [], [], remain if remain is not None else 0.5)
        if not ready:
            if deadline is not None:
                return
            continue
        line = proc.stdout.readline()
        if not line:
            return
        yield line


def _listen(req: dict) -> dict:
    """`gdbus monitor` 回调解析：每个 ActionInvoked 走一次 `op=act`（先台账、后结算待办）。"""
    config, store = req["config"], req["store"]
    once = bool(req.get("once"))
    timeout_s = req.get("timeout_s")
    lines = req.get("lines")
    max_lines = int(req.get("max_lines") or 500)
    result: dict = {"at": E.iso(), "signals": 0, "acted": 0, "ignored": 0, "suppressed": 0,
                    "opened": 0, "snoozed": 0, "closed": 0, "handled": [], "errors": [],
                    "stopped": None}
    proc = None
    try:
        if lines is None:
            try:
                proc = subprocess.Popen(notify.MONITOR_ARGS, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
            except OSError as exc:
                raise notify.ActionError(f"起不了 gdbus monitor: {type(exc).__name__}: {exc}") from exc
            if proc.poll() not in (None, 0):
                raise notify.ActionError(f"gdbus monitor 立刻退出（exit={proc.returncode}）")
            lines = _iter_monitor(proc, timeout_s)

        seen = 0
        for line in lines:
            seen += 1
            if seen > max_lines:
                result["stopped"] = "max-lines"
                break
            m = notify.SIGNAL_RE.search(line or "")
            if not m:
                continue
            kind, dbus_id, key = m.group(1), int(m.group(2)), (m.group(3) or "").strip()
            result["signals"] += 1
            if kind == "NotificationClosed":
                result["closed"] += 1
                if once:
                    result["stopped"] = "once"
                    break
                continue
            try:
                info = _act({"config": config, "store": store, "dbus_id": dbus_id,
                             "action": key})
            except (notify.ActionError, ChannelError) as exc:
                result["errors"].append(str(exc))
                continue
            result["handled"].append(info)
            action_key = str(info.get("action") or "")
            if action_key in notify.IGNORE_KIND_ACTIONS:
                result["suppressed"] += 1
            elif action_key in notify.OPEN_ACTIONS:
                result["opened"] += 1
            elif action_key in notify.SNOOZE_ACTIONS:
                result["snoozed"] += 1
            else:
                result["acted" if info.get("status") == "done" else "ignored"] += 1
            if once:
                result["stopped"] = "once"
                break
        else:
            if result["stopped"] is None:
                ended = proc is not None and proc.poll() is not None
                result["stopped"] = "eof" if ended else "timeout"
        if proc is not None and proc.poll() not in (None, 0):
            raise notify.ActionError(f"gdbus monitor 退出码 {proc.returncode}（没能监听动作回调）")
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):  # pragma: no cover
                pass
    return result
