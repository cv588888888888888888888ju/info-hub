#!/usr/bin/env bash
# info-hub S9 验收：phone-mcp 通道插件（任务书 A10 / A17）。
# 判据：替身推送成功 ⇒ deliveries 多一行 channel=phone、替身收到 delivery_id；layout 有 ⇒ 带 v=1 与
# options[*].key，layout 无 ⇒ 只给 title+text；bridge 失败 ⇒ 退出码 1 且库不变；acks --apply 后
# status=acked 而待办 status 不变；schema 可解析；pending 重推按退避到点；dismiss 调 hub_dismiss。
# 只在隔离环境跑：临时根 + 临时配置 + tests/stubs/mcp_stub.py 替身（不碰真手机、不碰真库）。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BIN="$ROOT/bin"
STUB="$ROOT/tests/stubs/mcp_stub.py"
export PYTHONPATH="$ROOT/src"

TMP="$(mktemp -d /tmp/infohub-s9-XXXXXX)"
STUB_PID=""
cleanup() {
  [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null
  /usr/bin/rm -rf "$TMP"
}
trap cleanup EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "$2"; }
sqlq() {  # sqlq <root> <cfg> <python-expr>，库里 st 已就绪
  HUB_ROOT="$1" HUB_CONFIG="$2" python3 - "$3" <<'PY'
import sys
from infohub.config import load
from infohub.store import Store
cfg = load(); st = Store(cfg.db_path)
print(eval(sys.argv[1]))
PY
}
jget() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
LOG="$TMP/mcp.log"

mkcfg() {  # mkcfg <out> <root> [dotted.k=v ...]
  local out="$1" root="$2"; shift 2
  python3 - "$ROOT/hub.config.yaml" "$out" "$root" "$@" <<'PY'
import sys, yaml
src, dst, root = sys.argv[1], sys.argv[2], sys.argv[3]
raw = yaml.safe_load(open(src, encoding="utf-8"))
raw["root"] = root
def put(dotted, value):
    node = raw
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
for kv in sys.argv[4:]:
    key, value = kv.split("=", 1)
    put(key, yaml.safe_load(value))
yaml.safe_dump(raw, open(dst, "w", encoding="utf-8"), allow_unicode=True)
PY
}

R0="$TMP/r0"; C0="$TMP/c0.yaml"
mkcfg "$C0" "$R0" "plugins.phone-mcp.enabled=true" \
      "phone_remind.bridge=http://127.0.0.1:$PORT/mcp" "phone_remind.timeout_s=5"
HUB_ROOT="$R0" HUB_CONFIG="$C0" python3 "$ROOT/tests/m2_seed.py" 1 0 "s9" >/dev/null
TODO=$(sqlq "$R0" "$C0" "st.conn.execute('select id from todos limit 1').fetchone()[0]")

start_stub() {
  : > "$LOG"
  python3 "$STUB" "$PORT" "$LOG" &
  STUB_PID=$!
  python3 - "$PORT" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
for _ in range(100):
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port)); s.close(); break
    except OSError:
        time.sleep(0.05)
    finally:
        s.close()
PY
}
stop_stub() { kill "$STUB_PID" 2>/dev/null; wait "$STUB_PID" 2>/dev/null; STUB_PID=""; }

# ================================================================ T1 layout 有 ⇒ 带 v=1 / options 键
start_stub
OUT=$(HUB_ROOT="$R0" HUB_CONFIG="$C0" "$BIN/hub-phone-push" --title "交材料" --body "明天 10 点" \
      --mode question --options '[{"key":"ack","label":"知道了"},{"key":"reply","label":"回复"}]' \
      --layout '{"v":1,"title":"交材料","input":{"enabled":true}}' --todo-id "$TODO" --json 2>"$TMP/e1")
RC=$?
[ "$RC" = 0 ] && pass "T1 推送退出 0" || fail "T1 推送退出 $RC" "$(cat "$TMP/e1")"
DID=$(printf '%s' "$OUT" | jget "d['delivery_id']")
ROW=$(sqlq "$R0" "$C0" "dict(st.conn.execute('select channel,status,todo_id from deliveries where id=?',('$DID',)).fetchone())")
case "$ROW" in *"'channel': 'phone'"*"'status': 'delivered'"*) pass "T1 台账 channel=phone status=delivered" ;;
  *) fail "T1 台账行不对" "$ROW" ;; esac
ARGS=$(python3 - "$LOG" <<'PY'
import json, sys
row = json.loads(open(sys.argv[1], encoding="utf-8").readline())
a = row["args"]
print(json.dumps({"tool": row["tool"], "did": a.get("delivery_id"), "layout_v": (a.get("layout") or {}).get("v"),
                  "opt_keys": [o.get("key") for o in (a.get("options") or [])], "mode": a.get("mode")}, ensure_ascii=False))
PY
)
case "$ARGS" in *"\"did\": \"$DID\""*) pass "T1 替身收到同一 delivery_id" ;; *) fail "T1 delivery_id 不一致" "$ARGS" ;; esac
case "$ARGS" in *'"layout_v": 1'*) pass "T1 替身收到 layout.v=1" ;; *) fail "T1 layout 未透传" "$ARGS" ;; esac
case "$ARGS" in *'"opt_keys": ["ack", "reply"]'*) pass "T1 替身收到 options[*].key" ;; *) fail "T1 options 不对" "$ARGS" ;; esac
case "$ARGS" in *'"mode": "question"'*) pass "T1 mode 透传" ;; *) fail "T1 mode 不对" "$ARGS" ;; esac

