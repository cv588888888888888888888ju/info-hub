"""phone-mcp 通道命令（S9）：新推一条 / 重推 pending / 收回手机卡片。

语义与退出码见 plugins/channels/phone-mcp/README.md 与 INTERFACE.md §11.6；
MCP 细节复用 `infohub.phone_remind.bridge_call`，台账复用 `infohub.deliver`。
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError, load
from .plugin import list_plugins, load_entry, load_module
from .store import Store, StoreError

EXIT_OK, EXIT_FATAL, EXIT_USAGE = 0, 1, 2

CHANNEL_NAME = "phone-mcp"


def channel_row(config):
    """按插件清单找已启用的 phone-mcp 通道行；没有 ⇒ None（调用方退出码 1）。"""
    try:
        rows = list_plugins(config)
    except Exception:  # 插件宿主坏了也只等于「没有通道」
        return None
    for row in rows:
        if (row.get("name") == CHANNEL_NAME and row.get("kind") == "channel"
                and row.get("status") == "ok" and not row.get("shadowed")):
            return row
    return None


def channel_entry(config):
    row = channel_row(config)
    return load_entry(row) if row else None


def _parse(argv):
    p = argparse.ArgumentParser(prog="hub-phone-push",
                                description="推一条投递到手机（或重推 pending / 收回卡片）")
    p.add_argument("--title")
    p.add_argument("--body", default="")
    p.add_argument("--mode", default=None)
    p.add_argument("--options", default=None, help="JSON 数组，元素取 key/label/description")
    p.add_argument("--layout", default=None, help="layout v1 的 JSON 对象")
    p.add_argument("--todo-id", default=None)
    p.add_argument("--kind", default=None)
    p.add_argument("--pending", action="store_true", help="重推 channel=phone 且到点的 pending 行")
    p.add_argument("--dismiss", default=None, metavar="DELIVERY_ID")
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


def _out(args, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    if payload.get("delivery_id"):
        print(f"{payload.get('status') or 'ok'}\t{payload['delivery_id']}\t{payload.get('title') or ''}")
    else:
        print(json.dumps(payload, ensure_ascii=False))


def main_phone_push(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        config = load()
        store = Store(config.db_path)
        store.migrate()
    except (ConfigError, StoreError) as exc:
        print(f"配置/库错误: {exc}", file=sys.stderr)
        return EXIT_USAGE

    entry = channel_entry(config)
    if entry is None:
        print(f"{CHANNEL_NAME} 通道未启用或加载失败（查 plugins.{CHANNEL_NAME}.enabled）",
              file=sys.stderr)
        return EXIT_FATAL

    try:
        if args.dismiss:
            res = entry({"op": "dismiss", "config": config, "store": store,
                         "delivery_id": args.dismiss})
            _out(args, {"delivery_id": args.dismiss, "status": "dismissed",
                        "ok": res.get("ok")})
            return EXIT_OK if res.get("ok") else EXIT_FATAL

        if args.pending:
            mod = load_module(channel_row(config))
            rows = mod.due_pending(store)
            failed = 0
            for row in rows:
                try:
                    entry({"op": "retry", "config": config, "store": store,
                           "delivery_id": row["id"]})
                    _out(args, {"delivery_id": row["id"], "status": "delivered"})
                except Exception as exc:  # 单行失败不影响其余行
                    failed += 1
                    _out(args, {"delivery_id": row["id"], "status": "pending",
                                "error": str(exc)[:200]})
            return EXIT_OK if not failed else EXIT_FATAL

        if not args.title:
            print("--title 必填（或用 --pending / --dismiss）", file=sys.stderr)
            return EXIT_USAGE
        res = entry({"op": "dispatch", "config": config, "store": store,
                     "title": args.title, "body": args.body, "mode": args.mode,
                     "options": args.options, "layout": args.layout,
                     "todo_id": args.todo_id, "kind": args.kind})
        _out(args, {"delivery_id": res.get("delivery_id"), "status": "delivered",
                    "title": args.title, "ref": res.get("ref")})
        return EXIT_OK
    except Exception as exc:  # PushError / ChannelError / 任意插件异常
        print(f"推送失败: {exc}", file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main_phone_push())
