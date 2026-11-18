#!/usr/bin/env bash
# 控制台消息中心验收（任务书 ~/hub-tasks/active/HUB_TASK_console-message-center.md §3 A1–A6）。
# 隔离：临时根 + 临时端口；不碰真库、不弹真通知、不重启任何服务；可复跑。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-msg-center-XXXXXX)
PASS=0; FAIL=0; UI_PID=""; HTTP_CODE=""; HTTP_BODY=""

cleanup() {
  if [ -n "$UI_PID" ]; then kill "$UI_PID" 2>/dev/null || true; wait "$UI_PID" 2>/dev/null || true; fi
  /usr/bin/rm -rf "$TMP"
}
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); if [ "$#" -ge 2 ] && [ -n "$2" ]; then printf 'FAIL  %s -- %s\n' "$1" "$2"; else printf 'FAIL  %s\n' "$1"; fi; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want=$2 got=$3"; fi; }
check_contains() { if [ -f "$3" ] && grep -qF -- "$2" "$3"; then ok "$1"; else bad "$1" "文件 $(basename "$3") 缺少字面量: $2"; fi; }

# ---- 隔离根（含媒体根） ---------------------------------------------------
cp "$HUB/hub.config.yaml" "$TMP/c.yaml"
python3 - "$TMP/c.yaml" "$TMP/root" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); text = p.read_text(encoding="utf-8")
text, n = re.subn(r"(?m)^root:.*$", "root: " + sys.argv[2], text, count=1)
if n != 1: raise SystemExit("配置里找不到 root: 行")
text, m = re.subn(r"(?m)^(  assets:\n(?:    .*\n)*?    roots:) \[\]$", r"\1 [media]", text, count=1)
if m != 1: raise SystemExit("配置里找不到 ui.assets.roots")
p.write_text(text, encoding="utf-8")
PY
export HUB_CONFIG="$TMP/c.yaml"
export HUB_ROOT="$TMP/root"
export PYTHONPATH="$HUB/src:"
DB="$TMP/root/hub.db"
mkdir -p "$TMP/root"
check_eq "隔离根写进临时配置（不碰真库）" "root: $TMP/root" "$(grep -m1 '^root:' "$TMP/c.yaml")"

hub_deliver() { bash "$HUB/bin/hub-deliver" "$@" 2>>"$TMP/hub-deliver.err"; }
db_scalar() { python3 - "$DB" "$1" "$2" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT " + sys.argv[3] + " FROM deliveries WHERE id = ?", (sys.argv[2],)).fetchone()
print("" if row is None or row[0] is None else row[0])
PY
}

# ---- HTTP 小工具 ----------------------------------------------------------
capture() {
  local method=$1 path=$2 data out
  shift 2; data="$*"
  if [ -n "$data" ]; then
    out=$(curl -sS --max-time 10 -X "$method" -H 'Content-Type: application/json' --data "$data" -w '\n%{http_code}' "$BASE$path")
  else out=$(curl -sS --max-time 10 -X "$method" -w '\n%{http_code}' "$BASE$path"); fi
  HTTP_CODE=$(printf '%s\n' "$out" | tail -n 1)
  HTTP_BODY=$(printf '%s\n' "$out" | sed '$d')
}
jget() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1" 2>/dev/null; }
body_get() { printf '%s' "$HTTP_BODY" | jget "$1"; }

# ---- 起临时控制台（端口 0） ----------------------------------------------
bash "$HUB/bin/hub-ui" --port 0 >"$TMP/ui.out" 2>"$TMP/ui.err" &
UI_PID=$!
PORT=""
for _ in $(seq 1 100); do
  [ -s "$TMP/ui.out" ] && PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$TMP/ui.out" 2>/dev/null || true)
  [ -n "$PORT" ] && break
  kill -0 "$UI_PID" 2>/dev/null || break
  sleep 0.05
done
if [ -z "$PORT" ]; then echo "FATAL: hub-ui 没起来"; cat "$TMP/ui.err" 2>/dev/null || true; exit 1; fi
BASE="http://127.0.0.1:$PORT"
echo "临时控制台: $BASE（HUB_ROOT=$TMP/root）"

