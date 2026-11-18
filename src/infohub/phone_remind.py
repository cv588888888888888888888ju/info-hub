"""手机强提醒通道（2026-11-10）：把 info-hub 待办推到手机的顶部常驻条 / 锁屏全屏页。

为什么单独一条通道：桌面通知是弱提醒；用户要的是「一直挂在页面顶端」+「锁屏也能看」。
手机侧由 com.jxev.ai 的 ReminderOverlay（顶部条）与 ReminderActivity（showWhenLocked 全屏页）
实现；本模块只负责「选哪条待办 + 发过去 + 记账 + 收回执」。

契约见 INTERFACE.md §2.7b。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from . import config as config_mod
from .events import iso
from .store import Store, StoreError

EXIT_OK, EXIT_FATAL, EXIT_USAGE = 0, 1, 2


class BridgeError(Exception):
    pass


def _post(url: str, body: dict, headers: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode()
    hd = {"Content-Type": "application/json",
          "Accept": "application/json, text/event-stream"}
    hd.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hd, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers, resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise BridgeError(f"bridge HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(f"bridge 不可达: {exc}") from exc


def _parse(text: str) -> dict:
    """streamable-http 可能回 SSE（data: {...}）也可能回裸 JSON。"""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def bridge_call(config, tool: str, arguments: dict, timeout: float | None = None) -> dict:
    """跑一次 MCP tools/call（initialize → 会话头 → 调用）。"""
    timeout = float(config.get("phone_remind.timeout_s", timeout or 30))
    url = config.get("phone_remind.bridge", "http://<bridge-host>:9500/mcp")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "hub-phone-remind", "version": "1"}}}
    headers, _ = _post(url, init, timeout=timeout)
    sid = headers.get("mcp-session-id")
    hs = {"mcp-session-id": sid} if sid else {}
    if sid:
        try:
            _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, hs, timeout=10)
        except BridgeError:
            pass
    _, resp = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": tool, "arguments": arguments}}, hs, timeout=timeout)
    payload = _parse(resp)
    if "error" in payload:
        raise BridgeError(str(payload["error"]))
    result = payload.get("result", {})
    content = result.get("content") or []
    if isinstance(content, list) and content:
        try:
            return json.loads(content[0].get("text", "{}"))
        except json.JSONDecodeError:
            return {"raw": content[0].get("text")}
    return result


def cmd_push(config, todo_id: str, fullscreen: bool, dry_run: bool) -> int:
    store = Store(config.db_path)
    store.migrate()
    try:
        detail = store.todo_detail(todo_id)
    except StoreError as exc:
        print(f"待办读不出来: {exc}", file=sys.stderr)
        return EXIT_FATAL
    if not detail or not detail.get("todo"):
        print(f"没有这条待办: {todo_id}", file=sys.stderr)
        return EXIT_FATAL
    todo = detail["todo"]
    if todo.get("status") not in ("open", "doing"):
        print(f"待办状态是 {todo.get('status')}，不再打扰: {todo_id}", file=sys.stderr)
        return EXIT_FATAL
    params = {"title": todo.get("title") or "提醒",
              "text": todo.get("why") or "",
              "todo_id": todo_id,
              "fullscreen": bool(fullscreen),
              "timeout": 0}
    if dry_run:
        print(json.dumps({"dry_run": True, "action": "reminder_show", "params": params},
                         ensure_ascii=False))
        return EXIT_OK
    try:
        out = bridge_call(config, "device_action",
                          {"action_type": "reminder_show", "parameters": params})
    except BridgeError as exc:
        print(f"推送失败: {exc}", file=sys.stderr)
        return EXIT_FATAL
    data = out.get("data") or {}
    ok = bool(data.get("shown") or data.get("success"))
    if ok:
        store.mark_todo_notified(todo_id)
    print(json.dumps({"todo_id": todo_id, "title": todo.get("title"), "shown": ok,
                      "fullscreen": data.get("fullscreen"), "keyguard": data.get("keyguard")},
                     ensure_ascii=False))
    return EXIT_OK if ok else EXIT_FATAL


def cmd_sync(config, limit: int, dry_run: bool) -> int:
    """把 open/doing 待办全量推到手机，供顶部常驻条 + 列表页显示。"""
    store = Store(config.db_path)
    store.migrate()
    rows = []
    for status in ("open", "doing"):
        rows.extend(store.todos(status=status, limit=limit))
    items = []
    for t in rows[:limit]:
        items.append({
            "id": t.get("id"),
            "kind": (t.get("dedupe_key") or "manual").split(":")[0],
            "title": (t.get("title") or "")[:100],
            "why": (t.get("why") or "")[:160],
            "status": t.get("status"),
        })
    payload = {"count": len(items), "items": items}
    if dry_run:
        print(json.dumps({"dry_run": True, "count": payload["count"],
                          "bytes": len(json.dumps(payload, ensure_ascii=False))},
                         ensure_ascii=False))
        return EXIT_OK
    try:
        out = bridge_call(config, "device_action",
                          {"action_type": "hub_sync", "parameters": payload},
                          timeout=max(30.0, len(items) / 10.0))
    except BridgeError as exc:
        print(f"同步失败: {exc}", file=sys.stderr)
        return EXIT_FATAL
    data = out.get("data") or {}
    print(json.dumps({"count": data.get("count", len(items)), "ok": bool(data.get("success"))},
                     ensure_ascii=False))
    return EXIT_OK


def cmd_clear(config) -> int:
    out = bridge_call(config, "device_action", {"action_type": "reminder_clear", "parameters": {}})
    print(json.dumps(out.get("data") or out, ensure_ascii=False))
    return EXIT_OK


def cmd_acks(config, apply: bool) -> int:
    store = Store(config.db_path)
    store.migrate()
    out = bridge_call(config, "device_action", {"action_type": "reminder_ack_drain", "parameters": {}})
    data = out.get("data") or {}
    acks = data.get("acks") or []
    applied = []
    for ack in acks:
        tid = ack.get("todo_id") or ""
        if not tid:
            continue
        if apply:
            store.write_todo_revision(tid, "acked_at", None, iso(),
                                      author="human", reason="phone-ack")
            applied.append(tid)
    print(json.dumps({"count": len(acks), "acks": acks, "applied": applied}, ensure_ascii=False))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-phone-remind",
                                     description="info-hub 手机强提醒通道（顶部条 / 锁屏全屏）")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push")
    p_push.add_argument("todo_id")
    p_push.add_argument("--fullscreen", action="store_true", help="强制全屏（锁屏会自动用）")
    p_push.add_argument("--dry-run", action="store_true")
    p_push.add_argument("--json", action="store_true")

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--limit", type=int, default=400)
    p_sync.add_argument("--dry-run", action="store_true")

    sub.add_parser("clear")
    p_acks = sub.add_parser("acks")
    p_acks.add_argument("--apply", action="store_true", help="给回执的待办写 acked_at 留痕")

    args = parser.parse_args(argv)
    config = config_mod.load(args.config)
    config.ensure_dirs()
    try:
        if args.cmd == "push":
            return cmd_push(config, args.todo_id, args.fullscreen, args.dry_run)
        if args.cmd == "sync":
            return cmd_sync(config, args.limit, args.dry_run)
        if args.cmd == "clear":
            return cmd_clear(config)
        if args.cmd == "acks":
            return cmd_acks(config, args.apply)
        return EXIT_USAGE
    except BridgeError as exc:
        print(f"bridge 错误: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except StoreError as exc:
        print(f"库错误: {exc}", file=sys.stderr)
        return EXIT_FATAL
