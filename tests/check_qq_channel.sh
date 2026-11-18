#!/usr/bin/env bash
# 验收：QQ 通道（plugins/channels/qq-onebot；契约 INTERFACE.md §11.7d）。
# 断言面：① 未装 ⇒ 退出 1 且提示 plugins.qq-onebot.enabled；② 成功 ⇒ delivered + message_id + Bearer；
#   ③ 群消息/名字解析；④ 后端拒绝 ⇒ 行留 pending + attempts/last_error；⑤ 重推；⑥ 撤回；
#   ⑦ dry-run 不发请求；⑧ 回复结算（含自己发的不结算、重复跑幂等）；⑨ 选项 "2" ⇒ 标签；⑩ 入参错退出 2。
# 隔离：临时 HUB_ROOT + 本机 stub OneBot（ssh_host 置空 ⇒ qq_bridge 在本机跑同一条命令），不碰真库。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-qqch-XXXXXX)
export PYTHONPATH="$HUB/src"
PASS=0
FAIL=0
STUB_PID=""

cleanup() {
  [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null
  /usr/bin/rm -rf "$TMP"
}
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s -- %s\n' "$1" "${2:-}"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want=$2 got=$3"; fi; }
check_has() { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "「$3」里没有「$2」" ;; esac; }

printf 'stub-token-0123456789' > "$TMP/token"

cat > "$TMP/stub.py" <<'PY'
import json, os, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

root = sys.argv[1]
log = os.path.join(root, "calls.jsonl")
token = open(os.path.join(root, "token"), encoding="utf-8").read().strip()
fail_flag = os.path.join(root, "fail")

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        action = self.path.strip("/")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") or "{}"
        auth = self.headers.get("Authorization")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"action": action, "auth": auth,
                                 "body": json.loads(raw)}, ensure_ascii=False) + "\n")
        if auth != "Bearer " + token:
            return self._send({"status": "failed", "retcode": 1401, "message": "unauthorized"})
        if action == "send_msg":
            if os.path.exists(fail_flag):
                return self._send({"status": "failed", "retcode": 100, "message": "后端拒绝"})
            return self._send({"status": "ok", "retcode": 0, "data": {"message_id": 424242}})
        if action == "delete_msg":
            return self._send({"status": "ok", "retcode": 0, "data": None})
        if action == "get_friend_list":
            return self._send({"status": "ok", "retcode": 0,
                               "data": [{"user_id": 12345, "nickname": "测试好友", "remark": "阿甲"}]})
        if action == "get_group_list":
            return self._send({"status": "ok", "retcode": 0,
                               "data": [{"group_id": 998877, "group_name": "测试群"}]})
        return self._send({"status": "failed", "retcode": 404, "message": "unknown action"})

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(os.path.join(root, "port"), "w", encoding="utf-8") as fh:
    fh.write(str(srv.server_address[1]))
srv.serve_forever()
PY

python3 "$TMP/stub.py" "$TMP" >/dev/null 2>&1 &
STUB_PID=$!
for _ in $(seq 1 50); do [ -s "$TMP/port" ] && break; sleep 0.1; done
PORT=$(cat "$TMP/port")
[ -n "$PORT" ] || { echo "stub 没起来"; exit 1; }

