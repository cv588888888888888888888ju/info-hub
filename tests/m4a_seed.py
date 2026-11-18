#!/usr/bin/env python3
"""M4a 验收夹具：造「已弹通知 + 关联待办」的合成历史（判定口径见 INTERFACE.md §4.6）。

用法：
  python3 tests/m4a_seed.py <kind> <acted> <ignored> <fresh> <today_shown> <candidates> [extra]

参数语义（时间一律相对 now）：
  acted       通知弹于 48h 前，关联待办**之后**被标 done      ⇒ 算「已响应」
  ignored     通知弹于 48h 前，关联待办仍 open               ⇒ 算「被忽略」（超判定期）
  fresh       通知弹于 1h 前，关联待办仍 open                ⇒ 算「待判定期」（pending）
  today_shown 通知弹于此刻（shown=1）                        ⇒ 占今天的同类配额
  candidates  新建 N 条 open 且 notified_at=NULL 的待办      ⇒ dispatch 的候选
  extra       逗号分隔；`unlinked` = 再补 6 条没有 todo_id 的老通知（不进样本）

只写本机 SQLite，不联网、不弹窗。退出码 0；参数错 = 2。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, "src")

from infohub.config import load          # noqa: E402
from infohub.store import Store          # noqa: E402

USAGE = "usage: m4a_seed.py <kind> <acted> <ignored> <fresh> <today_shown> <candidates> [extra]"


def main(argv: list[str]) -> int:
    if len(argv) < 7:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        kind = argv[1]
        acted, ignored, fresh, today, cand = (int(x) for x in argv[2:7])
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    extra = {x.strip() for x in (argv[7] if len(argv) > 7 else "").split(",") if x.strip()}

    cfg = load()
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    store.migrate()

    now = datetime.now().astimezone()
    tag = f"{int(time.time() * 1000) % 10 ** 7}"
    old = (now - timedelta(hours=48)).isoformat(timespec="seconds")
    recent = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    now_s = now.isoformat(timespec="seconds")

    def mk_todo(key: str, title: str) -> str:
        tid, _ = store.upsert_todo({"dedupe_key": key, "title": title, "why": "m4a 夹具",
                                    "confidence": 0.9, "source_event_ids": [1]})
        return tid

    def mk_note(ts: str, todo_id: str | None) -> None:
        store.conn.execute(
            "INSERT INTO notifications (ts, kind, body, basis_event_ids, shown, action, todo_id)"
            " VALUES (?, ?, ?, '[]', 1, NULL, ?)", (ts, kind, f"{kind} 合成通知", todo_id))
        store.conn.commit()

    for i in range(acted):
        tid = mk_todo(f"{kind}:m4a-{tag}-acted-{i}", f"{kind} 已响应样张 {i}")
        mk_note(old, tid)
        store.set_todo_status(tid, "done", human=True)   # updated_at=now > 通知时间 ⇒ acted
    for i in range(ignored):
        mk_note(old, mk_todo(f"{kind}:m4a-{tag}-ignored-{i}", f"{kind} 被忽略样张 {i}"))
    for i in range(fresh):
        mk_note(recent, mk_todo(f"{kind}:m4a-{tag}-fresh-{i}", f"{kind} 待判定期样张 {i}"))
    for i in range(today):
        mk_note(now_s, mk_todo(f"{kind}:m4a-{tag}-today-{i}", f"{kind} 今天已弹样张 {i}"))
    for i in range(cand):
        mk_todo(f"{kind}:m4a-{tag}-cand-{i}", f"{kind} 待提示候选 {i}")
    if "unlinked" in extra:
        for _ in range(6):
            mk_note(old, None)

    store.close()
    print(json.dumps({"kind": kind, "acted": acted, "ignored": ignored, "fresh": fresh,
                      "today_shown": today, "candidates": cand, "extra": sorted(extra),
                      "tag": tag}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
