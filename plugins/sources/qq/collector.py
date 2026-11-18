"""QQ 消息采集器（路径 A）：读服务器 qq-relay 通知库里的 QQ 行，只读、不 scp 整库。

数据源 = <bridge-host> 的 `/opt/qq-relay/qq_ingest.db` 表 `n`（手机端中继服务写入，远端 `UNIQUE(key)`）；
本采集器经 ssh 让远端 python 读增量并打印 JSON 数组（默认 `python3 -c`，DB 只读打开）。

两条增量/去重轴（缺一不可）：
- 游标按自增 `id`：`state_get('qq')['cursor']['last_id']`，只取 `id > last_id`；
- 去重按通知 `key`：同一 key 的重放（远端约束缺失 / 游标回退）不再入库，即使正文被上游改写
  —— 但该保证以 `seen_keys` 里仍留有这个 key 为前提；`seen_keys` 只保留最近 `seen_keys_max`（缺省 500）个，
  且游标整行丢失后仅剩 events 指纹去重（正文被改写时会当成新事件，见 check_qq_source.sh 的去重边界探针）。

语义映射（字段名照 phone-hub，不另设 occurred_at/summary/body 三键）：
- 行 `ts`(ms) → 事件发生时刻 = 事件 `ts`（ISO8601 本地时区），payload 里保留原始 ms；
- `app` + `title` → 摘要（payload `app` 与 `title` 两键，同 phone-hub）；
- `text` → 正文 = payload `text`；`key` → payload `key`（去重键）。
事件形状与 phone-hub 逐字段一致（payload 六键 ts/pkg/app/title/text/key，kind=notify，privacy=normal）。
字段语义见 INTERFACE.md §4 采集器表 / §5.2 payload / §10 插件契约。

失败语义（INTERFACE.md §6）：远端不可达 / 表缺列 / 超时一律 raise CollectorError，由宿主记
`run:qq` 与 fail_streak、退出码仍 0 —— 不吞掉，也不让采集器退出非 0。

测试注入：`collect.qq.ssh_host` 置空 ⇒ 在本机跑同一条远端命令（配 `remote_db` 指向临时 sqlite
fixture），所以 tests/check_qq_source.sh 不需要真 ssh。`remote_cmd` 收到的参数固定为
`<db> <after_id> <limit>`。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess

from infohub import events as E
from infohub import redact
from infohub.sources import CollectorError

NODE = "qq"
PKG = "com.tencent.mobileqq"

# 远端读库程序（默认 remote_cmd）：只读打开、只取 QQ 且 id > after、按 id 升序，输出 JSON 数组。
# 调用形状：python3 -c <本程序> <db> <after_id> <limit>（collect 把后三个参数拼在后面）。
_DUMP_PY = """
import sqlite3, json, sys
db, after, limit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
cols = ["id", "ts", "pkg", "app", "title", "text", "subtext", "key", "received_at"]
sql = ("SELECT id, ts, pkg, app, title, text, subtext, key, received_at FROM n"
       " WHERE pkg = ? AND id > ? ORDER BY id LIMIT ?")
rows = [dict(zip(cols, r)) for r in con.execute(sql, ("com.tencent.mobileqq", after, limit))]
print(json.dumps(rows, ensure_ascii=False))
"""

REMOTE_DUMP = "python3 -c " + shlex.quote(_DUMP_PY)


def _cursor(store) -> dict:
    return dict(((store.state_get(NODE) or {}).get("cursor")) or {})


def _fail(store, message: str) -> CollectorError:
    """记一次失败（fail_streak+1）并返回待抛的异常；游标其余字段原样保留。"""
    cur = _cursor(store)
    cur["fail_streak"] = int(cur.get("fail_streak") or 0) + 1
    store.state_put(NODE, cursor=cur, ok=False, error=message)
    return CollectorError(message)


def _ok(store, last_id: int, seen: list[str]) -> None:
    store.state_put(NODE, cursor={"last_id": last_id, "seen_keys": seen, "fail_streak": 0},
                    ok=True, error=None)


def collect(config, store) -> list[dict]:
    host = str(config.collector_get(NODE, "ssh_host", "root@<bridge-host>") or "").strip()
    db = str(config.collector_get(NODE, "remote_db", "/opt/qq-relay/qq_ingest.db"))
    remote_cmd = str(config.collector_get(NODE, "remote_cmd", REMOTE_DUMP) or REMOTE_DUMP)
    limit = max(1, int(config.collector_get(NODE, "limit", 200)))
    timeout = float(config.collector_get(NODE, "timeout_s", 20))
    seen_max = max(1, int(config.collector_get(NODE, "seen_keys_max", 500)))

    cur = _cursor(store)
    last_id = int(cur.get("last_id") or 0)
    seen = [str(k) for k in (cur.get("seen_keys") or [])]
    known = set(seen)

    line = f"{remote_cmd} {shlex.quote(db)} {last_id} {limit}"
    if host:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10", host, line]
    else:
        cmd = ["sh", "-c", line]          # 测试注入：本机跑同一条命令（见模块 docstring）

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=os.environ.copy())
    except subprocess.TimeoutExpired as exc:
        raise _fail(store, f"读取 QQ 中继库超时（>{timeout:g}s）") from exc
    except OSError as exc:
        raise _fail(store, f"执行远端读取命令失败: {exc}") from exc

    if proc.returncode != 0:
        raw = proc.stderr.decode("utf-8", "replace").strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        tail = " ".join(lines[-3:]) if lines else ""        # 末几行才带真异常（traceback 头无信息）
        err = tail[-200:]
        raise _fail(store, f"读取 QQ 中继库失败（rc={proc.returncode}）: {err or '无 stderr'}")

    try:
        rows = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
    except json.JSONDecodeError as exc:
        raise _fail(store, f"远端返回非 JSON: {proc.stdout[:120]!r}") from exc
    if not isinstance(rows, list):
        raise _fail(store, f"远端返回的不是数组: {type(rows).__name__}")

    out: list[dict] = []
    max_id = last_id
    for row in rows:
        if not isinstance(row, dict):
            raise _fail(store, f"远端返回的行不是对象: {row!r}"[:200])
        try:
            rid, ts = int(row["id"]), int(row["ts"])
        except (KeyError, TypeError, ValueError) as exc:      # 表缺列 / 契约漂移
            raise _fail(store, f"远端行缺列（id/ts）: {row!r}"[:200]) from exc
        max_id = max(max_id, rid)
        if str(row.get("pkg") or "") != PKG:                  # 远端已过滤；自定义 remote_cmd 也守契约
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            raise _fail(store, f"远端行缺去重键 key（id={rid}）")
        if key in known:                                      # 按 key 去重，不只靠指纹
            continue
        known.add(key)
        seen.append(key)
        payload = {
            "ts": ts,
            "pkg": str(row.get("pkg") or PKG),
            "app": str(row.get("app") or ""),
            "title": str(row.get("title") or ""),
            "text": str(row.get("text") or ""),
            "key": key,
        }
        payload, _summary = redact.apply(payload, config)      # 脱敏先于造事件（INTERFACE.md §4.21）
        out.append(E.make_event(NODE, "notify", payload, ts=E.iso(ts)))

    _ok(store, max_id, seen[-seen_max:])
    return out
