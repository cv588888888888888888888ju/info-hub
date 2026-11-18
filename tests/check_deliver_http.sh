#!/usr/bin/env bash
# S5 验收：本机投递 HTTP 接口 + 认领租约（任务书 A7 / A11；契约 INTERFACE.md §11.5）。
# 隔离：临时根 + 临时端口（内核分配）；不碰真库、不重启任何服务；可复跑。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-s5-http-XXXXXX)
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

hub_deliver() { bash "$HUB/bin/hub-deliver" "$@" 2>>"$TMP/hub-deliver.err"; }

# ---- HTTP 小工具 ----------------------------------------------------------
status_of() { curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$@"; }

capture() { # capture METHOD PATH JSON [extra curl args...]
  local method=$1 path=$2 data=$3
  shift 3
  local out
  if [ -n "$data" ]; then
    out=$(curl -sS --max-time 10 -X "$method" -H 'Content-Type: application/json' \
      --data "$data" -w '\n%{http_code}' "$BASE$path" "$@")
  else
    out=$(curl -sS --max-time 10 -X "$method" -w '\n%{http_code}' "$BASE$path" "$@")
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

if command -v ss >/dev/null 2>&1; then
  if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qx "127.0.0.1:$PORT"; then
    ok "只绑 127.0.0.1:$PORT"
  else
    bad "监听地址不是 127.0.0.1:$PORT" "$(ss -ltnH 2>/dev/null | awk '{print $4}' | tr '\n' ' ')"
  fi
fi

# ---- A7：失败语义 + 成功回执 ---------------------------------------------
echo
echo "== A7：HTTP 路由 =="
ID1=$(hub_deliver add --channel dsh --title 'S5 夹具一' --body '第一条')
ID2=$(hub_deliver add --channel dsh --title 'S5 夹具二' --body '第二条')

capture GET '/api/deliver/pending?channel=dsh' ''
check_eq "A7 pending?channel=dsh = 200" "200" "$HTTP_CODE"
check_eq "A7 pending 返回两条 dsh" "2" "$(body_get 'len(d["deliveries"])')"

check_eq "A7 Host: evil.example = 403" "403" "$(status_of -H 'Host: evil.example' "$BASE/api/deliver/pending?channel=dsh")"
check_eq "A7 Origin: 非本机 = 403" "403" "$(status_of -H 'Origin: http://evil.example' "$BASE/api/deliver/pending?channel=dsh")"

capture POST '/api/deliver/ack' '{"id":"no-such-id"}'
check_eq "A7 POST ack 未知 id = 404" "404" "$HTTP_CODE"
check_eq "A7 404 正文带 error" "True" "$(body_get '"error" in d')"

capture POST '/api/deliver/reply' "$(printf '{"id":"%s","text":""}' "$ID1")"
check_eq "A7 空 reply = 400" "400" "$HTTP_CODE"
capture POST '/api/deliver/reply' "$(printf '{"id":"%s","text":"   "}' "$ID1")"
check_eq "A7 全空白 reply = 400" "400" "$HTTP_CODE"
capture POST '/api/deliver/reply' '{"id":"no-such-id","text":"x"}'
check_eq "A7 reply 未知 id = 404" "404" "$HTTP_CODE"

capture POST '/api/deliver/ack' "$(printf '{"id":"%s","action":"no-such-action"}' "$ID1")"
check_eq "A7 ack 不认识 action = 400" "400" "$HTTP_CODE"

capture POST '/api/deliver/ack' "$(printf '{"id":"%s"}' "$ID1")"
check_eq "A7 ack 成功 = 200" "200" "$HTTP_CODE"
check_eq "A7 ack 后 status=acked" "acked" "$(body_get 'd["status"]')"
check_eq "A7 首次 ack changed=true" "True" "$(body_get 'd["changed"]')"
ACK_AT=$(body_get 'd["acked_at"]')
check_eq "A7 acked_at 非空" "True" "$( [ -n "$ACK_AT" ] && echo True || echo False )"

capture POST '/api/deliver/ack' "$(printf '{"id":"%s"}' "$ID1")"
check_eq "A7 重复 ack = 200" "200" "$HTTP_CODE"
check_eq "A7 重复 ack changed=false" "False" "$(body_get 'd["changed"]')"
check_eq "A7 重复 ack 不改 acked_at" "$ACK_AT" "$(body_get 'd["acked_at"]')"

TEXT='收到，明天做 "带引号" 与 空格'
REPLY_DATA=$(python3 -c 'import json,sys; print(json.dumps({"id": sys.argv[1], "text": sys.argv[2]}, ensure_ascii=False))' "$ID1" "$TEXT")
capture POST '/api/deliver/reply' "$REPLY_DATA"
check_eq "A7 reply 成功 = 200" "200" "$HTTP_CODE"
check_eq "A7 reply 后 status=replied" "replied" "$(body_get 'd["status"]')"
check_eq "A7 reply 文本原样" "$TEXT" "$(body_get 'd["reply"]')"

capture POST '/api/deliver/claim' '{"id":"no-such-id","owner":"alice","ttl_s":30}'
check_eq "A7 claim 未知 id = 404" "404" "$HTTP_CODE"
capture POST '/api/deliver/claim' "$(printf '{"id":"%s","owner":"  ","ttl_s":30}' "$ID2")"
check_eq "A7 claim owner 为空 = 400" "400" "$HTTP_CODE"
capture POST '/api/deliver/claim' "$(printf '{"id":"%s","owner":"alice","ttl_s":0}' "$ID2")"
check_eq "A7 claim ttl_s=0 = 400" "400" "$HTTP_CODE"
capture POST '/api/deliver/claim' "$(printf '{"id":"%s","owner":"alice","ttl_s":"30"}' "$ID2")"
check_eq "A7 claim ttl_s 非整数 = 400" "400" "$HTTP_CODE"
capture POST '/api/deliver/no-such-route' '{}'
check_eq "A7 未知 deliver 路由 = 404" "404" "$HTTP_CODE"

# ---- A11：认领租约 --------------------------------------------------------
echo
echo "== A11：认领租约 =="
hub_deliver ack "$ID2" >/dev/null
ID3=$(hub_deliver add --channel dsh --title 'S5 租约一' --body 'lease-1')
ID4=$(hub_deliver add --channel dsh --title 'S5 租约二' --body 'lease-2')

capture GET '/api/deliver/pending?channel=dsh&limit=50' ''
check_eq "A11 两条 pending 都取得到" "2" "$(body_get 'len(d["deliveries"])')"
check_eq "A11 含 ID3" "True" "$(body_get "any(x['id']=='$ID3' for x in d['deliveries'])")"
check_eq "A11 含 ID4" "True" "$(body_get "any(x['id']=='$ID4' for x in d['deliveries'])")"

capture POST '/api/deliver/claim' "$(printf '{"id":"%s","owner":"alice","ttl_s":30}' "$ID3")"
check_eq "A11 claim ID3 = 200" "200" "$HTTP_CODE"
check_eq "A11 lease_owner=alice" "alice" "$(body_get 'd["lease_owner"]')"
LEASE3=$(body_get 'd["lease_until"]')
check_eq "A11 lease_until 非空" "True" "$( [ -n "$LEASE3" ] && echo True || echo False )"

capture GET '/api/deliver/pending?channel=dsh' ''
check_eq "A11 第二次 pending 取不到已认领未过期的 ID3" "False" "$(body_get "any(x['id']=='$ID3' for x in d['deliveries'])")"
check_eq "A11 未认领的 ID4 仍取得到" "True" "$(body_get "any(x['id']=='$ID4' for x in d['deliveries'])")"

capture POST '/api/deliver/claim' "$(printf '{"id":"%s","owner":"bob","ttl_s":30}' "$ID4")"
check_eq "A11 claim ID4 bob = 200" "200" "$HTTP_CODE"
capture POST '/api/deliver/claim' "$(printf '{"id":"%s","owner":"carol","ttl_s":30}' "$ID4")"
check_eq "A11 重复 claim ID4 carol = 200" "200" "$HTTP_CODE"
check_eq "A11 重复 claim 只有最新 owner 生效（HTTP）" "carol" "$(body_get 'd["lease_owner"]')"

ROWS=$(hub_deliver list --json)
check_eq "A11 库里 ID4 lease_owner=carol" "carol" "$(printf '%s' "$ROWS" | jget "[x['lease_owner'] for x in d['deliveries'] if x['id']=='$ID4'][0]")"
check_eq "A11 库里 ID3 lease_owner 仍=alice" "alice" "$(printf '%s' "$ROWS" | jget "[x['lease_owner'] for x in d['deliveries'] if x['id']=='$ID3'][0]")"

capture GET '/api/deliver/pending?channel=dsh' ''
check_eq "A11 两条都被认领后 pending 为空" "0" "$(body_get 'len(d["deliveries"])')"

capture POST '/api/deliver/claim' "$(printf '{"id":"%s","owner":"dave","ttl_s":1}' "$ID3")"
check_eq "A11 短租约 claim = 200" "200" "$HTTP_CODE"
check_eq "A11 短租约 owner=dave" "dave" "$(body_get 'd["lease_owner"]')"
sleep 1.5
capture GET '/api/deliver/pending?channel=dsh' ''
check_eq "A11 ttl_s=1 过期后可再取到 ID3" "True" "$(body_get "any(x['id']=='$ID3' for x in d['deliveries'])")"
check_eq "A11 ID4 未过期仍被遮挡" "False" "$(body_get "any(x['id']=='$ID4' for x in d['deliveries'])")"
check_eq "A11 过期行 lease_owner 不自动清空" "dave" "$(body_get "[x['lease_owner'] for x in d['deliveries'] if x['id']=='$ID3'][0]")"

capture GET '/api/deliver/pending?channel=dsh&limit=abc' ''
check_eq "A11 limit 非整数 = 400" "400" "$HTTP_CODE"
capture GET '/api/deliver/pending?channel=no-such-channel' ''
check_eq "A11 未知 channel = 200 空列表" "200" "$HTTP_CODE"
check_eq "A11 未知 channel 没有行" "0" "$(body_get 'len(d["deliveries"])')"

# ---- 收尾 ----------------------------------------------------------------
echo
echo "RESULT pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
