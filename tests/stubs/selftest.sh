#!/usr/bin/env bash
# 替身自测（W3）：bridge_stub.py 与 gdbus_stub.sh 的机械验收。
# 只依赖 bash + python3 + curl（不 import infohub、不连真手机、不弹真通知）。
# 退出 0 = 全部断言通过；任何一条失败退出 1。
#
# 用法：bash info-hub/tests/stubs/selftest.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/hub-stubs-XXXXXX")"
BRIDGE_PIDS=()

cleanup() {
  local pid
  for pid in "${BRIDGE_PIDS[@]}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  for pid in "${BRIDGE_PIDS[@]}"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null
  done
  /usr/bin/rm -rf "$TMP"
}
trap cleanup EXIT

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'PASS %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL %s  [%s]\n' "$1" "${2:-}"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "期望 [$3]，实际 [$2]"; fi; }
check_has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "输出里没有 [$3]" ;; esac; }
check_lacks() { case "$2" in *"$3"*) bad "$1" "不该出现 [$3]" ;; *) ok "$1" ;; esac; }

# assert_py <desc>：断言代码从 stdin 读，环境变量由调用行前缀传入
assert_py() {
  local desc="$1"
  if python3 -; then ok "$desc"; else bad "$desc" "python 断言失败（见上方 traceback）"; fi
}

# ---- 起服务 ---------------------------------------------------------------
# start_bridge <前缀> <jsonl 路径> [k=v ...]；成功时 START_URL / START_PID 有值
START_URL=""
START_PID=""
start_bridge() {
  local prefix="$1" log="$2" i url=""
  shift 2
  local out="$TMP/$prefix.out"
  env BRIDGE_STUB_PORT=0 BRIDGE_STUB_LOG="$log" "$@" python3 "$HERE/bridge_stub.py" \
    >"$out" 2>"$TMP/$prefix.err" &
  local pid=$!
  BRIDGE_PIDS+=("$pid")
  for i in $(seq 1 100); do
    url="$(sed -n 's/^BRIDGE_STUB_LISTENING //p' "$out" | head -1)"
    [ -n "$url" ] && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  START_URL="$url"
  START_PID="$pid"
  [ -n "$url" ]
}

# mcp_post <url> <json> <outfile> [session] -> 打印 HTTP 状态码
mcp_post() {
  local url="$1" body="$2" out="$3" sid="${4:-}"
  local -a args=(-sS -o "$out" -w '%{http_code}'
    -H 'Content-Type: application/json'
    -H 'Accept: application/json, text/event-stream'
    -d "$body")
  [ -n "$sid" ] && args+=(-H "mcp-session-id: $sid")
  curl "${args[@]}" "$url"
}

# =========================================================================
# 1. bridge_stub：initialize / notifications-initialized / tools/list / tools/call
# =========================================================================
BRIDGE_LOG="$TMP/calls.jsonl"
REPLAY="$TMP/replay.json"
cat >"$REPLAY" <<'JSON'
{"hub_deliver": {"ok": true, "delivery_id": "d-replay", "stub": "replay", "shown": true}}
JSON

if start_bridge bridge "$BRIDGE_LOG"; then
  ok "bridge 启动（127.0.0.1 临时端口，URL=$START_URL）"
else
  bad "bridge 启动" "$(cat "${TMP}/bridge.err" 2>/dev/null)"
  printf '\nPASS=%d FAIL=%d\n' "$PASS" "$FAIL"
  exit 1
fi

INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"selftest","version":"1"}}}'
CODE="$(curl -sS -o "$TMP/init.json" -D "$TMP/init.hdr" -w '%{http_code}' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d "$INIT_BODY" "$START_URL")"
check_eq "initialize 回 200" "$CODE" "200"
if grep -qi '^mcp-session-id:' "$TMP/init.hdr"; then ok "initialize 回了 Mcp-Session-Id 头"; else bad "initialize 回了 Mcp-Session-Id 头"; fi
FILE="$TMP/init.json" assert_py "initialize 结果含 serverInfo.name=bridge-stub" <<'PY'
import json, os
data = json.load(open(os.environ["FILE"], encoding="utf-8"))
assert data["result"]["serverInfo"]["name"] == "bridge-stub", data
assert "tools" in data["result"]["capabilities"], data
PY
SID="$(sed -n 's/^[Mm]cp-[Ss]ession-[Ii]d: *//p' "$TMP/init.hdr" | tr -d '\r' | head -1)"

