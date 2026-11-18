#!/usr/bin/env python3
"""假 gdbus（M2b 验收用）：`monitor` 吐信号行、`call` 返回 `(uint32 N,)`、GetCapabilities 带 actions。

环境变量：
- `GDBUS_STUB_ID`         Notify 返回的通知 id（默认 77）
- `GDBUS_STUB_CALL_FAIL`  非空 ⇒ Notify 退出 1
- `GDBUS_STUB_CALL_LOG`   非空 ⇒ 把每回调用的 argv 追加写进去（断言按钮确实传了）
- `GDBUS_STUB_MONITOR`    非空 ⇒ 这个文件里每行是一个信号体，如 `ActionInvoked(uint32 79, 'acted')`
- `GDBUS_STUB_MONITOR_FAIL` 非空 ⇒ monitor 退出 1（模拟 gdbus 起不来）
- `GDBUS_STUB_HOLD`       monitor 吐完信号后 holding 多少秒（默认 0.5，模拟常驻）
"""
from __future__ import annotations

import os
import sys
import time

DBUS = "org.freedesktop.Notifications"


def main() -> int:
    args = sys.argv[1:]
    joined = " ".join(args)          # 方法名是以 `--method org.freedesktop.Notifications.X` 形式给的，只能按子串判
    log = os.environ.get("GDBUS_STUB_CALL_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(" ".join(args) + "\n")

    if "monitor" in args:
        if os.environ.get("GDBUS_STUB_MONITOR_FAIL"):
            print("Failed to monitor: boom", file=sys.stderr)
            return 1
        print(f"Monitoring signals on object /org/freedesktop/Notifications owned by {DBUS}",
              flush=True)
        path = os.environ.get("GDBUS_STUB_MONITOR")
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    print(f"The signal {DBUS}.{line} received", flush=True)
                    time.sleep(0.02)
        time.sleep(float(os.environ.get("GDBUS_STUB_HOLD", "0.5")))
        return 0

    if "GetCapabilities" in joined:
        print("(['actions', 'body', 'icon-static'],)")
        return 0

    if "Notifications.Notify" in joined:
        if os.environ.get("GDBUS_STUB_CALL_FAIL"):
            print("Error: boom", file=sys.stderr)
            return 1
        print(f"(uint32 {os.environ.get('GDBUS_STUB_ID', '77')},)")
        return 0

    if "GetIdletime" in joined:
        print("(uint64 0,)")
        return 0

    print("()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
