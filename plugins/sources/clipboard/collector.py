"""剪贴板采集器：读当前 Wayland 剪贴板文本（只读）。语义见 INTERFACE.md §4、§5.2。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time

from infohub import events as E
from infohub.sources import CollectorError

NODE = "clipboard"


def _cursor(store) -> dict:
    return dict(((store.state_get(NODE) or {}).get("cursor")) or {})


def _write(store, **fields) -> dict:
    cur = _cursor(store)
    cur.update(fields)
    store.state_put(NODE, cursor=cur, ok=fields.get("fail_streak", 0) == 0,
                    error=fields.get("last_error"))
    return cur


def _fail(store, message: str, backoff_after: int, backoff_s: float) -> CollectorError:
    """记一次失败；连续失败到阈值就进冷却，之后一段时间直接跳过（不再每次白等 timeout_s）。"""
    streak = int(_cursor(store).get("fail_streak") or 0) + 1
    until = 0.0
    note = message
    if streak >= backoff_after:
        until = time.time() + backoff_s
        note = f"{message}；连续失败 {streak} 次，进入冷却 {backoff_s:.0f}s"
    _write(store, fail_streak=streak, backoff_until_ts=until, last_error=note)
    return CollectorError(note)


def collect(config, store) -> list[dict]:
    cmd = str(config.collector_get(NODE, "command", "wl-paste"))
    args = [str(a) for a in (config.collector_get(NODE, "args", ["-n"]) or [])]
    timeout = float(config.collector_get(NODE, "timeout_s", 5))
    max_chars = int(config.collector_get(NODE, "max_chars", 2000))
    backoff_after = max(1, int(config.collector_get(NODE, "backoff_after", 3)))
    backoff_s = float(config.collector_get(NODE, "backoff_s", 900))

    cur = _cursor(store)
    until = float(cur.get("backoff_until_ts") or 0)
    if until > time.time():
        # 冷却中：不跑、不算失败、不报错（`hub-store stats` 的 last_error 里看得到）
        return []

    if shutil.which(cmd) is None:
        raise _fail(store, f"命令不存在: {cmd}（需要 wl-clipboard）", backoff_after, backoff_s)

    try:
        proc = subprocess.run([cmd, *args], capture_output=True, timeout=timeout,
                              env=os.environ.copy())
    except subprocess.TimeoutExpired as exc:
        raise _fail(store, f"{cmd} 读取超时（>{timeout}s）", backoff_after, backoff_s) from exc
    except OSError as exc:
        raise _fail(store, f"执行 {cmd} 失败: {exc}", backoff_after, backoff_s) from exc

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        # 剪贴板里没有文本（例如只装了图片）不算故障
        if any(word in err.lower() for word in ("type", "target", "empty")):
            _write(store, fail_streak=0, backoff_until_ts=0.0, last_error=None)
            return []
        raise _fail(store, f"{cmd} 退出 {proc.returncode}: {err[:200]}", backoff_after, backoff_s)

    text = proc.stdout.decode("utf-8", "replace").rstrip("\n")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text.strip() else None
    if digest is None or cur.get("hash") == digest:
        _write(store, fail_streak=0, backoff_until_ts=0.0, last_error=None)
        return []

    _write(store, hash=digest, fail_streak=0, backoff_until_ts=0.0, last_error=None)
    payload = {"head": text[:max_chars], "chars": len(text), "truncated": len(text) > max_chars}
    return [E.make_event(NODE, "clip.copy", payload, ts=E.iso(), privacy="sensitive")]