CODE="$(mcp_post "$START_URL" '{"jsonrpc":"2.0","method":"notifications/initialized"}' "$TMP/initnotif.out" "$SID")"
check_eq "notifications/initialized 回 202" "$CODE" "202"

CODE="$(mcp_post "$START_URL" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' "$TMP/tools.json" "$SID")"
check_eq "tools/list 回 200" "$CODE" "200"
FILE="$TMP/tools.json" assert_py "tools/list 含 hub_deliver / hub_dismiss / device_action" <<'PY'
import json, os
data = json.load(open(os.environ["FILE"], encoding="utf-8"))
names = {tool["name"] for tool in data["result"]["tools"]}
assert {"hub_deliver", "hub_dismiss", "device_action"} <= names, names
PY

CALL_BODY='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"hub_deliver","arguments":{"delivery_id":"d-1","mode":"single","title":"标题","body":"正文","options":[{"key":"ack","label":"知道了"}],"layout":{"v":1,"title":"标题"},"timeout_s":600}}}'
CODE="$(mcp_post "$START_URL" "$CALL_BODY" "$TMP/call.json" "$SID")"
check_eq "tools/call 回 200" "$CODE" "200"
FILE="$TMP/call.json" assert_py "tools/call 回执是 content[0].text 里的 JSON 且 ok=true" <<'PY'
import json, os
data = json.load(open(os.environ["FILE"], encoding="utf-8"))
receipt = json.loads(data["result"]["content"][0]["text"])
assert receipt["ok"] is True, receipt
assert receipt["delivery_id"] == "d-1", receipt
PY

check_eq "JSONL 恰好一行（initialize/tools/list 不落盘）" "$(grep -c '[^[:space:]]' "$BRIDGE_LOG")" "1"
LOG="$BRIDGE_LOG" assert_py "JSONL 那一行含本次 tools/call 的参数（delivery_id/mode/options/layout）" <<'PY'
import json, os
rows = [json.loads(line) for line in open(os.environ["LOG"], encoding="utf-8") if line.strip()]
assert len(rows) == 1, rows
row = rows[0]
assert row["method"] == "tools/call", row
assert row["name"] == "hub_deliver", row
args = row["params"]["arguments"]
assert args["delivery_id"] == "d-1", args
assert args["mode"] == "single", args
assert args["options"][0]["key"] == "ack", args
assert args["layout"] == {"v": 1, "title": "标题"}, args
PY

# 回放文件：同一实例上预设回执优先于内置默认回执
if start_bridge replay "$TMP/replay.jsonl" BRIDGE_STUB_REPLAY="$REPLAY"; then
  CODE="$(mcp_post "$START_URL" "$CALL_BODY" "$TMP/call2.json")"
  check_eq "回放实例 tools/call 回 200" "$CODE" "200"
  FILE="$TMP/call2.json" assert_py "回放文件生效（返回预设回执 d-replay）" <<'PY'
import json, os
data = json.load(open(os.environ["FILE"], encoding="utf-8"))
receipt = json.loads(data["result"]["content"][0]["text"])
assert receipt == {"ok": True, "delivery_id": "d-replay", "stub": "replay", "shown": True}, receipt
PY
  check_eq "回放实例 JSONL 恰好一行" "$(grep -c '[^[:space:]]' "$TMP/replay.jsonl")" "1"
else
  bad "回放实例 bridge 启动"
fi

# SSE 回包（phone_remind._parse 支持 data: 行）
if start_bridge sse "$TMP/sse.jsonl" BRIDGE_STUB_SSE=1; then
  CODE="$(mcp_post "$START_URL" '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hub_dismiss","arguments":{"delivery_id":"d-1"}}}' "$TMP/sse.out")"
  check_eq "SSE 模式 tools/call 回 200" "$CODE" "200"
  check_has "SSE 模式回包是 data: 行" "$(cat "$TMP/sse.out")" "data: "
else
  bad "SSE 模式 bridge 启动"
fi

# 强制失败模式（给「bridge 不可达/失败」用例用）
if start_bridge fail "$TMP/fail.jsonl" BRIDGE_STUB_FAIL=http500; then
  CODE="$(mcp_post "$START_URL" "$INIT_BODY" "$TMP/fail.out")"
  check_eq "BRIDGE_STUB_FAIL=http500 时回 500" "$CODE" "500"
else
  bad "失败模式 bridge 启动"
fi

