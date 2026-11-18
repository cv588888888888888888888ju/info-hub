#!/usr/bin/env bash
# S6 验收：控制台投递面板（任务书 A8；契约 INTERFACE.md §4.8 控制台 / §11.5 投递 HTTP）。
# 隔离：临时根 + 临时端口（内核分配）；不碰真库、不弹真通知、不重启任何服务；可复跑。
# 断言面：GET / 的 HTML 有 id="deliveries"、每条投递的行模板有 title/body/channel/status
# 与「确认」「回复」两个按钮、前端调 /api/deliver/pending|ack|reply 且空文本不发请求；
# 再用 curl 走 ack/reply 断言 200 且库行状态变化。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-s6-ui-XXXXXX)
PASS=0
FAIL=0
UI_PID=""
HTTP_CODE=""
HTTP_BODY=""

cleanup() {
  if [ -n "$UI_PID" ]; then
    kill "$UI_PID" 2>/dev/null || true
    wait "$UI_PID" 2>/dev/null || true
  fi
  /usr/bin/rm -rf "$TMP"
}
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() {
  FAIL=$((FAIL + 1))
  if [ "$#" -ge 2 ] && [ -n "$2" ]; then printf 'FAIL  %s -- %s\n' "$1" "$2"
  else printf 'FAIL  %s\n' "$1"; fi
}
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want=$2 got=$3"; fi; }
check_contains() { # check_contains <label> <needle> <file>
  if [ -f "$3" ] && grep -qF -- "$2" "$3"; then ok "$1"
  else bad "$1" "文件 ${3##*/} 缺少字面量: $2"; fi
}

# ---- 隔离根 ---------------------------------------------------------------
cp "$HUB/hub.config.yaml" "$TMP/c.yaml"
python3 - "$TMP/c.yaml" "$TMP/root" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
text = p.read_text(encoding="utf-8")
text, n = re.subn(r"(?m)^root:.*$", "root: " + sys.argv[2], text, count=1)
if n != 1:
    raise SystemExit("配置里找不到 root: 行")
p.write_text(text, encoding="utf-8")
PY
export HUB_CONFIG="$TMP/c.yaml"
export HUB_ROOT="$TMP/root"
export PYTHONPATH="$HUB/src${PYTHONPATH:+:$PYTHONPATH}"
DB="$TMP/root/hub.db"

check_eq "隔离根写进临时配置（不碰真库）" "root: $TMP/root" "$(grep -m1 '^root:' "$TMP/c.yaml")"

hub_deliver() { bash "$HUB/bin/hub-deliver" "$@" 2>>"$TMP/hub-deliver.err"; }

db_field() { # db_field <id> <column> —— 直读临时库，证明确实落库
  python3 - "$DB" "$1" "$2" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT " + sys.argv[3] + " FROM deliveries WHERE id = ?",
                   (sys.argv[2],)).fetchone()
print("" if row is None or row[0] is None else row[0])
PY
}

# ---- HTTP 小工具（与 check_deliver_http.sh 同风格）------------------------
capture() { # capture METHOD PATH JSON
  local method=$1 path=$2 data=$3
  local out
  if [ -n "$data" ]; then
    out=$(curl -sS --max-time 10 -X "$method" -H 'Content-Type: application/json' \
      --data "$data" -w '\n%{http_code}' "$BASE$path")
  else
    out=$(curl -sS --max-time 10 -X "$method" -w '\n%{http_code}' "$BASE$path")
  fi
  HTTP_CODE=$(printf '%s\n' "$out" | tail -n 1)
  HTTP_BODY=$(printf '%s\n' "$out" | sed '$d')
}
jget() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1" 2>/dev/null; }
body_get() { printf '%s' "$HTTP_BODY" | jget "$1"; }

# ---- 起临时控制台（端口 0）------------------------------------------------
bash "$HUB/bin/hub-ui" --port 0 >"$TMP/ui.out" 2>"$TMP/ui.err" &
UI_PID=$!
PORT=""
for _ in $(seq 1 100); do
  if [ -s "$TMP/ui.out" ]; then
    PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$TMP/ui.out" 2>/dev/null || true)
    [ -n "$PORT" ] && break
  fi
  if ! kill -0 "$UI_PID" 2>/dev/null; then break; fi
  sleep 0.05
done
if [ -z "$PORT" ]; then
  echo "FATAL: hub-ui 没起来"
  cat "$TMP/ui.err" 2>/dev/null || true
  exit 1
fi
BASE="http://127.0.0.1:$PORT"
echo "临时控制台: $BASE（HUB_ROOT=$TMP/root）"

# ---- 夹具：两条待确认投递 -------------------------------------------------
ID1=$(hub_deliver add --channel dsh --title 'S6 面板夹具一' --body '第一条正文')
ID2=$(hub_deliver add --channel computer --title 'S6 面板夹具二' --body '第二条正文')
check_eq "夹具：两条投递 id 非空" "True" "$( [ -n "$ID1" ] && [ -n "$ID2" ] && echo True || echo False )"

