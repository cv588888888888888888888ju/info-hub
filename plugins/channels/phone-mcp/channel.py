"""phone-mcp 通道插件（S9）：把投递行推到手机 MCP，手机回执走投递 HTTP。

单函数入口 `send(request)`；request 形状、op 语义与失败语义见同目录 `README.md`。
MCP 调用**复用** `infohub.phone_remind.bridge_call`（initialize → session → tools/call），不另写协议；
台账写入口**复用** `infohub.deliver`（S4），不自己拼 INSERT。

推送顺序按 A10 取舍：新推送**先推手机、成功后才落台账行**（失败退出码 1 且库里不留行）；
已存在的 pending 行重推（op=retry）失败则留 pending、记 attempts/last_error（退避依据，见 backoff_s）。
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from datetime import datetime

from infohub import deliver as D
from infohub import events as E
from infohub.phone_remind import BridgeError, bridge_call

#: 台账里的通道名（INTERFACE.md §11.1 的 `phone`）
CHANNEL = "phone"
OPS = ("dispatch", "retry", "dismiss")
#: 允许就地回写的列；列语义见 INTERFACE.md §11.1
PATCHABLE = ("status", "remote_ref", "last_error", "attempts")
#: 退避表（秒）：第 n 次失败后的等待；超出表长一律封顶（INTERFACE.md §11.6 的手机段）
BACKOFF = (60, 120, 240, 600)
#: 后端选择（README §6）：bridge = jxev App（只读卡），helper = 自制可回复通知 APK
BACKENDS = ("bridge", "helper", "auto")
#: auto 模式自检结果缓存：(adb, device) -> helper 是否可用
_READY_CACHE: dict[tuple[str, str], bool] = {}
#: helper 默认入口。投放用 am start（实测 HyperOS/MIUI 的 Greezer 会直接丢 adb 广播：
#: "Greezer Denial ... process is not permitted to auto start"）；广播只作换 ROM 的兜底。
HELPER_PKG = "com.chenty.hubreplier"
HELPER_ACTIVITY = "com.chenty.hubreplier/.HubActivity"
HELPER_SHOW = "com.chenty.hubreplier/.ShowReceiver"
HELPER_DISMISS = "com.chenty.hubreplier/.DismissReceiver"


class ChannelError(Exception):
    """通道入参不合法（未知 op / 缺字段 / 不认识的 mode 或 options）。"""


class PushError(Exception):
    """手机侧拒绝或不可达（调用方退出码 1）。"""


def send(request: dict) -> dict:
    req = dict(request or {})
    op = str(req.get("op") or "dispatch")
    if op == "dispatch":
        return _dispatch(req)
    if op == "retry":
        return _retry(req)
    if op == "dismiss":
        return _dismiss(req)
    raise ChannelError(f"不认识的 op: {op}（只认 {'/'.join(OPS)}）")


def _tool(config) -> str:
    return str(config.get("phone_remind.deliver_tool") or "hub_deliver")


def _dismiss_tool(config) -> str:
    return str(config.get("phone_remind.dismiss_tool") or "hub_dismiss")


def _payload(delivery_id: str, mode: str, title: str, body: str,
             options, layout) -> dict:
    """手机侧入参：有 layout ⇒ 带 layout + body；layout 缺失 ⇒ 回落 title + text（A17）。"""
    if layout is None:
        return {"delivery_id": delivery_id, "mode": mode, "title": title, "text": body}
    payload = {"delivery_id": delivery_id, "mode": mode, "title": title,
               "body": body, "layout": layout}
    if options:
        payload["options"] = options
    return payload


def _push(config, delivery_id: str, mode: str, title: str, body: str,
          options, layout) -> dict:
    """按 backend 分流（README §6）；auto = helper 装好就用 helper，否则回落 bridge。"""
    if _use_helper(config):
        return _push_helper(config, delivery_id, mode, title, body, options, layout)
    return _push_bridge(config, delivery_id, mode, title, body, options, layout)


def _push_bridge(config, delivery_id: str, mode: str, title: str, body: str,
                 options, layout) -> dict:
    payload = _payload(delivery_id, mode, title, body, options, layout)
    try:
        out = bridge_call(config, _tool(config), payload)
    except BridgeError as exc:
        raise PushError(f"手机不可达: {exc}") from exc
    if not isinstance(out, dict) or out.get("ok") is False:
        raise PushError(f"手机拒绝推送: {json.dumps(out, ensure_ascii=False)[:300]}")
    return out


# ------------------------------------------------------------------ helper 后端
def _backend(config) -> str:
    name = str(config.get("phone_remind.backend") or "bridge").strip() or "bridge"
    if name not in BACKENDS:
        raise ChannelError(f"不认识的 backend: {name}（只认 {'/'.join(BACKENDS)}）")
    return name


def _use_helper(config) -> bool:
    backend = _backend(config)
    if backend == "helper":
        return True
    if backend == "bridge":
        return False
    return _helper_ready(config)


def _helper_ready(config) -> bool:
    """auto 模式的自检：adb 在 + 设备填了 + helper 包已装。结果按 (adb, device) 缓存一次。"""
    key = (str(config.get("phone_remind.adb") or "adb"), str(config.get("phone_remind.device") or ""))
    if key[1] and key in _READY_CACHE:
        return _READY_CACHE[key]
    ready = False
    try:
        out = _adb(config, ["shell", "pm", "list", "packages", _helper_pkg(config)])
        ready = _helper_pkg(config) in out
    except PushError:
        ready = False
    if key[1]:
        _READY_CACHE[key] = ready
    return ready


def _helper_pkg(config) -> str:
    return str(config.get("phone_remind.helper_pkg") or HELPER_PKG)


def _adb(config, args: list[str]) -> str:
    """跑一次 adb（带 -s 设备）；失败一律折成 PushError，调用方退出码 1。"""
    exe = str(config.get("phone_remind.adb") or "adb")
    if not (shutil.which(exe) or shutil.os.path.exists(exe)):
        raise PushError(f"adb 不可用: {exe}（phone_remind.adb 配错或没装 platform-tools）")
    device = str(config.get("phone_remind.device") or "").strip()
    if not device:
        raise PushError("helper 后端需要 phone_remind.device（adb devices 里的序列号）")
    cmd = [exe, "-s", device, *args]
    timeout = float(config.get("phone_remind.adb_timeout_s") or 15)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PushError(f"adb 超时（{timeout:g}s）: {' '.join(cmd)}") from exc
    except OSError as exc:
        raise PushError(f"adb 起不来: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise PushError(f"adb 失败（rc={proc.returncode}）: {detail}")
    return proc.stdout or ""


def _start(config, component: str, extras: dict) -> str:
    """am start 到 helper 的 Activity（payload 走 base64，避开设备端再套一层 shell 引号）。"""
    args = ["shell", "am", "start", "-n", component]
    for key, value in extras.items():
        args += ["--es", key, str(value)]
    out = _adb(config, args)
    low = (out or "").lower()
    if "error" in low or "exception" in low or "does not exist" in low:
        raise PushError(f"helper 起不来: {out.strip()[:300]}")
    if "starting:" not in low and "activity" not in low:
        raise PushError(f"helper 没有确认启动: {out.strip()[:300]}")
    return out


def _broadcast(config, component: str, extras: dict) -> str:
    args = ["shell", "am", "broadcast", "-n", component]
    for key, value in extras.items():
        args += ["--es", key, str(value)]
    out = _adb(config, args)
    low = (out or "").lower()
    if "exception" in low or "error:" in low or "securityexception" in low:
        raise PushError(f"helper 拒绝广播: {out.strip()[:300]}")
    if "broadcast completed" not in low:
        raise PushError(f"helper 没有确认广播: {out.strip()[:300]}")
    return out


def _transport(config) -> str:
    name = str(config.get("phone_remind.helper_transport") or "activity").strip() or "activity"
    if name not in ("activity", "broadcast"):
        raise ChannelError(f"不认识的 helper_transport: {name}（只认 activity/broadcast）")
    return name


def _push_helper(config, delivery_id: str, mode: str, title: str, body: str,
                 options, layout) -> dict:
    # helper 的 extras 契约用 id/body（见 tools/hubreplier/README.md §2），与 bridge 的
    # delivery_id/text 不同名，这里做一次显式映射，两边字段都带上以便排查。
    payload = {"id": delivery_id, "delivery_id": delivery_id, "mode": mode,
               "title": title, "body": body or "", "text": body or ""}
    if options:
        payload["options"] = options
    if layout is not None:
        payload["layout"] = layout
    # token 跟着 phone_api 走：配置缺省时用 $HUB_ROOT/phone_api.token（自动生成），不写死在仓库里
    from infohub.secret import get_token
    token = get_token(config, explicit=str(config.get("phone_remind.helper_token") or ""))
    if token:
        payload["token"] = token
    raw = json.dumps(payload, ensure_ascii=False)
    if _transport(config) == "broadcast":
        component = str(config.get("phone_remind.helper_show") or HELPER_SHOW)
        _broadcast(config, component, {"payload": raw})
        return {"ok": True, "ref": delivery_id, "backend": "helper", "transport": "broadcast"}
    activity = str(config.get("phone_remind.helper_activity") or HELPER_ACTIVITY)
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    _start(config, activity, {"op": "show", "payload_b64": b64})
    return {"ok": True, "ref": delivery_id, "backend": "helper", "transport": "activity"}


def _dispatch(req: dict) -> dict:
    config, store = req["config"], req["store"]
    title = str(req.get("title") or "").strip()
    if not title:
        raise ChannelError("title 不能为空")
    body = str(req.get("body") or "")
    mode = D.parse_mode(req.get("mode")) or "single"
    options = D.parse_options(req.get("options"))
    layout = D.parse_layout(req.get("layout"))

    delivery_id = D.new_id()
    out = _push(config, delivery_id, mode, title, body, options, layout)
    row = D.create(store, channel=CHANNEL, title=title, body=body, mode=mode,
                   options=options, layout=layout, todo_id=req.get("todo_id"),
                   kind=req.get("kind"), delivery_id=delivery_id)
    ref = str(out.get("ref") or out.get("delivery_id") or "") or None
    _patch(store, delivery_id, status="delivered", remote_ref=ref, attempts=0)
    return {"ok": True, "delivery_id": delivery_id, "mode": mode, "ref": ref,
            "backend": out.get("backend") or "bridge",
            "row": store.delivery(delivery_id) or row}


def _retry(req: dict) -> dict:
    """重推一条已存在的 pending 行；失败留 pending + attempts/last_error 后抛 PushError。"""
    config, store = req["config"], req["store"]
    delivery_id = str(req.get("delivery_id") or "").strip()
    if not delivery_id:
        raise ChannelError("delivery_id 不能为空")
    row = store.delivery(delivery_id)
    if row is None:
        raise ChannelError(f"投递不存在: {delivery_id}")
    try:
        out = _push(config, delivery_id, row.get("mode") or "single", row.get("title") or "",
                    row.get("body") or "", row.get("options"), row.get("layout"))
    except PushError as exc:
        record_failure(store, delivery_id, str(exc))
        raise
    ref = str(out.get("ref") or out.get("delivery_id") or "") or None
    _patch(store, delivery_id, status="delivered", remote_ref=ref, attempts=0)
    return {"ok": True, "delivery_id": delivery_id, "ref": ref,
            "row": store.delivery(delivery_id)}


def _dismiss(req: dict) -> dict:
    config = req["config"]
    delivery_id = str(req.get("delivery_id") or "").strip()
    if not delivery_id:
        raise ChannelError("delivery_id 不能为空")
    if _use_helper(config):
        if _transport(config) == "broadcast":
            component = str(config.get("phone_remind.helper_dismiss") or HELPER_DISMISS)
            _broadcast(config, component, {"id": delivery_id})
        else:
            activity = str(config.get("phone_remind.helper_activity") or HELPER_ACTIVITY)
            _start(config, activity, {"op": "dismiss", "id": delivery_id})
        return {"ok": True, "delivery_id": delivery_id, "backend": "helper"}
    try:
        out = bridge_call(config, _dismiss_tool(config), {"delivery_id": delivery_id})
    except BridgeError as exc:
        raise PushError(f"手机不可达: {exc}") from exc
    return {"ok": bool(out.get("ok", True)), "delivery_id": delivery_id, "remote": out}


def _patch(store, delivery_id: str, **fields) -> dict | None:
    """回写投递行少数几列（白名单见 PATCHABLE）；store.py 没有通用更新入口。"""
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


# --------------------------------------------------------------------- 退避重推
def backoff_s(attempts: int) -> int:
    """第 attempts 次失败后该等多少秒（attempts>=1；超出表长取最后一档）。"""
    n = max(1, int(attempts))
    return BACKOFF[min(n, len(BACKOFF)) - 1]


def due_pending(store, now: str | None = None, limit: int = 50) -> list[dict]:
    """挑出「该重推」的 pending 行：attempts=0 ⇒ 立刻；否则 updated_at + backoff 已到点。"""
    now = now or E.iso()
    out = []
    for row in store.pending_deliveries(channel=CHANNEL, limit=limit):
        attempts = int(row.get("attempts") or 0)
        if attempts == 0:
            out.append(row)
            continue
        try:
            waited = (datetime.fromisoformat(now)
                      - datetime.fromisoformat(str(row.get("updated_at")))).total_seconds()
        except ValueError:
            out.append(row)
            continue
        if waited >= backoff_s(attempts):
            out.append(row)
    return out


def record_failure(store, delivery_id: str, error: str) -> dict | None:
    """失败行留 pending 并记 attempts/last_error（退避依据）。"""
    row = store.delivery(str(delivery_id)) or {}
    return _patch(store, delivery_id, status="pending",
                  attempts=int(row.get("attempts") or 0) + 1,
                  last_error=str(error)[:300])
