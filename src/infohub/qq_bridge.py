"""QQ / OneBot v11 共用件：远端调用、目标解析、事件 spool 增量读。

三处用得上（契约 INTERFACE.md §11.7d 与 §4.24）：
- plugins/channels/qq-onebot/channel.py（发送 / 撤回）；
- src/infohub/qq_send.py（hub-qq-send 命令：按 uin 或名字发私聊/群消息）；
- src/infohub/qq_replies.py（hub-qq-replies：把 QQ 回来的私聊当回复结算到投递台账）。

配置键统一在 qq_onebot.*（ssh_host / api / token_file / timeout_s / spool_path），
采集器 collect.qq.* 只多一个自己的游标，spool 路径两边默认同一个。
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess

#: 远端 POST 程序：python3 -c <本程序> <api> <token_file> <action> <json_payload>
_REMOTE_PY = """
import json, sys, urllib.request, urllib.error
api, tokfile, action, payload = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
try:
    token = open(tokfile, encoding="utf-8").read().strip()
except OSError as exc:
    print(json.dumps({"status": "failed", "message": "token read failed: %s" % exc}))
    raise SystemExit(0)
req = urllib.request.Request(api.rstrip("/") + "/" + action,
                             data=json.dumps(payload).encode("utf-8"),
                             headers={"Authorization": "Bearer " + token,
                                      "Content-Type": "application/json"},
                             method="POST")
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode("utf-8", "replace"))
except urllib.error.HTTPError as exc:
    print(json.dumps({"status": "failed", "message": "HTTP %s" % exc.code}))
except Exception as exc:
    print(json.dumps({"status": "failed", "message": str(exc)}))
"""

#: 远端 spool 增量读：python3 -c <本程序> <path> <offset> <max_bytes>
_READ_PY = """
import base64, json, os, sys
path, off, cap = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    size = os.path.getsize(path)
except OSError as exc:
    print(json.dumps({"error": str(exc)}))
    raise SystemExit(0)
if off > size:
    off = 0
with open(path, "rb") as fh:
    fh.seek(off)
    chunk = fh.read(cap)
print(json.dumps({"size": size, "offset": off,
                  "chunk": base64.b64encode(chunk).decode("ascii")}))
"""


class QQError(Exception):
    """QQ 后端不可达 / 拒绝 / 返回不合契约。"""


def host(config) -> str:
    return str(config.get("qq_onebot.ssh_host", "root@<bridge-host>") or "").strip()


def api(config) -> str:
    return str(config.get("qq_onebot.api", "http://127.0.0.1:3000") or "http://127.0.0.1:3000")


def token_file(config) -> str:
    return str(config.get("qq_onebot.token_file", "/opt/qq-relay/onebot.token")
               or "/opt/qq-relay/onebot.token")


def spool_path(config) -> str:
    return str(config.get("qq_onebot.spool_path", "/opt/qq-relay/queue/events.jsonl")
               or "/opt/qq-relay/queue/events.jsonl")


def timeout(config) -> float:
    return float(config.get("qq_onebot.timeout_s", 25))


def _run(config, line: str) -> str:
    h = host(config)
    cmd = (["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10", h, line] if h else ["sh", "-c", line])
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout(config))
    except subprocess.TimeoutExpired as exc:
        raise QQError(f"QQ 后端超时（>{timeout(config):g}s）") from exc
    except OSError as exc:
        raise QQError(f"执行远端命令失败: {exc}") from exc
    if proc.returncode != 0:
        tail = " ".join(ln.strip() for ln in proc.stderr.decode("utf-8", "replace").splitlines()
                        if ln.strip())[-200:]
        raise QQError(f"远端命令失败（rc={proc.returncode}）: {tail or '无 stderr'}")
    return proc.stdout.decode("utf-8", "replace")


def onebot_call(config, action: str, payload: dict) -> dict:
    """执行一次 OneBot action，返回响应对象（status=ok 由调用方判）。"""
    remote = "python3 -c " + shlex.quote(_REMOTE_PY) + " " + " ".join(shlex.quote(x) for x in (
        api(config), token_file(config), action,
        json.dumps(payload, ensure_ascii=False)))
    raw = _run(config, remote)
    try:
        out = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise QQError(f"后端返回非 JSON: {raw[:120]!r}") from exc
    if not isinstance(out, dict):
        raise QQError(f"后端返回的不是对象: {type(out).__name__}")
    return out


def call_ok(config, action: str, payload: dict) -> dict:
    out = onebot_call(config, action, payload)
    if str(out.get("status") or "") != "ok":
        raise QQError(str(out.get("message") or out.get("wording") or out)[:300])
    data = out.get("data")
    return data if isinstance(data, (dict, list)) else {}


def resolve_target(config, ident: str, group: bool = False) -> tuple[str, str, str]:
    """ident 是数字 id 或名字 ⇒ (type, id, 显示名)；名字按完全相等/包含匹配。"""
    ident = str(ident or "").strip()
    if not ident:
        raise QQError("目标不能为空（uin / group_id / 名字）")
    typ = "group" if group else "private"
    if ident.isdigit():
        return typ, ident, ""
    rows = call_ok(config, "get_group_list" if group else "get_friend_list", {})
    rows = rows if isinstance(rows, list) else []
    exact, fuzzy = None, None
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("group_name") if group else
                   (row.get("remark") or row.get("nickname") or ""))
        rid = str(row.get("group_id") if group else row.get("user_id") or "")
        if name == ident:
            exact = (rid, name)
        elif fuzzy is None and ident in name:
            fuzzy = (rid, name)
    hit = exact or fuzzy
    if not hit or not hit[0]:
        raise QQError(f"找不到{'群' if group else '好友'}: {ident}")
    return typ, hit[0], hit[1]


def read_spool(config, offset: int, max_bytes: int = 4 * 1024 * 1024) -> tuple[bytes, int]:
    """增量读事件 spool ⇒ (完整行之前的字节块, 新的绝对 offset)。半行留给下一轮。"""
    remote = "python3 -c " + shlex.quote(_READ_PY) + " " + " ".join(shlex.quote(x) for x in (
        spool_path(config), str(int(offset)), str(int(max_bytes))))
    raw = _run(config, remote)
    try:
        head = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise QQError(f"spool 读取返回非 JSON: {raw[:120]!r}") from exc
    if head.get("error"):
        raise QQError(f"spool 不可读: {head['error']}"[:200])
    base = int(head.get("offset") or 0)
    try:
        chunk = base64.b64decode(head.get("chunk") or "")
    except (ValueError, TypeError) as exc:
        raise QQError("spool 返回的 chunk 不是 base64") from exc
    complete = chunk[: chunk.rfind(b"\n") + 1] if b"\n" in chunk else b""
    return complete, base + len(complete)


def message_text(message) -> str:
    """OneBot 消息段数组 ⇒ 可读正文（占位约定见 INTERFACE.md §4.24）。"""
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    labels = {"image": "[图片]", "face": "[表情]", "mface": "[表情]", "record": "[语音]",
              "video": "[视频]", "file": "[文件]", "forward": "[聊天记录]", "json": "[卡片]",
              "xml": "[卡片]", "reply": "", "rps": "[猜拳]", "dice": "[骰子]", "poke": "[戳一戳]"}
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
            parts.append(labels.get(typ, "[%s]" % (typ or "未知")))
    return "".join(parts).strip()
