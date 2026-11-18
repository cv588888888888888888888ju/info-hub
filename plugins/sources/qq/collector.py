"""QQ 消息采集器（OneBot v11 事件 spool）：读 <bridge-host> 上 onebot-tap 写出的 JSONL，只读。

链路（2026-11-18 起）：QQ 客户端（SnowLuma 注入）→ WebUI/OneBot v11 ws://127.0.0.1:3001
→ onebot-tap.service 落 /opt/qq-relay/queue/events.jsonl（每条一行 OneBot 事件）
→ 本采集器经 ssh 增量读该文件。

增量轴 = 字节游标：state_get('qq')['cursor']['offset']；只读未完整行之前的部分，写完才前进
（半行留在下一次）。文件被轮转（size < offset）就从 0 重读，并用 seen_keys 去重。
去重键 = 事件 message_id（key 形如 "qq:<message_id>"），seen_keys 只留最近 seen_keys_max 个。

语义映射（字段名照 phone-hub，payload 六键 ts/pkg/app/title/text/key，kind=notify）：
- 事件 time（秒）→ payload ts（毫秒，与通知库同一口径）；
- title = 群名（群消息）或发送者昵称（私聊）；text = 消息正文（纯文本段拼接，
  非文本段用 [图片]/[表情]/[语音]/[视频]/[文件]/[复合] 占位）；app = "qq"；pkg = "qq.onebot"。
- 自己发的消息默认跳过（include_self: true 才收）。

失败语义（INTERFACE.md §6）：spool 不可达 / 超时 / 行不是 JSON 对象 ⇒ raise CollectorError，
宿主记 run:qq 与 fail_streak，退出码仍 0。

测试注入：collect.qq.ssh_host 置空 ⇒ 在本机跑同一条命令（配 spool_path 指向临时 fixture）。
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess

from infohub import events as E
from infohub import redact
from infohub.sources import CollectorError

NODE = "qq"
PKG = "qq.onebot"
APP = "qq"

# 远端读 spool：给 offset 与最大读取字节，返回 base64 的字节块 + 当前文件大小。
# 调用形状：python3 -c <本程序> <spool> <offset> <max_bytes>
_READ_PY = """
import base64, json, os, sys
path, off, cap = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    size = os.path.getsize(path)
except OSError as exc:
    print(json.dumps({"error": str(exc)}))
    raise SystemExit(0)
rotated = off > size
if rotated:
    off = 0
with open(path, "rb") as fh:
    fh.seek(off)
    chunk = fh.read(cap)
print(json.dumps({"size": size, "offset": off, "rotated": rotated,
                  "chunk": base64.b64encode(chunk).decode("ascii")}))