# ---- 夹具：一张图 + 两条历史投递 ----------------------------------------
mkdir -p "$TMP/root/media"
python3 - "$TMP/root/media/shot.png" <<'PY'
import base64, sys
png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
open(sys.argv[1], "wb").write(base64.b64decode(png))
PY
OLD1=$(hub_deliver add --channel console --title 'S2 旧消息一' --body '旧正文一')
OLD2=$(hub_deliver add --channel console --title 'S2 旧消息二' --body '旧正文二')

# ---- A1：CLI 归档语义 -----------------------------------------------------
hub_deliver archive "$OLD1" --json >"$TMP/arch.json"
check_eq "A1 CLI archive 写入 archived_at" "$(python3 -c 'import json,sys; print(bool(json.load(open(sys.argv[1]))["archived_at"]))' "$TMP/arch.json")" "True"
hub_deliver list --json >"$TMP/list-default.json"
check_eq "A1 默认 list 不含归档行" "False" "$(python3 -c 'import json,sys; print(any(r["id"]==sys.argv[1] for r in json.load(open(sys.argv[2]))["deliveries"]))' "$OLD1" "$TMP/list-default.json")"
hub_deliver list --archived --json >"$TMP/list-arch.json"
check_eq "A1 list --archived 只含归档行" "True" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[2]))["deliveries"]; print([r["id"] for r in d]==[sys.argv[1]])' "$OLD1" "$TMP/list-arch.json")"
hub_deliver archive "$OLD1" --json >"$TMP/arch2.json"
check_eq "A1 重复归档幂等（时间不变）" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["archived_at"])' "$TMP/arch.json")" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["archived_at"])' "$TMP/arch2.json")"
hub_deliver archive d-does-not-exist --json >/dev/null 2>&1
check_eq "A1 归档未知 id 退出码 1" "1" "$?"
check_eq "A1 未归档夹具仍在默认 list" "True" "$(python3 -c 'import json,sys; print(any(r["id"]==sys.argv[1] for r in json.load(open(sys.argv[2]))["deliveries"]))' "$OLD2" "$TMP/list-default.json")"

# ---- A2：页面发消息 -------------------------------------------------------
capture POST /api/messages '{"text":"S2 页面发的一条","attachments":["media/shot.png"]}'
check_eq "A2 POST /api/messages 201" "201" "$HTTP_CODE"
MSG_ID=$(body_get 'd["id"]')
check_eq "A2 返回 id 非空" "True" "$([ -n "$MSG_ID" ] && echo True || echo False)"
check_eq "A2 库内 channel=console" "console" "$(db_scalar "$MSG_ID" channel)"
check_eq "A2 库内 kind=message" "message" "$(db_scalar "$MSG_ID" kind)"
check_eq "A2 附件落库为 JSON 数组" '["media/shot.png"]' "$(db_scalar "$MSG_ID" attachments)"

# ---- A3：列表 / 归档开关 --------------------------------------------------
capture GET /api/messages ''
check_eq "A3 列表含新消息" "True" "$(body_get "any(m['id']=='$MSG_ID' for m in d['messages'])")"
check_eq "A3 默认列表不含已归档行" "False" "$(body_get "any(m['id']=='$OLD1' for m in d['messages'])")"
capture GET '/api/messages?archived=1'
check_eq "A3 archived=1 含已归档行" "True" "$(body_get "any(m['id']=='$OLD1' for m in d['messages'])")"
capture POST /api/messages/archive "{\"id\":\"$MSG_ID\"}"
check_eq "A3 archive 200" "200" "$HTTP_CODE"
check_eq "A3 归档后 archived_at 非空" "True" "$([ -n "$(db_scalar "$MSG_ID" archived_at)" ] && echo True || echo False)"
capture GET /api/messages ''
check_eq "A3 归档后默认列表不含它" "False" "$(body_get "any(m['id']=='$MSG_ID' for m in d['messages'])")"
capture GET '/api/messages?archived=1'
check_eq "A3 归档后 archived=1 含它" "True" "$(body_get "any(m['id']=='$MSG_ID' for m in d['messages'])")"
capture POST /api/messages/archive '{"id":"d-nope"}'
check_eq "A3 归档未知 id ⇒ 404" "404" "$HTTP_CODE"

