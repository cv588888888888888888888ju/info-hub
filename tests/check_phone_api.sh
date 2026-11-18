#!/usr/bin/env bash
# 手机主页接口验收（INTERFACE §11.10）：token 门禁、列消息、发消息、归档、附件、拒绝面。
# 隔离：临时根 + 临时端口（--host 127.0.0.1 --port 0）；不碰真库、不绑 WG 地址。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-phone-api-XXXXXX)
PASS=0; FAIL=0; PID=""; CODE=""; BODY=""

cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null || true; /usr/bin/rm -rf "$TMP"; }
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); if [ "$#" -ge 2 ] && [ -n "$2" ]; then printf 'FAIL  %s -- %s\n' "$1" "$2"; else printf 'FAIL  %s\n' "$1"; fi; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want=$2 got=$3"; fi; }

cp "$HUB/hub.config.yaml" "$TMP/c.yaml"
python3 - "$TMP/c.yaml" "$TMP/root" <<'PY'
import os, re, sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text(encoding="utf-8")
t, n = re.subn(r"(?m)^root:.*$", "root: " + sys.argv[2], t, count=1)
if n != 1: raise SystemExit("配置里找不到 root: 行")
# 暴露面：exposure 固定 public，便于断言页面确实不再有「密码/横幅」那套东西
t, e = re.subn(r"(?m)^  exposure:.*$", "  exposure: public", t, count=1)
if e != 1: raise SystemExit("配置里找不到 phone_api.exposure")
t, m = re.subn(r"(?m)^(  assets:\n(?:    .*\n)*?    roots:) \[\]$", r"\1 [media]", t, count=1)
if m != 1: raise SystemExit("配置里找不到 ui.assets.roots")
p.write_text(t, encoding="utf-8")
PY
export HUB_CONFIG="$TMP/c.yaml" HUB_ROOT="$TMP/root" PYTHONPATH="$HUB/src:"
mkdir -p "$TMP/root/media"
printf 'doc\n' > "$TMP/root/media/说明.md"
python3 - "$TMP/root/media/a.png" <<'PY'
import base64, sys
open(sys.argv[1], "wb").write(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
PY

bash "$HUB/bin/hub-phone-api" --host 127.0.0.1 --port 0 >"$TMP/out.json" 2>"$TMP/err.log" &
PID=$!
# 第一行是暴露面/密码状态，第二行才是 {"host","port","url"}；等后者出现
for _ in $(seq 1 60); do grep -q '"port"' "$TMP/out.json" 2>/dev/null && break; sleep 0.1; done
PORT=$(python3 -c 'import json,sys; print(next(json.loads(l)["port"] for l in open(sys.argv[1]) if "\"port\"" in l))' "$TMP/out.json" 2>/dev/null || true)
if [ -z "$PORT" ]; then echo "FATAL: phone_api 没起来"; cat "$TMP/err.log"; exit 1; fi
BASE="http://127.0.0.1:$PORT"
# token 现在可以不写在配置里：配置留空就用服务端生成的 $HUB_ROOT/phone_api.token（§11.10）
TOKEN=$(sed -n "s/^  token: *'\{0,1\}\([^']*\)'\{0,1\} *\$/\1/p" "$TMP/c.yaml")
[ -n "$TOKEN" ] || TOKEN=$(cat "$TMP/root/phone_api.token")
echo "临时 phone_api: $BASE"

get() {
  local path=$1 tok="yes"
  [ "$#" -ge 2 ] && tok=$2
  if [ "$tok" = "yes" ]; then
    curl -sS --max-time 10 -H "X-Hub-Token: $TOKEN" -w '\n%{http_code}' "$BASE$path"
  else
    curl -sS --max-time 10 -w '\n%{http_code}' "$BASE$path"
  fi
}
post() { curl -sS --max-time 10 -X POST -H 'Content-Type: application/json' -H "X-Hub-Token: $TOKEN" --data "$2" -w '\n%{http_code}' "$BASE$1"; }
split() { CODE=$(printf '%s\n' "$1" | tail -n 1); BODY=$(printf '%s\n' "$1" | sed '$d'); }
jget() { printf '%s' "$BODY" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1],{"d":d}))' "$1" 2>/dev/null; }