# =========================================================================
# 2. gdbus_stub：call（Notify / GetCapabilities）与 monitor（动作信号）
# =========================================================================
GDBUS="$HERE/gdbus_stub.sh"
NOTIFY_ARGS=(call --session --dest org.freedesktop.Notifications
  --object-path /org/freedesktop/Notifications
  --method org.freedesktop.Notifications.Notify
  info-hub 0 "" stub-summary stub-body '["打开", "稍后"]' '{}' 8000)

CALL_OUT="$(GDBUS_STUB_ID=70 GDBUS_STUB_CALL_LOG="$TMP/gdbus.log" bash "$GDBUS" "${NOTIFY_ARGS[@]}")"
RC=$?
check_eq "gdbus call Notify 退出码" "$RC" "0"
check_eq "gdbus call Notify 输出形状 (uint32 70,)" "$CALL_OUT" "(uint32 70,)"
CALL="$CALL_OUT" assert_py "notify.py 的 DBUS_ID_RE 能从输出取到 70" <<'PY'
import os, re
DBUS_ID_RE = re.compile(r"\(uint32\s+(\d+)\s*,?\)")
m = DBUS_ID_RE.search(os.environ["CALL"])
assert m and int(m.group(1)) == 70, os.environ["CALL"]
PY
check_has "Notify 的入参被记进 GDBUS_STUB_CALL_LOG" "$(cat "$TMP/gdbus.log")" "stub-summary"

CAPS="$(GDBUS_STUB_CAPABILITIES=actions,body bash "$GDBUS" call --session \
  --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications \
  --method org.freedesktop.Notifications.GetCapabilities)"
check_eq "GetCapabilities 输出形状" "$CAPS" "(['actions', 'body'],)"
CAPS="$(GDBUS_STUB_CAPABILITIES=none bash "$GDBUS" call --session \
  --dest org.freedesktop.Notifications --method org.freedesktop.Notifications.GetCapabilities)"
check_eq "GDBUS_STUB_CAPABILITIES=none 回空能力表" "$CAPS" "()"

if GDBUS_STUB_CALL_FAIL=1 bash "$GDBUS" "${NOTIFY_ARGS[@]}" >/dev/null 2>&1; then
  bad "GDBUS_STUB_CALL_FAIL=1 时 Notify 应退出非 0"
else
  ok "GDBUS_STUB_CALL_FAIL=1 时 Notify 退出非 0"
fi

MON_OUT="$(GDBUS_STUB_ACTIONS='acted snooze' GDBUS_STUB_MONITOR_ID=70 GDBUS_STUB_HOLD=0 \
  bash "$GDBUS" monitor --session --dest org.freedesktop.Notifications)"
check_has "monitor 输出 ActionInvoked(acted)" "$MON_OUT" \
  "The signal org.freedesktop.Notifications.ActionInvoked(uint32 70, 'acted') received"
check_has "monitor 输出 ActionInvoked(snooze)" "$MON_OUT" \
  "The signal org.freedesktop.Notifications.ActionInvoked(uint32 70, 'snooze') received"
MON="$MON_OUT" assert_py "monitor 行能被 notify.py 的 SIGNAL_RE 逐条解析（同形状副本，不 import infohub）" <<'PY'
import os, re
SIGNAL_RE = re.compile(r"(ActionInvoked|NotificationClosed)\(uint32\s+(\d+)(?:,\s*'?([^')]*)'?)?\)")
got = [(m.group(1), int(m.group(2)), (m.group(3) or "").strip())
       for m in SIGNAL_RE.finditer(os.environ["MON"])]
assert got == [("ActionInvoked", 70, "acted"), ("ActionInvoked", 70, "snooze")], got
PY

MON_CLOSE="$(GDBUS_STUB_ACTIONS='acted' GDBUS_STUB_MONITOR_ID=70 GDBUS_STUB_CLOSE=1 GDBUS_STUB_HOLD=0 \
  bash "$GDBUS" monitor --session --dest org.freedesktop.Notifications)"
check_has "monitor 可附带 NotificationClosed" "$MON_CLOSE" \
  "The signal org.freedesktop.Notifications.NotificationClosed(uint32 70, 2) received"

if GDBUS_STUB_MONITOR_FAIL=1 bash "$GDBUS" monitor --session \
  --dest org.freedesktop.Notifications >/dev/null 2>&1; then
  bad "GDBUS_STUB_MONITOR_FAIL=1 时 monitor 应退出非 0"
else
  ok "GDBUS_STUB_MONITOR_FAIL=1 时 monitor 退出非 0"
fi

# =========================================================================
printf '\nPASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
