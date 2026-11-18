#!/usr/bin/env python3
"""验收用假通知器：把收到的通知追加进日志，退出码由 NOTIFY_STUB_EXIT 控制。

被 tests/verify_m2.sh 通过 `notify.command` 调用；这样验收不弹真窗口。
"""

from __future__ import annotations

import json
import os
import sys

log_path, summary, body, kind = (list(sys.argv[1:]) + ["", "", "", ""])[:4]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"summary": summary, "body": body, "kind": kind}, ensure_ascii=False) + "\n")
print(f"(uint32 {os.getpid()},)")
sys.exit(int(os.environ.get("NOTIFY_STUB_EXIT", "0") or 0))
