"""手机通知采集器（服务器为基准）：从 ECS 的 hub.db 取手机上报的通知。

手机 → 9444 加密通道 → ECS `hub.db`（服务器侧中枢）→ 本采集器 → info-hub 事件库。
语义见 INTERFACE.md §4；失败按采集器约定降级，不阻塞其它采集器。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

from infohub import events as E
from infohub import redact
from infohub.sources import CollectorError

NODE = "phone-hub"


def _cursor(store) -> dict:
    return dict(((store.state_get(NODE) or {}).get("cursor")) or {})


def _write(store, **fields) -> dict:
    cur = _cursor(store)
    cur.update(fields)
    store.state_put(NODE, cursor=cur, ok=fields.get("fail_streak", 0) == 0,
                    error=fields.get("last_error"))
    return cur


def collect(config, store) -> list[dict]:
    host = str(config.collector_get(NODE, "ssh_host", "root@<wg-server-ip>"))
    key = str(config.collector_get(NODE, "ssh_key", os.path.expanduser("~/.ssh/id_ed25519")))
    proxy = str(config.collector_get(NODE, "ssh_proxy",
                                     "ncat --proxy 127.0.0.1:11080 --proxy-type socks5 %h %p"))
    remote = str(config.collector_get(NODE, "remote_cmd",
                                      "cd /opt/mcp-bridge/ai_mcp_server && "
                                      ".venv/bin/python hub_dump.py"))
    limit = int(config.collector_get(NODE, "limit", 200))
    timeout = float(config.collector_get(NODE, "timeout_s", 30))
    head_chars = int(config.collector_get(NODE, "head_chars", 160))
    backoff_after = max(1, int(config.collector_get(NODE, "backoff_after", 3)))
    backoff_s = float(config.collector_get(NODE, "backoff_s", 600))

    cur = _cursor(store)
    until = float(cur.get("backoff_until_ts") or 0)
    if until > time.time():
        return []

    since_ms = int(cur.get("last_ts") or 0)
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=10"]
    if proxy:
        cmd += ["-o", f"ProxyCommand={proxy}"]
    cmd += ["-i", key, host, f"{remote} {since_ms} {limit}"]

    def fail(message: str) -> CollectorError:
        streak = int(_cursor(store).get("fail_streak") or 0) + 1
        until2 = 0.0
        note = message
        if streak >= backoff_after:
            until2 = time.time() + backoff_s
            note = f"{message}；连续失败 {streak} 次，进入冷却 {backoff_s:.0f}s"
        _write(store, fail_streak=streak, backoff_until_ts=until2, last_error=note)
        return CollectorError(note)

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=os.environ.copy())
    except subprocess.TimeoutExpired as exc:
        raise fail(f"拉取服务器中枢超时（>{timeout}s）") from exc
    except OSError as exc:
        raise fail(f"执行 ssh 失败: {exc}") from exc

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().replace("\n", " ")[:200]
        raise fail(f"ssh 退出 {proc.returncode}: {err}")

    try:
        rows = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
    except json.JSONDecodeError as exc:
        raise fail(f"服务器返回非 JSON: {proc.stdout[:120]!r}") from exc

    out: list[dict] = []
    max_ts = since_ms
    for row in rows:
        ts = int(row.get("ts") or 0)
        if ts <= since_ms:
            continue
        max_ts = max(max_ts, ts)
        title = (row.get("title") or "").strip()
        text = (row.get("text") or "").strip()
        payload = {
            "ts": ts,
            "pkg": row.get("pkg"),
            "app": row.get("app"),
            "title": title[:head_chars],
            "text": text[:head_chars],
            "key": row.get("key"),
        }
        # 验证码脱敏在造事件之前做 ⇒ 新行的 fingerprint 是脱敏后算的（INTERFACE.md §4.21）。
        payload, _redact_summary = redact.apply(payload, config)
        out.append(E.make_event(NODE, "notify", payload, ts=E.iso(ts)))

    _write(store, last_ts=max_ts, fail_streak=0, backoff_until_ts=0.0, last_error=None)
    return out