# ---- A8-1/2：页面结构 -----------------------------------------------------
echo
echo "== A8：控制台投递面板 =="
PAGE_CODE=$(curl -sS --max-time 10 -o "$TMP/page.html" -w '%{http_code}' "$BASE/")
check_eq "A8 GET / = 200" "200" "$PAGE_CODE"
check_contains 'A8 页面含容器 id="deliveries"' 'id="deliveries"' "$TMP/page.html"
check_contains 'A8 前端调 /api/deliver/pending' '/api/deliver/pending' "$TMP/page.html"
check_contains 'A8 前端调 /api/deliver/ack' '/api/deliver/ack' "$TMP/page.html"
check_contains 'A8 前端调 /api/deliver/reply' '/api/deliver/reply' "$TMP/page.html"
check_contains 'A8 回复用 prompt/输入框取文本' 'prompt(' "$TMP/page.html"
check_contains 'A8 空回复不发请求（trim 守卫）' 'if (!text.trim())' "$TMP/page.html"

# 每条可确认行的行模板：title/body/channel/status + 两个按钮
python3 - "$TMP/page.html" "$TMP/tpl.html" <<'PY'
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'<template id="delivery-row">(.*?)</template>', html, re.S)
if not m:
    raise SystemExit("找不到 <template id=\"delivery-row\">")
open(sys.argv[2], "w", encoding="utf-8").write(m.group(1))
PY
check_eq "A8 找到每条投递的行模板" "0" "$?"
for fld in title body channel status; do
  check_contains "A8 行模板显示 $fld" "data-f=\"$fld\"" "$TMP/tpl.html"
done
check_contains 'A8 行模板可展开看 layout（任务书 S6）' 'data-f="layout"' "$TMP/tpl.html"
check_contains 'A8 行模板有「确认」按钮 → ack' 'data-act="ack">确认<' "$TMP/tpl.html"
check_contains 'A8 行模板有「回复」按钮 → reply' 'data-act="reply">回复<' "$TMP/tpl.html"

# ---- A8-3：pending 列表真取到夹具 -----------------------------------------
capture GET '/api/deliver/pending?limit=50' ''
check_eq "A8 pending = 200" "200" "$HTTP_CODE"
check_eq "A8 pending 返回两条夹具" "2" "$(body_get 'len(d["deliveries"])')"
check_eq "A8 pending 含 title/body/channel/status 字段" "True" \
  "$(body_get "all(k in d['deliveries'][0] for k in ('title','body','channel','status'))")"
check_eq "A8 pending 含 夹具一" "True" "$(body_get "any(x['id']=='$ID1' for x in d['deliveries'])")"

# ---- A8-4：ack（面板「确认」按钮打的那条路由）-----------------------------
capture POST '/api/deliver/ack' "$(printf '{"id":"%s"}' "$ID1")"
check_eq "A8 POST ack = 200" "200" "$HTTP_CODE"
check_eq "A8 ack 响应 status=acked" "acked" "$(body_get 'd["status"]')"
check_eq "A8 ack 响应 changed=true" "True" "$(body_get 'd["changed"]')"
check_eq "A8 库行 status=acked" "acked" "$(db_field "$ID1" status)"
check_eq "A8 库行 acked_at 非空" "True" "$( [ -n "$(db_field "$ID1" acked_at)" ] && echo True || echo False )"

# 幂等：再点一次不刷新 acked_at
ACK_AT=$(db_field "$ID1" acked_at)
capture POST '/api/deliver/ack' "$(printf '{"id":"%s"}' "$ID1")"
check_eq "A8 重复 ack = 200" "200" "$HTTP_CODE"
check_eq "A8 重复 ack changed=false" "False" "$(body_get 'd["changed"]')"
check_eq "A8 重复 ack 不改 acked_at" "$ACK_AT" "$(db_field "$ID1" acked_at)"

# ---- A8-5：reply（面板「回复」按钮打的那条路由）---------------------------
capture POST '/api/deliver/reply' "$(printf '{"id":"%s","text":""}' "$ID2")"
check_eq "A8 空回复 = 400（前端也不发）" "400" "$HTTP_CODE"
capture POST '/api/deliver/reply' "$(printf '{"id":"%s","text":"   "}' "$ID2")"
check_eq "A8 全空白回复 = 400" "400" "$HTTP_CODE"
check_eq "A8 空回复后库行 reply 仍为空" "" "$(db_field "$ID2" reply)"
check_eq "A8 空回复后库行状态仍 pending" "pending" "$(db_field "$ID2" status)"

TEXT='收到，明天做 "带引号" 与 空格'
REPLY_DATA=$(python3 -c 'import json,sys; print(json.dumps({"id": sys.argv[1], "text": sys.argv[2]}, ensure_ascii=False))' "$ID2" "$TEXT")
capture POST '/api/deliver/reply' "$REPLY_DATA"
check_eq "A8 POST reply = 200" "200" "$HTTP_CODE"
check_eq "A8 reply 响应 status=replied" "replied" "$(body_get 'd["status"]')"
check_eq "A8 reply 响应文本原样" "$TEXT" "$(body_get 'd["reply"]')"
check_eq "A8 库行 status=replied" "replied" "$(db_field "$ID2" status)"
check_eq "A8 库行 reply 原样" "$TEXT" "$(db_field "$ID2" reply)"
check_eq "A8 库行 reply_at 非空" "True" "$( [ -n "$(db_field "$ID2" reply_at)" ] && echo True || echo False )"

# ack 只动自己那一行
check_eq "A8 ack 只动自己那一行（第二条仍 replied）" "replied" "$(db_field "$ID2" status)"

# ---- 收尾 ----------------------------------------------------------------
echo
echo "RESULT pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
