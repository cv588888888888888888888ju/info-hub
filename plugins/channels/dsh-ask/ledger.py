#!/usr/bin/env python3
"""dsh-ask 插件自带台账工具（S11）：独立进程直连 info-hub 库，补 §11.5 的三个缺口。

为什么存在：INTERFACE.md §11.5 只有 pending / ack / reply / claim 四个 HTTP 路由，
没有 ①建行 ②按 id 读行 ③置 expired。插件不另建库、不重写表，而是用 py 直连同一份
infohub 库：建行/读行/置 expired 走本脚本，HTTP 只用于 pending 探活 + claim。
缺口与 S18 的补齐项见 INTERFACE.md §12.6。

子命令与语义（键表见 INTERFACE.md §12.5）：
  add --channel C --mode M --title T [--body B] [--options JSON] [--layout JSON]
      [--todo-id ID] [--kind K]        建一行 status=pending，stdout 只打新 id
  get <id>                             按 id 读整行，stdout 打单行 JSON；没有 ⇒ 1
  expire <id>                          置 status=expired 并清空租约；stdout 打单行 JSON
  ack <id> [--action KEY]              同 bin/hub-deliver ack（幂等），stdout 打单行 JSON
  reply <id> --text T                  同 bin/hub-deliver reply（原样），stdout 打单行 JSON

退出码与 bin/hub-deliver 对齐：0 成功 / 1 未知 id、空回复、配置或存储致命 / 2 用法错误。
本文件注释只写指针；表结构、状态枚举、幂等语义一律以 INTERFACE.md §11 与 §12 为准。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

EXIT_OK, EXIT_FATAL, EXIT_USAGE = 0, 1, 2


def _ensure_sys_path() -> None:
    """index.mjs 已把 <hub_root>/src 塞进 PYTHONPATH；这里只做兜底（手工跑时也能用）。"""
    root = os.environ.get("DSH_ASK_HUB_ROOT") or ""
    if not root:
        return
    src = str(Path(root) / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


_ensure_sys_path()

try:
    from infohub import config as config_mod
    from infohub import deliver as D
    from infohub.store import Store, StoreError
except ImportError as exc:  # pragma: no cover - 只在 PYTHONPATH 没配好时走到
    print(f"fatal: 无法导入 infohub（检查 PYTHONPATH / DSH_ASK_HUB_ROOT）: {exc}",
          file=sys.stderr)
    raise SystemExit(EXIT_FATAL)


class NotFound(Exception):
    pass


def _row_json(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def _open_store(path: str | None = None) -> Store:
    config = config_mod.load(path)
    config.ensure_dirs()
    store = Store(config.db_path)
    store.migrate()
    return store


def expire(store: Store, delivery_id: str) -> dict:
    """置 expired 并清空租约（= 释放认领）；实现已下沉到 Store.expire_delivery。

    语义（§11.4/§11.5）：已是 replied/acked/suppressed 的行保留原终态，但租约照样清空
    —— expired 表示「作废」，不能把已经答过的行降级。
    """
    row = store.expire_delivery(delivery_id)
    if row is None:
        raise NotFound(f"投递不存在: {delivery_id}")
    return row


def cmd_add(store: Store, args) -> int:
    row = D.create(store, channel=args.channel, title=args.title, body=args.body or "",
                   mode=args.mode, options=args.options, layout=args.layout,
                   todo_id=args.todo_id, kind=args.kind)
    print(row["id"], flush=True)
    print(f"已建投递 {row['id']}  channel={row['channel']}  status={row['status']}",
          file=sys.stderr, flush=True)
    return EXIT_OK


def cmd_get(store: Store, args) -> int:
    row = store.delivery(args.id)
    if row is None:
        raise NotFound(f"投递不存在: {args.id}")
    print(_row_json(row), flush=True)
    return EXIT_OK


def cmd_expire(store: Store, args) -> int:
    print(_row_json(expire(store, args.id)), flush=True)
    return EXIT_OK


def cmd_ack(store: Store, args) -> int:
    print(_row_json(D.ack(store, args.id, action=args.action)), flush=True)
    return EXIT_OK


def cmd_reply(store: Store, args) -> int:
    print(_row_json(D.reply(store, args.id, args.text)), flush=True)
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger.py",
        description="dsh-ask 插件台账（INTERFACE.md §12；补 §11.5 缺口）")
    parser.add_argument("--config", default=None,
                        help="传给 infohub.config.load（缺省 $HUB_CONFIG → 项目内 hub.config.yaml）")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="建一条 channel=dsh 的投递（status=pending）")
    p_add.add_argument("--channel", required=True)
    p_add.add_argument("--mode", default="question", choices=list(D.MODES) + [""])
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--body", default="")
    p_add.add_argument("--options", default=None, help="JSON 数组 [{key,label,description}]")
    p_add.add_argument("--layout", default=None, help="整份界面 JSON（§13）")
    p_add.add_argument("--todo-id", dest="todo_id", default=None)
    p_add.add_argument("--kind", default=None)

    p_get = sub.add_parser("get", help="按 id 读整行（单行 JSON）")
    p_get.add_argument("id")

    p_expire = sub.add_parser("expire", help="置 expired 并释放租约（单行 JSON）")
    p_expire.add_argument("id")

    p_ack = sub.add_parser("ack", help="确认看见（幂等；不改待办状态）")
    p_ack.add_argument("id")
    p_ack.add_argument("--action", default=None)

    p_reply = sub.add_parser("reply", help="自由文本回复（原样留档）")
    p_reply.add_argument("id")
    p_reply.add_argument("--text", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    try:
        store = _open_store(args.config)
    except Exception as exc:  # 配置/库致命
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        handler = {"add": cmd_add, "get": cmd_get, "expire": cmd_expire,
                   "ack": cmd_ack, "reply": cmd_reply}[args.cmd]
        return handler(store, args)
    except D.DeliveryError as exc:      # 用法/取值 ⇒ 2
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (NotFound, D.DeliveryNotFound, D.EmptyReply) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except StoreError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
