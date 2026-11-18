"""hub-qq-send：把一条投递发到 QQ（私聊/群），或者重推 pending、撤回。

语义（契约 INTERFACE.md §11.7d）：
- 新发：建一行 channel=qq 的投递，再调 qq-onebot 通道插件的 op=dispatch；
  发送成功 ⇒ status=delivered + remote_ref 带 message_id；失败 ⇒ 行留 pending 且记 last_error，
  退出码 1（调用方能区分「账已记、没发出去」）。
- --pending：重推 channel=qq 的 pending 行（op=retry），失败仍留 pending 并退出码 1。
- --dismiss ID：撤回（OneBot delete_msg）。

目标既可以是数字（uin / group_id），也可以是名字（好友备注/昵称/群名，按完全相等优先、包含其次）。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import qq_bridge
from .config import ConfigError, load
from .plugin import list_plugins, load_entry
from .store import Store, StoreError

EXIT_OK, EXIT_FATAL, EXIT_USAGE = 0, 1, 2

CHANNEL_NAME = "qq-onebot"


def channel_row(config):
    try:
        rows = list_plugins(config)
    except Exception:
        return None
    for row in rows:
        if (row.get("name") == CHANNEL_NAME and row.get("kind") == "channel"
                and row.get("status") == "ok" and not row.get("shadowed")):
            return row
    return None


def _parse(argv):
    p = argparse.ArgumentParser(prog="hub-qq-send",
                                description="发一条 QQ 消息（或重推 pending / 撤回）")
    p.add_argument("--to", default=None, help="uin / group_id / 好友备注或群名")
    p.add_argument("--group", action="store_true", help="目标是群（--to 按 group_id/群名解析）")
    p.add_argument("--title", default="", help="标题（与 body 一起拼成一条消息）")
    p.add_argument("--body", default="", help="正文")
    p.add_argument("--options", default=None, help="JSON 数组 [{key,label,description}]")
    p.add_argument("--kind", default=None, help="投递种类（缺省 msg）")
    p.add_argument("--pending", action="store_true", help="重推 channel=qq 的 pending 行")
    p.add_argument("--dismiss", default=None, metavar="DELIVERY_ID", help="撤回一条已发出的消息")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


def _out(args, payload: dict, text: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(text)


def main(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        config = load()
        store = Store(config.db_path)
        store.migrate()
    except (ConfigError, StoreError) as exc:
        print(f"配置/库错误: {exc}", file=sys.stderr)
        return EXIT_USAGE

    row = channel_row(config)
    if row is None:
        print("qq-onebot 通道未启用：hub.config.yaml 里需要 plugins.qq-onebot.enabled=true",
              file=sys.stderr)
        return EXIT_FATAL
    send = load_entry(row)

    try:
        if args.dismiss:
            out = send({"op": "dismiss", "config": config, "store": store,
                        "delivery_id": args.dismiss})
            _out(args, out, f"已撤回 {args.dismiss}（message_id={out.get('message_id')}）")
            return EXIT_OK

        if args.pending:
            rows = [r for r in store.deliveries(status="pending", channel="qq", limit=50)
                    if (r.get("remote_ref") or "").startswith("{")]
            if not rows:
                _out(args, {"ok": True, "pending": 0}, "没有可重推的 qq pending 行")
                return EXIT_OK
            done, failed = [], []
            for r in rows:
                try:
                    res = send({"op": "retry", "config": config, "store": store,
                                "delivery_id": r["id"]})
                    done.append({"id": r["id"], "message_id": res.get("message_id")})
                except Exception as exc:
                    failed.append({"id": r["id"], "error": str(exc)[:200]})
            payload = {"ok": not failed, "retried": len(rows), "delivered": done,
                       "failed": failed}
            _out(args, payload, f"重推 {len(rows)} 条：成 {len(done)} 败 {len(failed)}")
            return EXIT_OK if not failed else EXIT_FATAL

        if not args.to:
            print("要么给 --to，要么给 --pending / --dismiss", file=sys.stderr)
            return EXIT_USAGE

        ident = args.to
        if not ident.isdigit():      # 名字 ⇒ 先解析成 uin / group_id（取数的错也算用法错）
            try:
                typ, ident, _name = qq_bridge.resolve_target(config, ident, args.group)
            except qq_bridge.QQError as exc:
                print(f"解析目标失败: {exc}", file=sys.stderr)
                return EXIT_USAGE
        target = {"type": "group" if args.group else "private", "id": ident}
        if args.dry_run:
            _out(args, {"ok": True, "dry_run": True, "target": target,
                        "title": args.title, "body": args.body}, "dry-run：未发送")
            return EXIT_OK

        out = send({"op": "dispatch", "config": config, "store": store,
                    "target": target, "title": args.title, "body": args.body,
                    "options": args.options, "kind": args.kind})
    except Exception as exc:
        usage = type(exc).__name__ == "ChannelError"    # 入参错 ⇒ 退出码 2（库不变）
        print(f"{'用法错误' if usage else '发送失败'}: {exc}", file=sys.stderr)
        return EXIT_USAGE if usage else EXIT_FATAL

    if out.get("transport") == "pending":
        print(f"未发出去（行留 pending {out['delivery_id']}）: {out.get('immediate_error')}",
              file=sys.stderr)
        _out(args, out, f"pending {out.get('delivery_id')}")
        return EXIT_FATAL
    _out(args, out, f"已发送 {out.get('delivery_id')}  message_id={out.get('message_id')}")
    return EXIT_OK
