"""投递台账（S4）：一条消息一行，手机 / 电脑 / DSH 三条通道共用一本账。

表与列、状态与模式取值、四个命令的入参与退出码**一律**见 INTERFACE.md §11 —— 本文注释只写指针，
不抄语义、不抄数值。SQL 全在 `store.py`（与其它层一致）；这里只做取值校验、id 生成与命令实现。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime

from .config import ConfigError, load
from .store import Store, StoreError

# 取值枚举以 INTERFACE.md §11.2 为准。
MODES = ("single", "status", "question")
STATUSES = ("pending", "delivered", "acked", "replied", "suppressed", "expired", "failed")
ACTIONS = ("ack", "done", "snooze", "ignore-kind", "reply", "open", "acted", "ignore")

EXIT_OK, EXIT_FATAL, EXIT_USAGE = 0, 1, 2


class DeliveryError(Exception):
    """取值/用法不合法（CLI 退出码 2；例如 `--mode` 不在枚举、`--options` 不是数组）。"""


class DeliveryNotFound(Exception):
    """未知投递 id（CLI 退出码 1；不做任何写入）。"""


class EmptyReply(Exception):
    """回复文本为空（CLI 退出码 1；库不变 —— 见 INTERFACE.md §11.3）。"""


# --------------------------------------------------------------------- 取值校验
def new_id(now: datetime | None = None) -> str:
    """新投递 id：`d-<yyyyMMddHHmmss>-<6 位十六进制>`（时间前缀让排序可读，随机尾避免同秒撞车）。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d%H%M%S")
    return f"d-{stamp}-{secrets.token_hex(3)}"


def parse_mode(value: str | None) -> str | None:
    """模式：`single`/`status`/`question`；缺省或空串 ⇒ None（通道自定）。"""
    if value is None:
        return None
    mode = str(value).strip()
    if not mode:
        return None
    if mode not in MODES:
        raise DeliveryError(f"不认识的 --mode: {mode}（可选 {'/'.join(MODES)}）")
    return mode


def parse_options(value) -> list | None:
    """`--options` 的 JSON 数组；每个元素只保留 `key`/`label`/`description` 三键。"""
    if value is None:
        return None
    raw = value
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise DeliveryError(f"--options 不是合法 JSON: {exc}") from exc
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise DeliveryError("--options 必须是 JSON 数组 [{key,label,description}]")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise DeliveryError("--options 的每个元素必须是对象")
        key = item.get("key")
        if not isinstance(key, str) or not key.strip():
            raise DeliveryError("--options 的每个元素必须有非空 key")
        out.append({k: item[k] for k in ("key", "label", "description") if k in item})
    return out


def parse_layout(value) -> dict | None:
    """`--layout` 的整份界面 JSON（对象）；`NULL`/空串 ⇒ None（通道默认版式）。"""
    if value is None:
        return None
    raw = value
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise DeliveryError(f"--layout 不是合法 JSON: {exc}") from exc
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DeliveryError("--layout 必须是 JSON 对象")
    return raw


