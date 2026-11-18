#!/usr/bin/env python3
"""验收用种子：往库里塞 N 条待办 + M 条 window 事件（界面切换）。

用法: m2_seed.py <todos> <window_events> [rule]     读 $HUB_CONFIG / $HUB_ROOT
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta

from infohub.config import load
from infohub.events import make_event
from infohub.store import Store

n_todos = int(sys.argv[1]) if len(sys.argv) > 1 else 1
n_win = int(sys.argv[2]) if len(sys.argv) > 2 else 1
rule = sys.argv[3] if len(sys.argv) > 3 else "fs-repeat"

cfg = load()
cfg.ensure_dirs()
store = Store(cfg.db_path)
store.migrate()
now = datetime.now().astimezone()
nonce = uuid.uuid4().hex[:6]   # 每次种子都要是新事件（去重键含 payload 指纹）

for i in range(n_win):
    store.insert_events([make_event(
        "window", "window.snapshot",
        {"frames": [{"app": "msedge", "title": f"页面 {nonce}-{i}"}], "count": 1},
        ts=(now - timedelta(seconds=10 - i)).isoformat(timespec="milliseconds"))])

for i in range(n_todos):
    key = f"{rule}:seed-{i}:{now.date().isoformat()}"
    store.upsert_todo({"dedupe_key": key, "title": f"合成待办 {i}", "why": "验收种子",
                       "confidence": 0.5, "source_event_ids": [1, 2, 3]})
print(f"seeded todos={n_todos} window_events={n_win}")
store.close()
