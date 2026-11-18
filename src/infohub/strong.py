"""强推送（S13）：一条消息同时弹手机全屏提醒 + 电脑桌面对话框。

语义契约见 INTERFACE.md §11.7c；这里只做编排，两条腿各自复用现成实现：
- 手机：建一行 kind=strong 的投递（台账可见、等回复），随后调 bridge 的
  device_action / reminder_show 立刻全屏提醒（由手机侧 MCP App 执行，需要它的无障碍服务保活）。
  可回复的全屏页由 helper（com.chenty.hubreplier）轮询到 kind=strong 时拉起（StrongActivity）。
- 电脑：`infohub.desktop_popup` 的 zenity 模态对话框（可选项、可打字回复）。
两条腿都落一份投递台账（channel=phone / computer），单独失败不影响另一条腿。
"""

from __future__ import annotations

import json
import sys

from . import deliver as D
from . import desktop_popup
from . import events as E
from .config import load
from .phone_remind import BridgeError, bridge_call
from .store import Store, StoreError

EXIT_OK, EXIT_FATAL, EXIT_USAGE = 0, 1, 2


def _patch(store: Store, delivery_id: str, **fields) -> None:
    """回写少数几列（与 phone-mcp 通道同一套白名单）。"""
    sets, args = [], []
    for key, value in fields.items():
        if key not in ("status", "remote_ref", "last_error", "attempts"):
            raise ValueError(f"不允许回写的列: {key}")
        sets.append(f"{key} = ?")
        args.append(value)
    sets.append("updated_at = ?")
    args.append(E.iso())
    args.append(str(delivery_id))
    store.conn.execute(f"UPDATE deliveries SET {', '.join(sets)} WHERE id = ?", tuple(args))
    store.conn.commit()


def _phone(config, store, title: str, body: str, options, layout) -> dict:
    """手机腿：只建 kind=strong 的 pending 行，由 helper 轮询拉起可回复的全屏页。

    不再调用 bridge 的 reminder_show —— 那条路会同时弹 com.jxev.ai 的 ReminderActivity
    （不可回复），与 helper 的 StrongActivity 撞成两个弹窗（用户 2026-11-18 明确不要并存）。
    """
    row = D.create(store, channel="phone", title=title, body=body, kind="strong",
                   options=options, layout=layout)
    return {"ok": True, "delivery_id": row["id"], "status": "pending",
            "transport": "helper-poll", "signaled": False}


def _desktop(config, store, title: str, body: str, options, ask_reply: bool = True) -> dict:
    """电脑腿：桌面强弹窗（zenity 模态，可选项/可回一句），不是系统通知那种弱提醒。"""
    row = D.create(store, channel="computer", title=title, body=body, kind="strong",
                   options=options)
    try:
        out = desktop_popup.show(title, body or "", options=options,
                                 timeout=desktop_popup.DEFAULT_TIMEOUT, ask_reply=ask_reply)
    except Exception as exc:  # 没有 zenity / 会话不可用都算这一条腿失败
        _patch(store, row["id"], status="pending", attempts=1, last_error=str(exc)[:300])
        return {"ok": False, "delivery_id": row["id"], "error": str(exc)[:300]}
    reply = out.get("reply")
    if reply:
        store.reply_delivery(row["id"], reply)
    else:
        _patch(store, row["id"], status="delivered", remote_ref=None, attempts=0,
               last_error=None)
    return {"ok": True, "delivery_id": row["id"], "reply": reply,
            "option": out.get("option"), "canceled": out.get("canceled")}


def cmd_strong(config, title: str, body: str, options=None, phone: bool = True,
               desktop: bool = True, ask_reply: bool = True, dry_run: bool = False) -> int:
    title = (title or "").strip()
    if not title:
        print("--title 必填", file=sys.stderr)
        return EXIT_USAGE
    if not (phone or desktop):
        print("至少选一条腿（--phone / --desktop，缺省两条都发）", file=sys.stderr)
        return EXIT_USAGE
    opts = D.parse_options(options)
    if dry_run:
        print(json.dumps({"dry_run": True, "title": title, "body": body,
                          "options": opts, "phone": phone, "desktop": desktop},
                         ensure_ascii=False))
        return EXIT_OK
    store = Store(config.db_path)
    store.migrate()
    out: dict = {"title": title}
    if phone:
        out["phone"] = _phone(config, store, title, body, opts, None)
    if desktop:
        out["desktop"] = _desktop(config, store, title, body, opts, ask_reply=ask_reply)
    ok = all((out.get(k) or {}).get("ok") for k in ("phone", "desktop") if k in out)
    print(json.dumps(out, ensure_ascii=False))
    return EXIT_OK if ok else EXIT_FATAL


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="hub-strong-push",
                                description="强推送：手机全屏提醒 + 电脑桌面对话框（同一条消息）")
    p.add_argument("--config", default=None)
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--options", default=None, help="JSON 数组，元素取 key/label")
    p.add_argument("--option", action="append", default=None,
                   help="选项标签，可重复（等价于 --options）")
    p.add_argument("--phone", action="store_true")
    p.add_argument("--desktop", action="store_true")
    p.add_argument("--no-reply", action="store_true", help="电脑只给确定、不收回复")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    every = not (args.phone or args.desktop)
    options = args.options
    if args.option:
        options = [{"key": o, "label": o} for o in args.option]
    try:
        config = load(args.config)
        config.ensure_dirs()
        return cmd_strong(config, args.title, args.body, options=options,
                          phone=args.phone or every, desktop=args.desktop or every,
                          ask_reply=not args.no_reply, dry_run=args.dry_run)
    except (BridgeError, StoreError) as exc:
        print(f"强推送失败: {exc}", file=sys.stderr)
        return EXIT_FATAL