# --------------------------------------------------------------------- 台账操作
def parse_attachments(value) -> list | None:
    """附件 = 数据根相对路径的数组（语义见 INTERFACE.md §11.9）；绝对路径 / `..` 一律拒。"""
    if value is None or value == "":
        return None
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise DeliveryError(f"--attachments 不是 JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise DeliveryError("--attachments 必须是数组")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise DeliveryError("--attachments 的每项都必须是非空字符串")
        rel = item.strip()
        if rel.startswith("/") or "\\" in rel or "\x00" in rel:
            raise DeliveryError(f"附件必须是数据根相对路径: {rel}")
        if any(part in ("", ".", "..") for part in rel.split("/")):
            raise DeliveryError(f"附件路径非法: {rel}")
        out.append(rel)
    return out or None


def create(store: Store, channel: str, title: str, body: str = "", mode: str | None = None,
           options=None, layout=None, todo_id: str | None = None, kind: str | None = None,
           delivery_id: str | None = None, attachments=None) -> dict:
    """建一行 `status=pending` 的投递（通道插件也走这里，不自己拼 SQL）。"""
    channel = (channel or "").strip()
    if not channel:
        raise DeliveryError("--channel 不能为空")
    title = (title or "").strip()
    if not title:
        raise DeliveryError("--title 不能为空")
    return store.add_delivery({
        "id": delivery_id or new_id(),
        "channel": channel,
        "mode": parse_mode(mode),
        "kind": kind,
        "todo_id": (todo_id or None),
        "title": title,
        "body": body if body is not None else "",
        "options": parse_options(options),
        "layout": parse_layout(layout),
        "attachments": parse_attachments(attachments),
        "status": "pending",
    })


def ack(store: Store, delivery_id: str, action: str | None = None) -> dict:
    """确认「看见了」：幂等，不改待办状态（INTERFACE.md §11.3/§11.4）。"""
    key = (action or "").strip() or None
    if key is not None and key not in ACTIONS:
        raise DeliveryError(f"不认识的 --action: {key}（可选 {'/'.join(ACTIONS)}）")
    row = store.ack_delivery(delivery_id, action=key)
    if row is None:
        raise DeliveryNotFound(f"投递不存在: {delivery_id}")
    return row


def expire(store: Store, delivery_id: str) -> dict:
    """作废一行（跳过提问）：status=expired、清租约；语义见 INTERFACE.md §11.4。"""
    row = store.expire_delivery(delivery_id)
    if row is None:
        raise DeliveryNotFound(f"投递不存在: {delivery_id}")
    return row


def reply(store: Store, delivery_id: str, text: str) -> dict:
    """自由文本原样留档；空文本 ⇒ EmptyReply（退出码 1，库不变）。"""
    if not text or not str(text).strip():
        raise EmptyReply("--text 不能为空（空回复退出码 1，库不变）")
    row = store.reply_delivery(delivery_id, text)
    if row is None:
        raise DeliveryNotFound(f"投递不存在: {delivery_id}")
    return row


def list_rows(store: Store, status: str | None = None, channel: str | None = None,
              limit: int = 50, archived: bool | None = None) -> list[dict]:
    """列台账；`archived` 语义见 INTERFACE.md §11.9。"""
    return store.deliveries(status=status, channel=channel, limit=limit, archived=archived)


def archive(store: Store, delivery_id: str) -> dict:
    """归档一行（幂等）；不存在 ⇒ DeliveryNotFound。"""
    row = store.archive_delivery(delivery_id)
    if row is None:
        raise DeliveryNotFound(f"投递不存在: {delivery_id}")
    return row


# --------------------------------------------------------------------- 认领租约（S5）
def pending(store: Store, channel: str | None = None, limit: int = 50) -> list[dict]:
    """待认领的行（HTTP GET /api/deliver/pending 的语义，INTERFACE.md §11.5）。"""
    return store.pending_deliveries(channel=channel, limit=limit)


def claim(store: Store, delivery_id: str, owner: str, ttl_s: int) -> dict:
    """认领一行：写 lease_owner / lease_until；重复 claim 同一 id 只有最新 owner 生效（§11.5）。"""
    owner = (owner or "").strip()
    if not owner:
        raise DeliveryError("owner 不能为空")
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int):
        raise DeliveryError("ttl_s 必须是整数")
    if not 1 <= ttl_s <= 604800:
        raise DeliveryError("ttl_s 越界（1 … 604800）")
    row = store.claim_delivery(delivery_id, owner, ttl_s)
    if row is None:
        raise DeliveryNotFound(f"投递不存在: {delivery_id}")
    return row


# --------------------------------------------------------------------- 命令输出
def _human_row(row: dict) -> str:
    line = (f"{str(row.get('created_at') or '')[:16].replace('T', ' ')}  "
            f"{row.get('channel') or '-':<10} {str(row.get('mode') or '-'):<8} "
            f"{row.get('status') or '-':<10} {row.get('id')}  "
            f"{str(row.get('title') or '')[:40]}")
    if row.get("reply") is not None:
        line += f"  reply={str(row['reply'])[:40]}"
    return line


