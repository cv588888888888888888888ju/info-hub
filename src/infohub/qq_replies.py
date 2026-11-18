"""hub-qq-replies：把 QQ 回来的消息当「回复」结算到投递台账。

语义（契约 INTERFACE.md §11.7d / §11.4）：
- 读事件 spool（与采集器同一个文件，自己的游标 key=qq-replies，字节 offset）；
- 只看 message 事件里「对方发的」：私聊 user_id / 群聊 group_id 对上某条 channel=qq 的
  delivered/pending/claimed 投递行的 remote_ref，取最近一条未回复的；
- 有 options 时把「1/n」或 label/key 命中的那个选项记成选项标签，否则原样记自由文本；
- 写入走 infohub.deliver.reply（status=replied，不改待办状态）。

进程形态：一次跑一遍（配 systemd timer 或 cron），幂等（游标 + 已 replied 的行不再结算）。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import deliver as D
from . import qq_bridge
from .config import ConfigError, load
from .store import Store, StoreError

EXIT_OK, EXIT_FATAL, EXIT_USAGE = 0, 1, 2
NODE = "qq-replies"
OPEN_STATUSES = ("delivered", "pending", "claimed")


def _parse(argv):
    p = argparse.ArgumentParser(prog="hub-qq-replies",
                               description="把 QQ 回来的消息结算成投递回复")
    p.add_argument("--limit", type=int, default=50, help="单轮最多结算几条（缺省 50）")
    p.add_argument("--max-bytes", type=int, default=4 * 1024 * 1024)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


def _cursor(store) -> dict:
    return dict(((store.state_get(NODE) or {}).get("cursor")) or {})


def _target_of(row: dict) -> dict:
    try:
        meta = json.loads(row.get("remote_ref") or "{}")
    except json.JSONDecodeError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _match_option(options, text: str) -> str | None:
    """把回复文字对到选项：阿拉伯数字 1..n、或与 key/label 相等（忽略首尾空白与大小写）。"""
    if not isinstance(options, list):
        return None
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            item = options[idx] if isinstance(options[idx], dict) else {}
            return str(item.get("label") or item.get("key") or raw)
    low = raw.lower()
    for item in options:
        if not isinstance(item, dict):
            continue
        for field in ("label", "key"):
            value = str(item.get(field) or "").strip()
            if value and value.lower() == low:
                return str(item.get("label") or item.get("key") or raw)
    return None


def settle(config, store, limit: int, max_bytes: int, dry_run: bool) -> dict:
    cur = _cursor(store)
    offset = int(cur.get("offset") or 0)
    chunk, new_offset = qq_bridge.read_spool(config, offset, max_bytes)

    delivered: list[dict] = []
    unmatched = 0
    scanned = 0
    for raw_line in chunk.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue          # 坏行不影响整轮（采集器那边负责报错）
        if not isinstance(event, dict) or event.get("post_type") != "message":
            continue
        scanned += 1
        self_id = str(event.get("self_id") or "")
        user = str(event.get("user_id") or "")
        if self_id and user == self_id:
            continue
        kind = str(event.get("message_type") or "")
        if kind == "group":
            want_type, want_id = "group", str(event.get("group_id") or "")
        elif kind == "private":
            want_type, want_id = "private", user
        else:
            continue
        text = qq_bridge.message_text(event.get("message"))
        if not text:
            continue
        rows = [r for r in store.deliveries(channel="qq", limit=100, archived=False)
                if r.get("status") in OPEN_STATUSES and not r.get("reply")
                and _target_of(r).get("type") == want_type
                and str(_target_of(r).get("id") or "") == want_id]
        if not rows:
            unmatched += 1
            continue
        row = rows[0]                      # deliveries() 已按 created_at/rowid 倒序
        chosen = _match_option(row.get("options"), text)
        payload = chosen or text
        if not dry_run:
            try:
                D.reply(store, row["id"], payload)
            except Exception as exc:       # EmptyReply / DeliveryNotFound
                unmatched += 1
                continue
        delivered.append({"delivery_id": row["id"], "from": want_id,
                          "text": payload, "option": chosen})
        if len(delivered) >= limit:
            break

    if not dry_run:
        store.state_put(NODE, cursor={"offset": new_offset,
                                      "fail_streak": 0}, ok=True, error=None)
    return {"ok": True, "scanned": scanned, "replied": delivered,
            "unmatched": unmatched, "cursor": new_offset}


def main(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        config = load()
        store = Store(config.db_path)
        store.migrate()
    except (ConfigError, StoreError) as exc:
        print(f"配置/库错误: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        out = settle(config, store, args.limit, args.max_bytes, args.dry_run)
    except qq_bridge.QQError as exc:
        print(f"读 spool 失败: {exc}", file=sys.stderr)
        return EXIT_FATAL
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"扫 {out['scanned']} 条，结算 {len(out['replied'])} 条，"
              f"没对上 {out['unmatched']} 条（cursor={out['cursor']}）")
        for item in out["replied"]:
            print(f"  {item['delivery_id']}  ← {item['from']}: {item['text']}")
    return EXIT_OK