make_cfg() {  # make_cfg <out> <yes|no: 装通道>
  python3 - "$HUB/hub.config.yaml" "$1" "${2:-yes}" "$PORT" "$TMP" <<'PY'
import sys, yaml
src, out, on, port, tmp = sys.argv[1:6]
raw = yaml.safe_load(open(src, encoding="utf-8"))
plugins = raw.setdefault("plugins", {})
if on == "yes":
    plugins["qq-onebot"] = {"enabled": True}
else:
    plugins.pop("qq-onebot", None)
raw["qq_onebot"] = {"ssh_host": "", "api": "http://127.0.0.1:" + port,
                    "token_file": tmp + "/token", "spool_path": tmp + "/events.jsonl",
                    "timeout_s": 10}
yaml.safe_dump(raw, open(out, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
PY
}

qqsend() { OUT=$(HUB_ROOT="$TMP" HUB_CONFIG="$TMP/cfg.yaml" "$HUB/bin/hub-qq-send" "$@" 2>"$TMP/last.err"); RC=$?; }
qqreplies() { OUT=$(HUB_ROOT="$TMP" HUB_CONFIG="$TMP/cfg.yaml" "$HUB/bin/hub-qq-replies" "$@" 2>"$TMP/rep.err"); RC=$?; }
lastfield() { python3 -c 'import sqlite3,sys
con=sqlite3.connect(sys.argv[1])
row=con.execute("SELECT "+sys.argv[2]+" FROM deliveries ORDER BY rowid DESC LIMIT 1").fetchone()
print("" if row is None else row[0])' "$TMP/hub.db" "$1"; }
calls() { wc -l < "$TMP/calls.jsonl" 2>/dev/null || echo 0; }
lastcall() { tail -1 "$TMP/calls.jsonl"; }

# ---- T0 通道未装 -----------------------------------------------------------
make_cfg "$TMP/off.yaml" no
OUT=$(HUB_ROOT="$TMP" HUB_CONFIG="$TMP/off.yaml" "$HUB/bin/hub-qq-send" --to 12345 --title t 2>"$TMP/off.err"); RC=$?
check_eq "T0 通道未装 ⇒ 退出码 1" 1 "$RC"
check_has "T0 提示要 plugins.qq-onebot.enabled" "plugins.qq-onebot.enabled" "$(cat "$TMP/off.err")"

make_cfg "$TMP/cfg.yaml" yes
HUB_ROOT="$TMP" HUB_CONFIG="$TMP/cfg.yaml" "$HUB/bin/hub-deliver" list >/dev/null 2>&1

# ---- T1 入参错 -------------------------------------------------------------
qqsend --title "只有标题" --body ""
check_eq "T1 缺 --to ⇒ 退出码 2" 2 "$RC"
qqsend --to 12345
check_eq "T1 title 与 body 都空 ⇒ 退出码 2" 2 "$RC"

# ---- T2 私聊发送成功 -------------------------------------------------------
qqsend --to 12345 --title "自检标题" --body "自检正文" --json
check_eq "T2 发送成功 ⇒ 退出码 0" 0 "$RC"
check_has "T2 输出带 message_id" "424242" "$OUT"
check_eq "T2 库里 status=delivered" delivered "$(lastfield status)"
check_has "T2 remote_ref 带 message_id" "424242" "$(lastfield remote_ref)"
check_eq "T2 stub 收到 1 次调用" 1 "$(calls)"
check_has "T2 stub 收到 Bearer 鉴权头" 'Bearer stub-token-0123456789' "$(lastcall)"
check_has "T2 请求体是私聊 + uin" '"user_id": 12345' "$(lastcall)"

# ---- T3 群消息 + 名字解析 --------------------------------------------------
qqsend --to 998877 --group --title "群自检" --body "在群里发一条"
check_eq "T3 群消息发送 ⇒ 退出码 0" 0 "$RC"
check_has "T3 请求体 message_type=group" '"message_type": "group"' "$(lastcall)"
check_has "T3 请求体带 group_id" '"group_id": 998877' "$(lastcall)"
qqsend --to 阿甲 --title "按名字发" --body "解析成 uin 才能发"
check_eq "T3 按好友名发送 ⇒ 退出码 0" 0 "$RC"
check_has "T3 名字解析成 user_id" '"user_id": 12345' "$(lastcall)"

# ---- T4 后端拒绝 ⇒ 行留 pending -------------------------------------------
: > "$TMP/fail"
qqsend --to 12345 --title "会被拒" --body "后端拒绝"
check_eq "T4 后端拒绝 ⇒ 退出码 1" 1 "$RC"
check_eq "T4 行留 pending" pending "$(lastfield status)"
check_eq "T4 attempts=1" 1 "$(lastfield attempts)"
check_has "T4 last_error 带后端原文" "后端拒绝" "$(lastfield last_error)"
check_has "T4 stderr 说明行留 pending" "pending" "$(cat "$TMP/last.err")"

# ---- T5 重推 ----------------------------------------------------------------
/usr/bin/rm -f "$TMP/fail"
qqsend --pending --json
check_eq "T5 重推 ⇒ 退出码 0" 0 "$RC"
check_has "T5 重推结果 retried=1" '"retried": 1' "$OUT"
check_eq "T5 重推成功 ⇒ delivered" delivered "$(lastfield status)"

# ---- T6 撤回 ----------------------------------------------------------------
DID=$(lastfield id)
qqsend --dismiss "$DID"
check_eq "T6 撤回 ⇒ 退出码 0" 0 "$RC"
check_has "T6 stub 收到 delete_msg" '"action": "delete_msg"' "$(lastcall)"

# ---- T7 dry-run 不发请求 ----------------------------------------------------
BEFORE=$(calls)
qqsend --to 12345 --title "干跑" --body "不该发出去" --dry-run
check_eq "T7 dry-run ⇒ 退出码 0" 0 "$RC"
check_eq "T7 dry-run 不发请求" "$BEFORE" "$(calls)"

# ---- T8 回复结算 ------------------------------------------------------------
python3 - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
events = [
    {"time": 1795007000, "self_id": 10001, "post_type": "message", "message_type": "private",
     "message_id": 9001, "user_id": 12345, "message": [{"type": "text", "data": {"text": "我看见了"}}],
     "sender": {"user_id": 12345, "nickname": "测试好友"}},
    {"time": 1795007001, "self_id": 10001, "post_type": "message", "message_type": "private",
     "message_id": 9002, "user_id": 10001, "message": [{"type": "text", "data": {"text": "我自己发的"}}],
     "sender": {"user_id": 10001, "nickname": "我"}},
]
with open(tmp + "/events.jsonl", "w", encoding="utf-8") as fh:
    for ev in events:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
PY
qqsend --to 12345 --title "等你回一句" --body "随便说点什么" >/dev/null
TARGET=$(lastfield id)
qqreplies --json
check_eq "T8 回复结算 ⇒ 退出码 0" 0 "$RC"
check_has "T8 结算了那条投递" "$TARGET" "$OUT"
check_eq "T8 行 status=replied" replied "$(lastfield status)"
check_eq "T8 reply 原样留档" "我看见了" "$(lastfield reply)"
check_eq "T8 自己发的不结算（只 1 条）" 1 "$(printf '%s' "$OUT" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["replied"]))')"
qqreplies --json
check_eq "T8 再跑一次幂等（0 条新结算）" 0 "$(printf '%s' "$OUT" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["replied"]))')"

# ---- T9 选项匹配 -----------------------------------------------------------
qqsend --to 12345 --title "选一个" --body "1 能 / 2 不能" --options '[{"key":"a","label":"能"},{"key":"b","label":"不能"}]' >/dev/null
python3 - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
ev = {"time": 1795007100, "self_id": 10001, "post_type": "message", "message_type": "private",
      "message_id": 9003, "user_id": 12345, "message": [{"type": "text", "data": {"text": "2"}}],
      "sender": {"user_id": 12345, "nickname": "测试好友"}}
with open(tmp + "/events.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
PY
qqreplies --json
check_eq "T9 选项 2 ⇒ 记标签「不能」" "不能" "$(lastfield reply)"

printf '\n%d 通过 / %d 失败\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && echo "ALL PASS"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