def _cmd_add(store: Store, args) -> int:
    row = create(store, channel=args.channel, title=args.title, body=args.body,
                 mode=args.mode, options=args.options, layout=args.layout,
                 todo_id=args.todo_id, attachments=args.attachments)
    if args.json:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)
    else:
        # stdout 只打新 id（脚本直接取），人读摘要走 stderr（INTERFACE.md §2.1 的分工）。
        print(row["id"], flush=True)
        print(f"已建投递 {row['id']}  channel={row['channel']}  status={row['status']}",
              file=sys.stderr, flush=True)
    return EXIT_OK


def _cmd_list(store: Store, args) -> int:
    # 缺省只看未归档（--archived 相反），语义见 INTERFACE.md §11.9。
    rows = list_rows(store, status=args.status, channel=args.channel, limit=args.limit,
                     archived=True if args.archived else False)
    if args.json:
        print(json.dumps({"deliveries": rows}, ensure_ascii=False, sort_keys=True), flush=True)
        return EXIT_OK
    if not rows:
        print("（还没有投递）")
        return EXIT_OK
    for row in rows:
        print(_human_row(row))
    return EXIT_OK


def _cmd_ack(store: Store, args) -> int:
    row = ack(store, args.id, action=args.action)
    changed = "true" if row.get("changed") else "false"
    if args.json:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)
    else:
        print(f"{row['id']}  status={row['status']}  changed={changed}"
              f"  acked_at={row.get('acked_at') or '-'}", flush=True)
    return EXIT_OK


def _cmd_archive(store: Store, args) -> int:
    row = archive(store, args.id)
    if args.json:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)
    else:
        print(f"{row['id']}  archived_at={row.get('archived_at') or '-'}", flush=True)
    return EXIT_OK


def _cmd_reply(store: Store, args) -> int:
    row = reply(store, args.id, args.text)
    if args.json:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)
    else:
        print(f"{row['id']}  status={row['status']}  reply_at={row.get('reply_at') or '-'}",
              flush=True)
    return EXIT_OK


# --------------------------------------------------------------------- 入口
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hub-deliver",
        description="投递台账（S4）：建投递行 / 列 / 确认 / 回复。语义见 INTERFACE.md §11")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="建一条投递（status=pending）")
    p_add.add_argument("--channel", required=True, help="通道名（computer / phone / dsh …）")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--body", default="")
    p_add.add_argument("--mode", default=None, choices=list(MODES) + [""])
    p_add.add_argument("--options", default=None, help="JSON 数组 [{key,label,description}]")
    p_add.add_argument("--layout", default=None, help="整份界面 JSON（INTERFACE.md §13）")
    p_add.add_argument("--todo-id", dest="todo_id", default=None)
    p_add.add_argument("--attachments", default=None, help="JSON 数组：数据根相对路径（§11.9）")
    p_add.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="列投递（按 created_at 倒序）")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--status", default=None, choices=list(STATUSES))
    p_list.add_argument("--channel", default=None)
    p_list.add_argument("--limit", type=int, default=50, help="最多几条（0 = 全部）")
    p_list.add_argument("--archived", action="store_true",
                        help="只看已归档（缺省只看未归档；语义见 INTERFACE.md §11.9）")

    p_ack = sub.add_parser("ack", help="确认看见（不改待办状态；重复 ack 幂等）")
    p_ack.add_argument("id")
    p_ack.add_argument("--action", default=None, help="稳定动作键（§11.2）")
    p_ack.add_argument("--json", action="store_true")

    p_reply = sub.add_parser("reply", help="自由文本回复（原样留档）")
    p_reply.add_argument("id")
    p_reply.add_argument("--text", required=True)
    p_reply.add_argument("--json", action="store_true")

    p_arch = sub.add_parser("archive", help="归档一行（幂等；默认列表不再显示）")
    p_arch.add_argument("id")
    p_arch.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    try:
        config = load(args.config)
        config.ensure_dirs()
        store = Store(config.db_path)
        store.migrate()
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        handler = {"add": _cmd_add, "list": _cmd_list, "ack": _cmd_ack,
                   "reply": _cmd_reply, "archive": _cmd_archive}[args.cmd]
        return handler(store, args)
    except DeliveryError as exc:                 # 用法/取值 ⇒ 退出码 2
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (DeliveryNotFound, EmptyReply) as exc:  # 语义拒绝 ⇒ 退出码 1
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except StoreError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()