# ================================================================ T2 layout 缺 ⇒ 只给 title+text
DID2=$(HUB_ROOT="$R0" HUB_CONFIG="$C0" "$BIN/hub-phone-push" --title "无布局" --body "正文X" --json 2>"$TMP/e2" | jget "d['delivery_id']")
RC=$?
[ "$RC" = 0 ] && pass "T2 无 layout 推送退出 0" || fail "T2 退出 $RC" "$(cat "$TMP/e2")"
A2=$(python3 - "$LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
a = rows[-1]["args"]
print(json.dumps({"text": a.get("text"), "has_layout": "layout" in a, "title": a.get("title")}, ensure_ascii=False))
PY
)
case "$A2" in *'"text": "正文X"'*) pass "T2 替身收到 text=正文" ;; *) fail "T2 text 不对" "$A2" ;; esac
case "$A2" in *'"has_layout": false'*) pass "T2 替身未收到 layout" ;; *) fail "T2 仍带 layout" "$A2" ;; esac

# ================================================================ T3 bridge 失败 ⇒ 退出 1 且库不变
BEFORE=$(sqlq "$R0" "$C0" "st.conn.execute('select count(*) from deliveries').fetchone()[0]")
stop_stub
HUB_ROOT="$R0" HUB_CONFIG="$C0" "$BIN/hub-phone-push" --title "会失败" --json >"$TMP/o3" 2>"$TMP/e3"
RC=$?
[ "$RC" = 1 ] && pass "T3 bridge 失败退出 1" || fail "T3 退出 $RC" "$(cat "$TMP/e3")"
AFTER=$(sqlq "$R0" "$C0" "st.conn.execute('select count(*) from deliveries').fetchone()[0]")
[ "$BEFORE" = "$AFTER" ] && pass "T3 失败不写库（$BEFORE 行不变）" || fail "T3 失败却写了库" "$BEFORE → $AFTER"

# ================================================================ T4 ack ⇒ status=acked，待办 status 不变
TS_BEFORE=$(sqlq "$R0" "$C0" "st.conn.execute('select status from todos where id=?',('$TODO',)).fetchone()[0]")
HUB_ROOT="$R0" HUB_CONFIG="$C0" "$BIN/hub-deliver" ack "$DID" >/dev/null 2>&1
[ "$?" = 0 ] && pass "T4 ack 退出 0" || fail "T4 ack 失败" "-"
ST_ROW=$(sqlq "$R0" "$C0" "st.conn.execute('select status from deliveries where id=?',('$DID',)).fetchone()[0]")
[ "$ST_ROW" = "acked" ] && pass "T4 投递 status=acked" || fail "T4 status=$ST_ROW" "-"
TS_AFTER=$(sqlq "$R0" "$C0" "st.conn.execute('select status from todos where id=?',('$TODO',)).fetchone()[0]")
[ "$TS_BEFORE" = "$TS_AFTER" ] && pass "T4 待办 status 不变（$TS_AFTER）" || fail "T4 待办被改" "$TS_BEFORE → $TS_AFTER"

# ================================================================ T5 schema 可解析
python3 -c "import json;json.load(open('$ROOT/plugins/channels/phone-mcp/layout.schema.json'))" \
  && pass "T5 layout.schema.json 可解析" || fail "T5 schema 坏了" "-"

# ================================================================ T6 pending 重推（attempts=0 立刻推）
PID=$(HUB_ROOT="$R0" HUB_CONFIG="$C0" "$BIN/hub-deliver" add --channel phone --title "排队中的" --body "b" 2>/dev/null | head -1)
start_stub
HUB_ROOT="$R0" HUB_CONFIG="$C0" "$BIN/hub-phone-push" --pending --json >"$TMP/o6" 2>"$TMP/e6"
RC=$?
[ "$RC" = 0 ] && pass "T6 pending 重推退出 0" || fail "T6 退出 $RC" "$(cat "$TMP/e6")"
PST=$(sqlq "$R0" "$C0" "tuple(st.conn.execute('select status,attempts from deliveries where id=?',('$PID',)).fetchone())")
[ "$PST" = "('delivered', 0)" ] && pass "T6 pending 行变 delivered" || fail "T6 行状态不对" "$PST"

# ================================================================ T7 dismiss ⇒ 调 hub_dismiss
HUB_ROOT="$R0" HUB_CONFIG="$C0" "$BIN/hub-phone-push" --dismiss "$DID" --json >"$TMP/o7" 2>"$TMP/e7"
RC=$?
[ "$RC" = 0 ] && pass "T7 dismiss 退出 0" || fail "T7 退出 $RC" "$(cat "$TMP/e7")"
D7=$(python3 - "$LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
r = rows[-1]
print(r["tool"], r["args"].get("delivery_id"))
PY
)
[ "$D7" = "hub_dismiss $DID" ] && pass "T7 替身收到 hub_dismiss + delivery_id" || fail "T7 调用不对" "$D7"

echo "---- $PASS ok, $FAIL fail"
[ "$FAIL" = 0 ]