split "$(get /api/messages no)"; check_eq "T1 无 token ⇒ 401" "401" "$CODE"
split "$(get "/api/messages?token=$TOKEN")"; check_eq "T2 查询参数 token 可用" "200" "$CODE"
split "$(get / no)"; check_eq "T3 页面无 token ⇒ 401" "401" "$CODE"
split "$(get "/?token=$TOKEN")"; check_eq "T4 页面带 token 200" "200" "$CODE"
check_eq "T5 页面含列表与灯箱、无发消息框" "True" "$(printf '%s' "$BODY" | grep -q 'id="list"' && printf '%s' "$BODY" | grep -q 'id="lb"' && ! printf '%s' "$BODY" | grep -q 'id="tin"' && echo True || echo False)"

split "$(post /api/messages '{"text":"手机打的一条"}')"
check_eq "T6 发消息 201" "201" "$CODE"
MID=$(jget 'd["id"]')
check_eq "T7 手机消息 channel=phone" "phone" "$(python3 - "$TMP/root/hub.db" "$MID" <<'PY'
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT channel FROM deliveries WHERE id=?", (sys.argv[2],)).fetchone()[0])
PY
)"
split "$(post /api/messages '{"text":"带附件","attachments":["media/a.png","media/说明.md"]}')"
AID=$(jget 'd["id"]')
split "$(get /api/messages?limit=5)"
check_eq "T8 列表含两条新建" "True" "$(jget "len([m for m in d['messages'] if m['id'] in ('$MID','$AID')])==2")"
check_eq "T9 附件解析成数组" "True" "$(jget "isinstance(next(m for m in d['messages'] if m['id']=='$AID')['attachments'], list)")"

split "$(post /api/messages/archive "{\"id\":\"$MID\"}")"
check_eq "T10 归档 200" "200" "$CODE"
split "$(get /api/messages?limit=5)"
check_eq "T11 归档后默认列表不含" "False" "$(jget "any(m['id']=='$MID' for m in d['messages'])")"
split "$(get /api/messages?archived=1&limit=5)"
check_eq "T12 archived=1 含归档行" "True" "$(jget "any(m['id']=='$MID' for m in d['messages'])")"
split "$(post /api/messages/archive '{"id":"d-nope"}')"; check_eq "T13 归档未知 id ⇒ 404" "404" "$CODE"

VID=$(python3 -c 'import base64; print(base64.urlsafe_b64encode(b"media/a.png").decode().rstrip("="))')
split "$(get "/thumbs/$VID")"; check_eq "T14 缩略图 200" "200" "$CODE"
split "$(get "/asset/$VID")"; check_eq "T15 原图 200" "200" "$CODE"
DID=$(python3 -c 'import base64; print(base64.urlsafe_b64encode("media/说明.md".encode()).decode().rstrip("="))')
split "$(get "/asset/$DID")"; check_eq "T16 文档可取" "200" "$CODE"
split "$(get "/asset/$(python3 -c 'import base64;print(base64.urlsafe_b64encode(b"../etc/passwd").decode().rstrip("="))')")"
check_eq "T17 越界 id ⇒ 403" "403" "$CODE"

split "$(post /api/messages '{"text":"  "}')"; check_eq "T18 空文本 400" "400" "$CODE"
split "$(get '/api/messages?archived=9')"; check_eq "T19 archived=9 400" "400" "$CODE"
split "$(post /api/messages '{"text":"x","attachments":["/etc/passwd"]}')"; check_eq "T20 绝对路径附件 400" "400" "$CODE"
split "$(get /api/nope)"; check_eq "T21 未知路由 404" "404" "$CODE"

hget() { curl -sS --max-time 10 -D - -o /dev/null -H "X-Hub-Token: $TOKEN" "$BASE$1"; }
split "$(get "/?token=$TOKEN")"
check_eq "T22 页面含图片灯箱与去重守卫" "True" "$(printf '%s' "$BODY" | grep -q 'id="lb"' && printf '%s' "$BODY" | grep -q 'm.body !== m.title' && echo True || echo False)"
check_eq "T23 文档下载带原始文件名" "True" "$(hget "/asset/$DID" | grep -qi "content-disposition:.*filename\*=UTF-8''%E8%AF%B4%E6%98%8E.md" && echo True || echo False)"
check_eq "T24 图片不强制下载" "False" "$(hget "/asset/$VID" | grep -qi 'content-disposition:' && echo True || echo False)"

# ---- 提问应答（§11.10：选项 + 打字 + 提交/跳过）----
QID=$(bash "$HUB/bin/hub-deliver" add --channel dsh --title "测试提问" --mode question \
      --options '[{"key":"a","label":"选项甲"}]' 2>/dev/null)
