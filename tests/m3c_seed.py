#!/usr/bin/env python3
"""M3c 验收种子：造**真实事件**（不是假证据 id），让规则引擎自己产出带证据的待办。

- 3 次同一文件改动 ⇒ 规则 `fs-repeat`（3 条证据）
- 3 条同域剪贴板链接 ⇒ 规则 `clipboard-domain-repeat`（3 条证据）
- 1 条界面事件 ⇒ 供通知层的 `ui-event` 门使用
读 $HUB_CONFIG / $HUB_ROOT。
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta

from infohub.config import load
from infohub.events import make_event
from infohub.store import Store

cfg = load()
cfg.ensure_dirs()
store = Store(cfg.db_path)
store.migrate()
now = datetime.now().astimezone()
iso = lambda d: d.isoformat(timespec="seconds")  # noqa: E731
nonce = uuid.uuid4().hex[:6]

events = []
for i in range(3):
    events.append(make_event("fs", "fs.change",
                             {"root": "/tmp", "path": f"bridge/a-{nonce}.py",
                              "mtime": now.timestamp() - i * 60, "size": 10},
                             ts=iso(now - timedelta(hours=i + 1))))
for i in range(3):
    events.append(make_event("clipboard", "clip.copy",
                             {"head": f"https://m3c-{nonce}.example.com/page{i}", "chars": 30,
                              "truncated": False},
                             ts=iso(now - timedelta(minutes=i + 1)), privacy="sensitive"))
events.append(make_event("window", "window.snapshot",
                         {"frames": [{"app": "msedge", "title": f"M3c {nonce}"}], "count": 1},
                         ts=iso(now)))
inserted, dup = store.insert_events(events)
print(f"m3c_seed inserted={inserted} dup={dup} nonce={nonce}")
store.close()
sys.exit(0)
