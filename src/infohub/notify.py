"""通知出口（M2）。门与失败语义见 INTERFACE.md §4.6、§5.5、§6。

一条通知要过五道门（顺序固定，任一不过即跳过并在 `gates` 里留痕）：

1. `enabled`  —— 开关，默认关
2. `dnd`      —— gsettings `show-banners=false` ⇒ 勿扰，不弹
3. `idle`     —— 空闲 ≥ `idle_max_s` ⇒ 人不在，不弹（用户 m01440：弹的那一瞬要贴着"我在做什么"）
4. `ui-event` —— `require_ui_event` 时，必须有一次**新的** `window` 事件（= 用户换了个界面）
5. `throttle` —— 最小间隔 / 每小时上限 / 同类每天上限，三者都只统计"真弹出去的"
6. `kind-suppressed`（M2c）—— 被「忽略此类」压住的类别直接跳过（选候选时就跳过，别的类别不会被它堵住）
7. `snoozed`  —— 候选被「稍后」推迟且还没到点（M2d）
8. `no-evidence`（M3c）—— 待办没有 `source_event_ids` ⇒ 跳过（§13 草案③「每条通知必须附依据，无依据不发」）。
   6/7/8 在同一处候选循环里判，**被跳过的候选不占位**（`continue`，不是 `break`）—— 无依据/被压/被推迟的那条不许饿死别的候选。

**探针失败一律 fail-closed**：分不清是否勿扰 / 是否空闲 ⇒ 不弹（宁可不发，也不打扰）。
发送失败不标记已通知（下一轮还会试），只落一条 `shown=0` 台账。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime

from . import events as E

UI_CURSOR = "notify:ui"          # collector_state 键：已消费到的 window 事件 id
#: 通知上的按钮（M2b）。**键名与待办状态的映射写死在 ACTION_STATUS**，不靠猜。
ACTIONS = ["acted", "完成", "open", "打开", "snooze", "稍后",
           "ignore", "忽略", "ignore-kind", "忽略此类"]
ACTION_STATUS = {"acted": "done", "完成": "done", "ignore": "dropped", "忽略": "dropped"}
#: 「打开」只留痕 + 可选开一个 URL，**不改待办状态**（点开 ≠ 做完）。
OPEN_ACTIONS = ("open", "打开")
#: 「稍后」把这条待办推迟 N 分钟（`notified_at` 清空 ⇒ 到期后会再提示一次），状态仍是 open。
SNOOZE_ACTIONS = ("snooze", "稍后")
#: 「忽略此类」不是普通动作：它压住**整个类别**若干天（M2c）。见 INTERFACE.md §4.6 门 6、§5.6。
IGNORE_KIND_ACTIONS = ("ignore-kind", "忽略此类")
#: 按钮标签 → 稳定键：D-Bus 传回来的是**显示标签**（可能是中文），台账/JSON 一律落键，别落标签。
CANON_ACTION = {"acted": "acted", "完成": "acted", "done": "acted",
                "open": "open", "打开": "open",
                "snooze": "snooze", "稍后": "snooze", "ignore": "ignore", "忽略": "ignore",
                "ignore-kind": "ignore-kind", "忽略此类": "ignore-kind"}
#: 默认按钮集：GNOME 横幅一般只显示前 3 个 ⇒ 默认就给三个（`notify.actions` 可改）。
DEFAULT_ACTIONS = ["打开", "稍后", "忽略此类"]
#: gdbus 回的是 `(uint32 70,)`
DBUS_ID_RE = re.compile(r"\(uint32\s+(\d+)\s*,?\)")
#: `gdbus monitor` 的信号行：`The signal org.freedesktop.Notifications.ActionInvoked(uint32 70, 'acted') received`
SIGNAL_RE = re.compile(r"(ActionInvoked|NotificationClosed)\(uint32\s+(\d+)(?:,\s*'?([^')]*)'?)?\)")
MONITOR_ARGS = ["gdbus", "monitor", "--session", "--dest", "org.freedesktop.Notifications"]
#: 电脑弹窗通道插件的清单名（S7）：dispatch / act / listen 都经它下发与结算（通道契约见该插件目录 README.md）
CHANNEL_NAME = "desktop-popup"
CHANNEL_KIND = "channel"
#: 空闲探针回的是 `(uint64 29866,)`（毫秒）。**只取括号里那个整数** —— 早期实现把所有数字字符
#: 连起来，把位宽 "64" 也吃进去（=> 6429.866 秒），于是永远判"人不在"。
IDLE_RE = re.compile(r"\(\s*(?:u?int\d+\s+)?(\d+)\s*,?\s*\)")
DEFAULT_ENV_TIMEOUT_S = 5.0


def _kind_of(todo: dict) -> str:
    """待办的类别 = 幂等键的规则段（与 `dispatch` 的门、台账的 kind 同一口径）。"""
    return str(todo.get("dedupe_key") or "todo").split(":")[0]


class ProbeError(Exception):
    """环境探针失败（gsettings / gdbus 不可用）。"""


class SendError(Exception):
    """通知发送失败（后端返回非 0 / 命令不存在）。"""


class ActionError(Exception):
    """动作落不了地（找不到对应通知 / 不认识的动作键 / 没有待办指针）。"""


def _source(value, default: str) -> str:
    """`*_source` 取值归一：YAML 里写 `off` 会被解析成 False，要当 "off" 用（坑，见 §4.6）。"""
    if value is False:
        return "off"
    if value is None or value is True:
        return default
    return str(value).strip().lower()


class Env:
    """真环境探针。语义见 INTERFACE.md §4.6；测试靠 PATH 注入假 gsettings/gdbus 覆盖。"""

    def __init__(self, timeout_s: float = DEFAULT_ENV_TIMEOUT_S):
        self.timeout_s = float(timeout_s)

    def _run(self, argv: list[str]) -> str:
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_s)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProbeError(f"{argv[0]}: {type(exc).__name__}: {exc}") from exc
        if out.returncode != 0:
            detail = (out.stderr or "").strip().splitlines()
            raise ProbeError(f"{argv[0]} exit={out.returncode}: {detail[-1][:120] if detail else ''}")
        return out.stdout.strip()

    def dnd(self) -> bool:
        text = self._run(["gsettings", "get", "org.gnome.desktop.notifications", "show-banners"])
        return text.strip().strip("'").lower() in ("false", "0")

    def idle_s(self) -> float:
        """mutter 空闲毫秒 → 秒（门 3 / M7 活跃判据共用；解析口径见 `IDLE_RE`）。"""
        text = self._run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                          "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                          "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"])
        match = IDLE_RE.search(text)
        if match:
            return int(match.group(1)) / 1000.0
        numbers = re.findall(r"\d+", text)
        return (int(numbers[-1]) / 1000.0) if numbers else 0.0

    def capabilities(self) -> list[str]:
        """拿不到能力清单不算错：只影响要不要带按钮。"""
        try:
            text = self._run(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                              "--object-path", "/org/freedesktop/Notifications",
                              "--method", "org.freedesktop.Notifications.GetCapabilities"])
        except ProbeError:
            return []
        return [w.strip("' ") for w in text.strip("()[], ").split(",") if w.strip("' ,")]


# --------------------------------------------------------------------------- 发送后端
def action_labels(config) -> list[str]:
    """通知上要带的按钮（显示标签 = 动作键）。`notify.actions` 可改；认不出的键直接丢掉（不猜）。

    S7 起通道插件也要这份按钮集（建台账 `options`），所以是公开函数。
    """
    raw = config.get("notify.actions")
    if not isinstance(raw, list) or not raw:
        raw = DEFAULT_ACTIONS
    labels = [str(x).strip() for x in raw]
    return [x for x in labels if x in ACTIONS]


#: 旧名保留（历史调用点/测试可能还在用）
_action_labels = action_labels


def _gdbus_argv(config, env: Env, summary: str, body: str) -> list[str]:
    app = str(config.get("notify.app_name") or "info-hub")
    timeout_ms = int(config.get("notify.timeout_ms") or 8000)
    actions = _action_labels(config) if "actions" in env.capabilities() else []
    return ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.Notify",
            app, "0", "", summary, body,
            json.dumps(actions, ensure_ascii=False).replace('"', "'"), "{}", str(timeout_ms)]


def _command_argv(config, summary: str, body: str, kind: str) -> list[str]:
    template = config.get("notify.command") or []
    if not isinstance(template, list) or not template:
        raise SendError("notify.backend=command 但 notify.command 为空")
    mapping = {"summary": summary, "body": body, "kind": kind,
               "app": str(config.get("notify.app_name") or "info-hub")}
    try:
        return [str(part).format(**mapping) for part in template]
    except (KeyError, IndexError) as exc:
        raise SendError(f"notify.command 占位符非法: {exc}") from exc


def send(config, env: Env, summary: str, body: str, kind: str, quiet: bool = False) -> str:
    """发一条通知。返回后端响应摘要（用于日志/台账）。失败抛 SendError。"""
    backend = str(config.get("notify.backend") or "gdbus")
    if backend == "stdout":
        print(f"[notify] {summary} :: {body}")
        return "stdout"
    if backend == "command":
        argv = _command_argv(config, summary, body, kind)
    elif backend == "gdbus":
        argv = _gdbus_argv(config, env, summary, body)
    else:
        raise SendError(f"未知 notify.backend: {backend}")

    try:
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=float(config.get("notify.send_timeout_s") or 15))
    except (OSError, subprocess.SubprocessError) as exc:
        raise SendError(f"{argv[0]}: {type(exc).__name__}: {exc}") from exc
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip().splitlines()
        raise SendError(f"{argv[0]} exit={out.returncode}: {detail[-1][:160] if detail else ''}")
    return (out.stdout or "").strip()[:200]


#: 自检探针的正文。**刻意不写成待办语气** —— 它回答的是"这条通道通不通"，不是"你该干活了"。
PROBE_BODY = "自检：通知通道正常（这条不是待办提示，不需要处理）"


def probe(config, store, dry_run: bool = False) -> dict:
    """自检（`hub-notify test`，M2 验收判据见 PROJECT.md §15）：真弹一条**探针**通知，台账记 `kind='probe'`。

    它**不是** B 形态，也不要被当成 B 形态的证据：
    - **刻意绕开门 1–8**（不看 enabled/勿扰/空闲/界面事件/节流）。理由是它要回答的问题是
      「这台机器上通知到底弹不弹得出来」，而这个答案**不能依赖"用户恰好切了界面"**。
    - **不碰 `todos`**：不改状态、不写 `notified_at`。
    - `todo_id` 为空 ⇒ `notification_effectiveness` 把它记成 `unlinked`、**不进样本**（见 store 该函数），
      所以它**不会污染降频判定**。
    - 真弹失败 ⇒ 返回 `error`，**不记台账**（没弹出去就没有"弹过"这回事），退出码仍 0 ——
      自检的结论是"通/不通"，不是"命令失败"。

    语义见 INTERFACE.md §4.6.1。
    """
    summary = str(config.get("notify.summary") or "info-hub")
    backend = str(config.get("notify.backend") or "gdbus")
    result: dict = {"at": E.iso(), "command": "test", "backend": backend, "summary": summary,
                    "body": PROBE_BODY, "dry_run": bool(dry_run), "sent": False,
                    "response": None, "notification_id": None, "ledger_id": None,
                    "error": None, "detail": ""}
    if dry_run:
        result["detail"] = "dry-run：没弹、没记账（只回报会发什么）"
        return result
    try:
        raw = send(config, Env(), summary, PROBE_BODY, "probe", quiet=True)
    except SendError as exc:
        result["error"] = str(exc)
        result["detail"] = "没弹出去 ⇒ 不记台账"
        return result
    result["sent"] = True
    result["response"] = raw
    result["notification_id"] = parse_dbus_id(raw)
    result["ledger_id"] = store.add_notification(kind="probe", body=PROBE_BODY,
                                                 basis_event_ids=[], shown=True,
                                                 todo_id=None, dbus_id=result["notification_id"])
    result["detail"] = f"已弹（台账 #{result['ledger_id']}，kind=probe）"
    return result


# --------------------------------------------------------------------------- 通道
def channel_entry(config):
    """按插件清单找 `kind=channel` 且 `name=desktop-popup` 的**已启用且加载成功**的通道。

    返回入口函数（`send(request)`）；没装 / 未启用 / 加载失败 ⇒ None（调用方跳过，不抛异常）。
    注册条件见通道目录 README.md §1：`notify.enabled` 与 `plugins.desktop-popup.enabled` 都要过。
    """
    from . import plugin as plugins
    try:
        for row in plugins.list_plugins(config):
            if str(row.get("name")) != CHANNEL_NAME or row.get("shadowed"):
                continue
            if row.get("status") != "ok":
                return None
            return plugins.load_entry(row)
    except Exception:   # 插件宿主自己坏了也只等于"没有通道"；dispatch 不许因此致命
        return None
    return None


# --------------------------------------------------------------------------- 闸门
def _skip(result: dict, reason: str, detail: str = "") -> dict:
    result["skipped"] = True
    result["reason"] = reason
    result["detail"] = detail
    result["gates"].append({"gate": reason, "ok": False, "detail": detail})
    return result


def dispatch(config, store, env: Env | None = None, dry_run: bool = False) -> dict:
    """评估一次通知。返回结构见 INTERFACE.md §2.4；`skipped=true` 不是错误。"""
    env = env or Env()
    result: dict = {"at": E.iso(), "sent": 0, "skipped": False, "reason": None, "detail": "",
                    "dry_run": bool(dry_run), "gates": [], "kind": None, "todo": None,
                    "body": None, "error": None, "enabled": bool(config.get("notify.enabled", False))}

    if not result["enabled"]:
        return _skip(result, "disabled", "notify.enabled=false")

    # 门 2：勿扰
    if bool(config.get("notify.respect_dnd", True)):
        source = _source(config.get("notify.dnd_source"), "gsettings")
        if source == "off":
            result["gates"].append({"gate": "dnd", "ok": True, "detail": "source=off"})
        else:
            try:
                if env.dnd():
                    return _skip(result, "dnd", "show-banners=false")
            except ProbeError as exc:
                return _skip(result, "probe-error", f"dnd: {exc}")
            result["gates"].append({"gate": "dnd", "ok": True, "detail": "show-banners=true"})

    # 门 3：空闲（人在不在）
    idle_max = float(config.get("notify.idle_max_s") or 0)
    if idle_max > 0:
        source = _source(config.get("notify.idle_source"), "mutter")
        idle = 0.0
        if source != "off":
            try:
                idle = env.idle_s()
            except ProbeError as exc:
                return _skip(result, "probe-error", f"idle: {exc}")
        if idle >= idle_max:
            return _skip(result, "idle", f"空闲 {idle:.0f}s ≥ {idle_max:.0f}s")
        result["gates"].append({"gate": "idle", "ok": True, "detail": f"{idle:.0f}s"})

    # 门 4：界面事件（用户"在做什么"的边界）
    ui_event_id = None
    if bool(config.get("notify.require_ui_event", True)):
        latest = store.latest_event("window")
        if latest is None:
            return _skip(result, "no-ui-event", "库里还没有 window 事件（换过界面才会有一条）")
        cursor = (store.state_get(UI_CURSOR) or {}).get("cursor") or {}
        seen = int(cursor.get("event_id") or 0)
        if int(latest["id"]) <= seen:
            return _skip(result, "no-ui-event", f"最新 window 事件 id={latest['id']} 已评估过")
        ui_event_id = int(latest["id"])
        result["gates"].append({"gate": "ui-event", "ok": True,
                                "detail": f"window event id={ui_event_id} at {latest['ts']}"})

    # 待办候选：跳过被「忽略此类」压住的类别、以及被「稍后」推迟还没到点的 —— 否则它们永远占着头一个候选，别的会饿死
    limit = int(config.get("notify.max_per_dispatch") or 1)
    pending = store.pending_notify_todos(limit=max(20, limit))
    if not pending:
        return _skip(result, "no-pending-todo", "没有还没提示过的 open 待办")
    todo = None
    suppressed_kinds: list[str] = []
    snoozed: list[tuple[str, str]] = []
    no_evidence: list[str] = []
    now_iso = E.iso()
    for cand in pending:
        cand_kind = _kind_of(cand)
        if store.kind_suppressed(cand_kind) is not None:
            suppressed_kinds.append(cand_kind)
            continue
        until = cand.get("snooze_until")
        if until and str(until) > now_iso:
            snoozed.append((str(cand["id"]), str(until)))
            continue
        # 门 8（M3c）：**无依据不发**（§13 草案③）—— 宁可不说，也不能说一句人没法核对的话
        if not list(cand.get("source_event_ids") or []):
            no_evidence.append(str(cand["id"]))
            continue
        todo = cand
        break
    if todo is None:
        if suppressed_kinds:
            return _skip(result, "kind-suppressed",
                         f"候选待办的类别都在「忽略此类」冷却中：{', '.join(sorted(set(suppressed_kinds)))}")
        if snoozed:
            return _skip(result, "snoozed",
                         f"候选都被「稍后」推迟（{snoozed[0][0]} 到 {snoozed[0][1]}）")
        if no_evidence:
            return _skip(result, "no-evidence",
                         f"候选待办没有依据事件（{no_evidence[0]} 等 {len(no_evidence)} 条）⇒ 不发")
        return _skip(result, "no-pending-todo", "没有还没提示过的 open 待办")
    kind = _kind_of(todo)
    basis = list(todo.get("source_event_ids") or [])
    body = f"{todo['title']}（依据 {len(basis)} 条事件）"
    result["evidence"] = len(basis)
    result["kind"] = kind
    result["todo"] = {"id": todo["id"], "title": todo["title"], "dedupe_key": todo.get("dedupe_key")}
    result["body"] = body

    # 门 5：节流（只统计真弹出去的）
    counts = store.notification_gate_counts(kind)
    min_interval = float(config.get("notify.min_interval_s") or 0)
    if min_interval > 0 and counts["last_shown"]:
        try:
            gap = (datetime.now().astimezone() - datetime.fromisoformat(counts["last_shown"])).total_seconds()
        except ValueError:
            gap = min_interval
        if gap < min_interval:
            return _skip(result, "throttled-min-interval", f"距上一条 {gap:.0f}s < {min_interval:.0f}s")
    max_hour = int(config.get("notify.max_per_hour") or 0)
    if max_hour and counts["shown_1h"] >= max_hour:
        return _skip(result, "throttled-hour", f"近 1 小时已弹 {counts['shown_1h']} ≥ {max_hour}")
    max_day_kind = int(config.get("notify.max_per_day_per_kind") or 0)
    if max_day_kind and counts["shown_today_kind"] >= max_day_kind:
        return _skip(result, "throttled-day", f"今天同类 {kind} 已弹 {counts['shown_today_kind']} ≥ {max_day_kind}")

    # 门 5b：有效性降频（M4a）——忽略率过高的类别自动收紧，等于「不管是降频，不是屏蔽」
    downshift = {row["kind"]: row for row in effectiveness(config, store)["kinds"] if row["downshifted"]}
    if kind in downshift:
        cap = int(config.get("notify.downshift_max_per_day") or 1)
        result["downshifted"] = downshift[kind]
        if counts["shown_today_kind"] >= cap:
            return _skip(result, "throttled-downshifted",
                         f"{kind} 被忽略率 {downshift[kind]['ignored_rate']:.0%} 判为降频"
                         f"，今天已弹 {counts['shown_today_kind']} ≥ {cap}")
        result["gates"].append({"gate": "downshift", "ok": True,
                                "detail": f"{kind} 降频中（今天第 {counts['shown_today_kind'] + 1}/{cap} 条）"})
    result["gates"].append({"gate": "throttle", "ok": True,
                            "detail": f"1h={counts['shown_1h']} today_kind={counts['shown_today_kind']}"})

    if dry_run:
        result["would_send"] = True
        result["gates"].append({"gate": "send", "ok": True, "detail": "dry-run，未发"})
        return result

    # 发送：S7 起经通道插件下发（投递台账那一行由插件写；本体只记自己的通知台账）
    summary = str(config.get("notify.summary") or "info-hub")
    entry = channel_entry(config)
    if entry is None:
        return _skip(result, "no-channel",
                     "desktop-popup 通道未启用 / 未安装 / 加载失败（见 hub-plugin list）")
    try:
        delivery = entry({"op": "dispatch", "config": config, "store": store, "env": env,
                          "title": todo["title"], "summary": summary, "body": body,
                          "kind": kind, "todo_id": todo["id"], "mode": "single"})
    except Exception as exc:  # 插件内异常不许把 dispatch 打成致命错误（S7 失败语义）
        delivery = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not delivery.get("ok"):
        store.add_notification(kind, body, basis, shown=False, todo_id=todo["id"])
        result["error"] = str(delivery.get("error") or "通道下发失败")
        result["delivery"] = delivery
        result["gates"].append({"gate": "send", "ok": False, "detail": result["error"]})
        return result

    dbus_id = delivery.get("dbus_id")
    store.add_notification(kind, body, basis, shown=True, todo_id=todo["id"], dbus_id=dbus_id)
    store.mark_todo_notified(todo["id"])
    result["dbus_id"] = dbus_id
    if ui_event_id is not None:
        store.state_put(UI_CURSOR, cursor={"event_id": ui_event_id}, ok=True)
    result["sent"] = 1
    result["backend_reply"] = delivery.get("backend_reply")
    result["delivery"] = {k: delivery.get(k) for k in
                          ("delivery_id", "remote_ref", "status")}
    result["actions"] = action_labels(config)
    result["gates"].append({"gate": "send", "ok": True, "detail": str(delivery.get("backend_reply"))})
    return result


def status(config, store, env: Env | None = None) -> dict:
    """`hub-notify status`：把五道门的现状摊开（不改任何状态）。"""
    env = env or Env()
    out: dict = {"enabled": bool(config.get("notify.enabled", False)),
                 "backend": str(config.get("notify.backend") or "gdbus"),
                 "require_ui_event": bool(config.get("notify.require_ui_event", True)),
                 "pending_todos": len(store.pending_notify_todos(limit=1000)),
                 "notifications": store.notifications(limit=10)}
    counts = store.notification_gate_counts("")
    out["shown_total"] = counts["shown_total"]
    out["shown_1h"] = counts["shown_1h"]
    out["last_shown"] = counts["last_shown"]
    for name, source, probe in (("dnd", _source(config.get("notify.dnd_source"), "gsettings"), env.dnd),
                                ("idle_s", _source(config.get("notify.idle_source"), "mutter"), env.idle_s)):
        if source == "off":
            out[name] = None
            continue
        try:
            out[name] = probe()
        except ProbeError as exc:
            out[name] = None
            out[f"{name}_error"] = str(exc)
    out["ui_cursor"] = (store.state_get(UI_CURSOR) or {}).get("cursor")
    out["latest_window_event"] = store.latest_event("window")
    out["suppress_days"] = int(config.get("notify.suppress_days") or 7)
    out["suppressions"] = store.suppressions(active_only=True)
    out["actions"] = _action_labels(config)
    out["snooze_minutes"] = int(config.get("notify.snooze_minutes") or 60)
    out["downshifted"] = [row["kind"] for row in effectiveness(config, store)["kinds"]
                          if row["downshifted"]]
    return out


def effectiveness(config, store) -> dict:
    """通知有效性 + 自动降频判定（M4a）。判据见 INTERFACE.md §4.6。

    `downshifted` = 样本 ≥ `downshift_min_samples` 且忽略率 > `downshift_threshold`。
    降频的效果只体现在门 5b：该类每天最多 `downshift_max_per_day` 条（不是屏蔽）。
    """
    window = int(config.get("notify.effectiveness_window_days") or 14)
    ignore_after = int(config.get("notify.ignore_after_hours") or 24)
    min_samples = int(config.get("notify.downshift_min_samples") or 5)
    threshold = float(config.get("notify.downshift_threshold") or 0.5)
    kinds = []
    for row in store.notification_effectiveness(window_days=window, ignore_after_hours=ignore_after):
        row["downshifted"] = bool(row["samples"] >= min_samples
                                  and row["ignored_rate"] is not None
                                  and row["ignored_rate"] > threshold)
        kinds.append(row)
    return {"window_days": window, "ignore_after_hours": ignore_after,
            "min_samples": min_samples, "threshold": threshold,
            "downshift_max_per_day": int(config.get("notify.downshift_max_per_day") or 1),
            "kinds": kinds}

# --------------------------------------------------------------------------- 动作回调（M2b）
def parse_dbus_id(text) -> int | None:
    """从 gdbus 回显里取系统通知 id；取不到返回 None（不算错，只是回写不了）。"""
    m = DBUS_ID_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def suppress(config, store, kind: str, days: int | None = None, note: str | None = None) -> dict:
    """把某类通知压住（M2c）。`days` 缺省取 `notify.suppress_days`。"""
    days = int(days if days is not None else (config.get("notify.suppress_days") or 7))
    if not str(kind or "").strip():
        raise ActionError("kind 不能为空")
    if days <= 0:
        raise ActionError("days 必须是正整数（要解除抑制请用 unsuppress）")
    return store.suppress_kind(kind, days, note=note, source="cli")


def unsuppress(config, store, kind: str) -> dict:
    """解除某类的抑制（返回它原来到什么时候解除）。"""
    row = store.kind_suppressed(kind) or {}
    removed = store.clear_kind_suppression(kind)
    return {"kind": str(kind), "removed": bool(removed), "was_until": row.get("until")}


def suppressions(store, active_only: bool = True) -> list[dict]:
    return store.suppressions(active_only=active_only)


def settle_action(config, store, dbus_id, action, reason: str = "notify-action") -> dict:
    """把「用户点了通知上的哪个按钮」落成待办状态（留痕见 INTERFACE.md §5.5）。

    `acted/完成` ⇒ 待办 `done`；`ignore/忽略` ⇒ `dropped`；一律 `human=True`（人点的，规则不许翻回去）。
    S7 起**由通道插件在写完 deliveries 台账之后调用**（本函数不写投递台账）；`hub-notify act` 见下面的 `act()`。
    """
    row = store.notification_by_dbus_id(dbus_id)
    if row is None:
        raise ActionError(f"没有 dbus_id={dbus_id} 对应的通知（可能已被清理或不是本进程发的）")
    key = str(action or "").strip()
    key = CANON_ACTION.get(key, key)
    if key in IGNORE_KIND_ACTIONS:
        # 「忽略此类」：① 这一条也不再提示（待办 dropped，人点的）② 整个类别压住 N 天（到期自动恢复）
        kind = str(row.get("kind") or "").strip()
        if not kind:
            raise ActionError(f"通知 #{row['id']} 没有 kind，无法按类别抑制")
        days = int(config.get("notify.suppress_days") or 7)
        rec = store.suppress_kind(kind, days,
                                  note=f"一键忽略此类（来自通知 #{row['id']}）", source="notify-action")
        todo_id = row.get("todo_id")
        if todo_id:
            store.set_todo_status(str(todo_id), "dropped", human=True)
        store.mark_notification_action(int(row["id"]), key)
        return {"at": E.iso(), "dbus_id": int(dbus_id), "action": key, "status": "dropped",
                "kind": kind, "days": days, "suppressed_until": rec["until"],
                "todo_id": str(todo_id) if todo_id else None,
                "notification_id": int(row["id"]), "reason": reason}
    status = ACTION_STATUS.get(key)
    todo_id = row.get("todo_id")

    if key in OPEN_ACTIONS:
        # 「打开」：只留痕 +（可选）开一个 URL，**不改待办状态** —— 点开不等于做完。
        url = str(config.get("notify.open_url") or "")
        template = config.get("notify.open_command") or []
        info = {"at": E.iso(), "dbus_id": int(dbus_id), "action": key, "status": None,
                "todo_id": str(todo_id) if todo_id else None, "url": url or None,
                "opened": False, "open_error": None, "notification_id": int(row["id"]), "reason": reason}
        if isinstance(template, list) and template and url:
            try:
                argv = [str(part).format(url=url, kind=str(row.get("kind") or ""),
                                         todo=str(todo_id or "")) for part in template]
            except (KeyError, IndexError) as exc:
                info["open_error"] = f"notify.open_command 占位符非法: {exc}"
            else:
                try:
                    done = subprocess.run(argv, capture_output=True, text=True,
                                          timeout=float(config.get("notify.open_timeout_s") or 5))
                    info["opened"] = done.returncode == 0
                    if done.returncode != 0:
                        detail = (done.stderr or done.stdout or "").strip().splitlines()
                        info["open_error"] = f"{argv[0]} exit={done.returncode}: {detail[-1][:120] if detail else ''}"
                except (OSError, subprocess.SubprocessError) as exc:
                    # 打开失败只是「没打开」，**不影响动作留痕、也不让点击算失败**（fail-soft）
                    info["open_error"] = f"{argv[0]}: {type(exc).__name__}: {exc}"
        store.mark_notification_action(int(row["id"]), key)
        return info

    if key in SNOOZE_ACTIONS:
        # 「稍后」：推迟 N 分钟；清 notified_at ⇒ 到点后会再提示一次。状态仍是 open（人没说做完了）。
        if not todo_id:
            raise ActionError(f"通知 #{row['id']} 没有 todo_id 指针（老数据），无法推迟")
        minutes = int(config.get("notify.snooze_minutes") or 60)
        until = E.iso_from_epoch(time.time() + minutes * 60)
        if not store.set_todo_snooze(str(todo_id), until):
            raise ActionError(f"待办不存在：{todo_id}")
        store.mark_notification_action(int(row["id"]), key)
        return {"at": E.iso(), "dbus_id": int(dbus_id), "action": key, "status": "open",
                "todo_id": str(todo_id), "snooze_until": until, "snooze_minutes": minutes,
                "notification_id": int(row["id"]), "reason": reason}

    if status is None:
        raise ActionError(f"不认识的动作键 {action!r}（已知：{', '.join(ACTIONS)}）")
    if not todo_id:
        raise ActionError(f"通知 #{row['id']} 没有 todo_id 指针（老数据），无法回写")
    if not store.set_todo_status(str(todo_id), status, human=True):
        raise ActionError(f"待办不存在：{todo_id}")
    store.mark_notification_action(int(row["id"]), key)
    return {"at": E.iso(), "dbus_id": int(dbus_id), "action": key, "status": status,
            "todo_id": str(todo_id), "notification_id": int(row["id"]), "reason": reason}


def act(config, store, dbus_id, action, reason: str = "notify-action", text=None) -> dict:
    """按钮动作入口：经 `desktop-popup` 通道插件「先写投递台账、再结算待办」（S7）。

    没有可用通道（没装 / 未启用）时退回 `settle_action`：老数据仍能结算，不报致命错误。
    """
    entry = channel_entry(config)
    if entry is None:
        return settle_action(config, store, dbus_id, action, reason=reason)
    return entry({"op": "act", "config": config, "store": store, "dbus_id": dbus_id,
                  "action": action, "text": text, "reason": reason})


def listen(config, store, once: bool = False, timeout_s=None, lines=None, max_lines: int = 500) -> dict:
    """监听系统通知的动作回调 → 投递台账 + 待办状态（S7 起由通道插件解析 monitor 输出）。

    - `lines` 给测试注入（可迭代的字符串行）；否则真起 `gdbus monitor`（常驻，靠 `timeout_s` 收工）。
    - 没有可用通道 ⇒ 返回 `stopped=no-channel`（空账，不抛异常）；monitor 起不来 / 异常退出 ⇒ `ActionError`。

    **真机实测（Fedora 44 / GNOME Shell 50.5）**：`NotificationClosed` 既不在 `CloseNotification` 时发、
    也不在通知到期时发 ⇒ `closed` 计数可能恒为 0（只是账，不影响动作回写）。`ActionInvoked` 必须真人点按钮，
    所以无头环境要验收动作链路就用 `hub-notify act <dbus_id> <action>`（同一套台账 + 结算路径）。
    """
    entry = channel_entry(config)
    if entry is None:
        return {"at": E.iso(), "signals": 0, "acted": 0, "ignored": 0, "suppressed": 0,
                "opened": 0, "snoozed": 0, "closed": 0, "handled": [], "errors": [],
                "stopped": "no-channel"}
    return entry({"op": "listen", "config": config, "store": store, "once": bool(once),
                  "timeout_s": timeout_s, "lines": lines, "max_lines": int(max_lines)})
