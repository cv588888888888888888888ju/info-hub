#!/usr/bin/env python3
"""M3 验收种子：塞 <events> 条 fs 事件 + <todos> 条待办 + <notes> 条通知台账。

用法: m3_seed.py <events> <todos> <notes>    读 $HUB_CONFIG / $HUB_ROOT
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta

from infohub.config import load
from infohub.events import make_event
from infohub.store import Store

n_events = int(sys.argv[1]) if len(sys.argv) > 1 else 2
n_todos = int(sys.argv[2]) if len(sys.argv) > 2 else 2
n_notes = int(sys.argv[3]) if len(sys.argv) > 3 else 1

cfg = load()
cfg.ensure_dirs()
store = Store(cfg.db_path)
store.migrate()
now = datetime.now().astimezone()
nonce = uuid.uuid4().hex[:6]

store.insert_events([make_event(
    "fs", "fs.change",
    {"path": f"/tmp/m3-{nonce}-{i}.txt", "event": "created"},
    ts=(now - timedelta(seconds=i)).isoformat(timespec="milliseconds")) for i in range(n_events)])

for i in range(n_todos):
    store.upsert_todo({"dedupe_key": f"fs-repeat:m3-{nonce}-{i}:{now.date().isoformat()}",
                       "title": f"合成待办 M3-{nonce}-{i}", "why": "M3 验收种子",
                       "confidence": 0.5, "source_event_ids": [1, 2, 3]})

for i in range(n_notes):
    store.add_notification("todo-daily", f"M3 种子通知 {nonce}-{i}", shown=True)

print(f"seeded events={n_events} todos={n_todos} notifications={n_notes}")
store.close()