# ---- A4：附件可见 ---------------------------------------------------------
capture GET '/api/messages?archived=1&limit=10'
ATT=$(body_get "next(m['attachments'][0] for m in d['messages'] if m['id']=='$MSG_ID')")
check_eq "A4 附件数组首项是相对路径" "media/shot.png" "$ATT"
VID=$(python3 -c 'import base64,sys; print(base64.urlsafe_b64encode(sys.argv[1].encode()).decode().rstrip("="))' "$ATT")
curl -sS --max-time 10 -D "$TMP/thumb.h" -o "$TMP/thumb.bin" "$BASE/thumbs/$VID"
check_eq "A4 /thumbs/<id> 200" "200" "$(awk 'NR==1{print $2}' "$TMP/thumb.h")"
check_contains "A4 缩略图 Content-Type 是图片" "image/" "$TMP/thumb.h"
curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$BASE/asset/$VID" >"$TMP/asset.code"
check_eq "A4 /asset/<id> 可取原图" "200" "$(cat "$TMP/asset.code")"

# ---- A4b：附件缺失 ⇒ 404（页面降级为「附件缺失」，不是 500）---------------
capture POST /api/messages '{"text":"附件缺失的那条","attachments":["media/nope.png"]}'
MISS_ID=$(body_get 'd["id"]')
MISS_VID=$(python3 -c 'import base64; print(base64.urlsafe_b64encode(b"media/nope.png").decode().rstrip("="))')
MISS_CODE=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$BASE/thumbs/$MISS_VID")
check_eq "A4b 附件不存在 ⇒ /thumbs 404（非 500）" "404" "$MISS_CODE"
capture GET '/api/messages?limit=50'
check_eq "A4b 缺附件的消息本身仍可列出" "True" "$(body_get "any(m['id']=='$MISS_ID' for m in d['messages'])")"

# ---- A5：页面结构 ---------------------------------------------------------
curl -sS --max-time 10 "$BASE/" -o "$TMP/page.html"
check_contains "A5 页面含容器 #messages" 'id="messages"' "$TMP/page.html"
check_contains "A5 页面含输入框 #msgin" 'id="msgin"' "$TMP/page.html"
check_contains "A5 页面含归档开关 #msgarch" 'id="msgarch"' "$TMP/page.html"
check_contains "A5 前端调 POST /api/messages" '/api/messages' "$TMP/page.html"
check_contains "A5 前端调归档路由" '/api/messages/archive' "$TMP/page.html"
check_contains "A5 行模板 message-row" '<template id="message-row">' "$TMP/page.html"
check_contains "A5 附件走 /thumbs/" '/thumbs/' "$TMP/page.html"
TPL=$(python3 - "$TMP/page.html" <<'PY'
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'<template id="message-row">(.*?)</template>', html, re.S)
if not m: print("MISSING"); raise SystemExit
row = m.group(1)
print("OK" if all(k in row for k in ("title", "body", "atts", "archive")) else "BAD")
PY
)
check_eq "A5 模板字段齐（title/body/附件/归档）" "OK" "$TPL"

# ---- A6：拒绝面 -----------------------------------------------------------
capture POST /api/messages '{"text":"   "}'
check_eq "A6 空文本 ⇒ 400" "400" "$HTTP_CODE"
capture POST /api/messages '{"text":"x","attachments":["/etc/passwd"]}'
check_eq "A6 绝对路径附件 ⇒ 400" "400" "$HTTP_CODE"
capture GET '/api/messages?archived=2'
check_eq "A6 archived=2 ⇒ 400" "400" "$HTTP_CODE"
capture GET '/api/messages?limit=0'
check_eq "A6 limit=0 ⇒ 400" "400" "$HTTP_CODE"

capture GET '/api/deliver/pending?limit=50'
check_eq "A6 归档行不出现在 pending" "False" "$(body_get "any(m['id']=='$MSG_ID' for m in d['deliveries'])")"

printf '\n%d 通过 / %d 失败\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && { echo "ALL PASS"; exit 0; } || exit 1