split "$(get /api/messages?limit=20)"
check_eq "T25 提问行带选项" "True" "$(jget "next((m['options'][0]['key'] for m in d['messages'] if m['id']=='$QID'), None)=='a'")"
split "$(post /api/deliver/reply "{\"id\":\"$QID\",\"text\":\"选项甲\"}")"
check_eq "T26 提交回复 200" "200" "$CODE"
check_eq "T27 回复落库 replied" "replied" "$(jget 'd["status"]')"
check_eq "T28 回复原文留档" "选项甲" "$(jget 'd["reply"]')"
QID2=$(bash "$HUB/bin/hub-deliver" add --channel dsh --title "测试提问2" --mode question 2>/dev/null)
split "$(post /api/deliver/skip "{\"id\":\"$QID2\"}")"
check_eq "T29 跳过 ⇒ expired" "expired" "$(jget 'd["status"]')"
split "$(post /api/deliver/skip '{"id":"d-nope"}')"; check_eq "T30 跳过未知 id ⇒ 404" "404" "$CODE"
split "$(post /api/deliver/reply "{\"id\":\"$QID2\",\"text\":\"  \"}")"; check_eq "T31 空回复 ⇒ 400" "400" "$CODE"
split "$(get "/?token=$TOKEN")"
check_eq "T32 页面含回复渲染" "True" "$(printf '%s' "$BODY" | grep -q '回复：' && printf '%s' "$BODY" | grep -q 'className = "ans"' && echo True || echo False)"
RESP=$(get "/api/messages?limit=80")
check_eq "T33 已答行带 reply 字段" "True" "$(printf '%s' "$RESP" | grep -q '"reply"' && echo True || echo False)"

# ---- 只有 token 一道门（用户 2026-11-17 22:56 定：有 token 就不需要密码，密码整套删掉）----
split "$(get /api/settings)"
check_eq "T34 /api/settings 只验 token" "True" "$(jget 'd["token_set"] is True and d["port"] > 0')"
split "$(get "/?token=$TOKEN&helper=1")"
check_eq "T35 页面不再有红横幅 / 密码字段" "True" "$(printf '%s' "$BODY" | grep -q 'id="pubbanner"' && echo False || { printf '%s' "$BODY" | grep -q 'PWSET' && echo False || { printf '%s' "$BODY" | grep -q 'X-Hub-Password' && echo False || { printf '%s' "$BODY" | grep -q 'hub://settings' && echo True || echo False; }; }; })"
split "$(get /api/settings no)"; check_eq "T36 /api/settings 无 token ⇒ 401" "401" "$CODE"
split "$(post /api/settings/password '{"password":"whatever"}')"
check_eq "T37 设密码路由已删 ⇒ 404" "404" "$CODE"
split "$(get /api/messages?limit=1)"; check_eq "T38 只带 token ⇒ 200" "200" "$CODE"
split "$(get /api/messages?limit=1 no)"; check_eq "T39 不带 token ⇒ 401" "401" "$CODE"
check_eq "T40 401 不再要求密码" "True" "$(jget 'd.get("password_required") is None and d.get("error") == "bad-token"')"
split "$(get "/?token=$TOKEN")"; check_eq "T41 页面只需 token" "200" "$CODE"
# 页面内联 JS 必须能解析——删密码时漏了分号曾让整个脚本语法错误、消息页一片空白且无报错（2026-11-17 踩坑）
printf '%s' "$BODY" | python3 -c 'import re,sys; h=sys.stdin.read(); print("\n".join(re.findall(r"<script[^>]*>(.*?)</script>", h, re.S)))' > "$TMP/page.js"
if command -v node >/dev/null 2>&1; then
  node --check "$TMP/page.js" >/dev/null 2>&1 && JSOK=True || JSOK=False
else
  JSOK=skip
fi
check_eq "T42 页面 JS 语法可解析" "True" "$JSOK"

# ---- token 自动生成（配置留空 ⇒ $HUB_ROOT/phone_api.token，0600）----
TF="$TMP/root/phone_api.token"
check_eq "T43 配置留空 ⇒ 生成 token 文件" "yes" "$([ -s "$TF" ] && echo yes || echo no)"
check_eq "T44 token 文件 0600" "600" "$(stat -c %a "$TF" 2>/dev/null)"
check_eq "T45 生成值被接受（401/200 由已有用例覆盖）" "yes" "$(grep -qxF "$TOKEN" "$TF" && echo yes || echo no)"

printf '\n%d 通过 / %d 失败\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && { echo "ALL PASS"; exit 0; } || exit 1