"""

REMOTE_READ = "python3 -c " + shlex.quote(_READ_PY)

SEG_LABELS = {
    "image": "[图片]", "face": "[表情]", "mface": "[表情]", "record": "[语音]",
    "video": "[视频]", "file": "[文件]", "forward": "[聊天记录]", "json": "[卡片]",
    "xml": "[卡片]", "markdown": "[卡片]", "mirai": "[卡片]", "reply": "",
    "at": "", "rps": "[猜拳]", "dice": "[骰子]", "poke": "[戳一戳]",
}


def _cursor(store) -> dict:
    return dict(((store.state_get(NODE) or {}).get("cursor")) or {})


def _fail(store, message: str) -> CollectorError:
    cur = _cursor(store)
    cur["fail_streak"] = int(cur.get("fail_streak") or 0) + 1
    store.state_put(NODE, cursor=cur, ok=False, error=message)
    return CollectorError(message)


def _ok(store, offset: int, seen: list[str]) -> None:
    store.state_put(NODE, cursor={"offset": offset, "seen_keys": seen, "fail_streak": 0},
                    ok=True, error=None)


def message_text(message) -> str:
    """OneBot 消息段数组 → 可读正文（与 INTERFACE.md §4.24 的占位约定一致）。"""
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        typ = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if typ == "text":
            parts.append(str(data.get("text") or ""))
        elif typ == "at":
            qq = str(data.get("qq") or "")
            parts.append("" if qq in ("all", "") else "@" + qq)
        else:
            parts.append(SEG_LABELS.get(typ, "[%s]" % (typ or "未知")))
    return "".join(parts).strip()


def _peer(event: dict) -> tuple[str, str]:
    """返回 (title, poll_key)：群消息取群名，私聊取发送者昵称。"""
    if event.get("message_type") == "group":
        title = str(event.get("group_name") or event.get("group_id") or "").strip()
        return title, "group:%s" % (event.get("group_id") or "")
    sender = event.get("sender") or {}
    title = str(sender.get("card") or sender.get("nickname") or event.get("user_id") or "").strip()
    return title, "private:%s" % (event.get("user_id") or "")


def collect(config, store) -> list[dict]:
    host = str(config.collector_get(NODE, "ssh_host", "root@<bridge-host>") or "").strip()
    spool = str(config.collector_get(NODE, "spool_path", "/opt/qq-relay/queue/events.jsonl"))
    remote_cmd = str(config.collector_get(NODE, "remote_cmd", REMOTE_READ) or REMOTE_READ)
    limit = max(1, int(config.collector_get(NODE, "limit", 200)))
    max_bytes = max(4096, int(config.collector_get(NODE, "max_bytes", 4 * 1024 * 1024)))
    timeout = float(config.collector_get(NODE, "timeout_s", 20))
    seen_max = max(1, int(config.collector_get(NODE, "seen_keys_max", 500)))
    include_self = bool(config.collector_get(NODE, "include_self", False))

    cur = _cursor(store)
    offset = int(cur.get("offset") or 0)
    seen = [str(k) for k in (cur.get("seen_keys") or [])]
    known = set(seen)

    line = f"{remote_cmd} {shlex.quote(spool)} {offset} {max_bytes}"
    if host:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10", host, line]
    else:
        cmd = ["sh", "-c", line]          # 测试注入：本机跑同一条命令（见模块 docstring）

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=os.environ.copy())
    except subprocess.TimeoutExpired as exc:
        raise _fail(store, f"读取 QQ 事件 spool 超时（>{timeout:g}s）") from exc
    except OSError as exc:
        raise _fail(store, f"执行远端读取命令失败: {exc}") from exc

    if proc.returncode != 0:
        raw = proc.stderr.decode("utf-8", "replace").strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        tail = " ".join(lines[-3:]) if lines else ""
        err = tail[-200:]
        raise _fail(store, f"读取 QQ 事件 spool 失败（rc={proc.returncode}）: {err or '无 stderr'}")

    try:
        head = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise _fail(store, f"远端返回非 JSON: {proc.stdout[:120]!r}") from exc
    if not isinstance(head, dict):
        raise _fail(store, f"远端返回的不是对象: {type(head).__name__}")
    if head.get("error"):
        raise _fail(store, f"spool 不可读: {head['error']}"[:200])

    base = int(head.get("offset") or 0)
    try:
        chunk = base64.b64decode(head.get("chunk") or "")
    except (ValueError, TypeError) as exc:
        raise _fail(store, "远端返回的 chunk 不是 base64") from exc

    complete = chunk[: chunk.rfind(b"\n") + 1] if b"\n" in chunk else b""   # 只吃完整行

    # 游标按「已消费到的行尾」前进：撞到 limit 提前 break 时，块里没处理的行必须留到下一轮，
    # 否则断网积压一次就把溢出的消息永久跳过了（2026-11-18 修）。
    consumed = 0
    out: list[dict] = []
    self_id = ""
    for raw_line in complete.splitlines(keepends=True):
        stripped = raw_line.strip()
        if not stripped:
            consumed += len(raw_line)
            continue
        try:
            event = json.loads(stripped.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise _fail(store, f"spool 行不是 JSON: {stripped[:120]!r}")
        if not isinstance(event, dict):
            raise _fail(store, f"spool 行不是对象: {stripped[:120]!r}")
        if event.get("post_type") != "message":
            consumed += len(raw_line)
            continue
        self_id = str(event.get("self_id") or self_id)
        user = str(event.get("user_id") or "")
        if not include_self and self_id and user == self_id:
            consumed += len(raw_line)
            continue
        try:
            mid = int(event["message_id"])
            ts = int(event["time"]) * 1000
        except (KeyError, TypeError, ValueError) as exc:      # 契约漂移
            raise _fail(store, f"spool 事件缺字段（message_id/time）: {stripped[:160]!r}") from exc
        key = f"qq:{mid}"
        if key in known:
            consumed += len(raw_line)
            continue
        known.add(key)
        seen.append(key)
        title, _peer_key = _peer(event)
        payload = {
            "ts": ts,
            "pkg": PKG,
            "app": APP,
            "title": title,
            "text": message_text(event.get("message")),
            "key": key,
        }
        payload, _summary = redact.apply(payload, config)     # 脱敏先于造事件（INTERFACE.md §4.21）
        out.append(E.make_event(NODE, "notify", payload, ts=E.iso(ts)))
        consumed += len(raw_line)
        if len(out) >= limit:
            break

    _ok(store, base + consumed, seen[-seen_max:])
    return out
