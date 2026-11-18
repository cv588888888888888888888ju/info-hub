#!/usr/bin/env bash
# S19 验收：控制台「媒体」面板（契约 plugins/views/ui/README-ASSETS.md）。
# 隔离：临时根（只改配置里的 root: 行）+ 临时端口（内核分配）；不碰真库、不动常驻服务；可复跑。
# 断言面：列表形状/排序/分页、图片与文档取字节（Content-Type + 字节一致）、
#   ../ 403、绝对路径 403、软链逃逸 403、允许根外 403、不存在 404、错误体不回显绝对路径、页面含媒体容器。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-s19-assets-XXXXXX)
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
check_eq "隔离根写进临时配置（不碰真库）" "root: $TMP/root" "$(grep -m1 '^root:' "$TMP/c.yaml")"

# ---- 夹具：两个允许根（screens/reports）+ 根内但不允许的文件 + 逃逸软链 ----
ROOT="$TMP/root"
mkdir -p "$ROOT/screens/2026-11-14" "$ROOT/screens/2026-11-13" "$ROOT/reports/sub"
printf '%s' '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAQABgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDl6KKK8c/RwooooA//2Q==' | base64 -d > "$ROOT/screens/2026-11-14/a.jpg"
printf '%s' 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAQCAIAAACDRijCAAAAHElEQVR4nGPUqDjBQA3ARBVTRg0aNWjUoBFsEAC0+gGIF3tLpwAAAABJRU5ErkJggg==' | base64 -d > "$ROOT/screens/2026-11-14/b.png"
cp "$ROOT/screens/2026-11-14/a.jpg" "$ROOT/screens/2026-11-13/older.jpg"
cp "$ROOT/screens/2026-11-14/b.png" "$ROOT/screens/.hidden.png"
printf '# 报表夹具\n\n这是验收用的文档。\n' > "$ROOT/reports/note.md"
printf '深层文档\n' > "$ROOT/reports/sub/deep.txt"
printf '这个文件在数据根里，但不在任何允许根里\n' > "$ROOT/secret.txt"
ln -s /etc/passwd "$ROOT/screens/escape.txt"
touch -d '2026-11-14 10:00:00' "$ROOT/screens/2026-11-14/a.jpg"
touch -d '2026-11-14 11:00:00' "$ROOT/screens/2026-11-14/b.png"
touch -d '2026-11-13 08:00:00' "$ROOT/screens/2026-11-13/older.jpg"
touch -d '2026-11-14 09:30:00' "$ROOT/reports/note.md"
touch -d '2026-11-13 07:00:00' "$ROOT/reports/sub/deep.txt"
check_eq "夹具：3 图 2 文档 + 1 根外文件 + 1 逃逸软链" "True" \
  "$( [ -f "$ROOT/screens/2026-11-14/a.jpg" ] && [ -f "$ROOT/reports/note.md" ] && [ -e "$ROOT/screens/escape.txt" ] && echo True || echo False )"

# ---- HTTP 小工具（与 check_ui_deliver.sh 同风格）--------------------------
capture() { # capture <path>
  local out
  out=$(curl -sS --max-time 10 -w '\n%{http_code}' "$BASE$1")
  HTTP_CODE=$(printf '%s\n' "$out" | tail -n 1)
  HTTP_BODY=$(printf '%s\n' "$out" | sed '$d')
}
jget() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1" 2>/dev/null; }
body_get() { printf '%s' "$HTTP_BODY" | jget "$1"; }
b64id() { python3 -c 'import base64,sys; print(base64.urlsafe_b64encode(sys.argv[1].encode()).decode().rstrip("="))' "$1"; }
ctype_of() { grep -i '^content-type:' "$1" | head -1 | tr -d '\r' | sed 's/^[^:]*:[[:space:]]*//'; }
code_of() { head -1 "$1" | awk '{print $2}'; }

# ---- 起临时控制台（端口 0，内核分配）-------------------------------------
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
echo "临时控制台: $BASE（HUB_ROOT=$ROOT）"

echo
echo "== A19-1：GET /api/assets 形状与排序 =="
capture "/api/assets?kind=image&limit=50"
check_eq "A19 媒体列表 200" 200 "$HTTP_CODE"
check_eq "A19 列表形状 {\"items\":[...]}" "True" "$(body_get 'isinstance(d.get("items"), list)')"
check_eq "A19 每项含 id/kind/name/size/mtime/url" "True" \
  "$(body_get 'all({"id","kind","name","size","mtime","url"} <= set(x) for x in d["items"])')"
check_eq "A19 kind=image 只返回 image" "True" "$(body_get 'all(x["kind"] == "image" for x in d["items"])')"
check_eq "A19 三张图片都列出（隐藏文件不算）" "3" "$(body_get 'len(d["items"])')"
check_eq "A19 image 项有 /asset/ url 与 /thumbs/ 缩略图" "True" \
  "$(body_get 'all(x["url"].startswith("/asset/") and x.get("thumb","").startswith("/thumbs/") for x in d["items"])')"
check_eq "A19 image 项 mtime 是 ISO8601（含 T）" "True" "$(body_get 'all("T" in x["mtime"] for x in d["items"])')"
printf '%s' "$HTTP_BODY" > "$TMP/list-image.json"
check_eq "A19 列表响应不含绝对路径" "False" "$(printf '%s' "$HTTP_BODY" | grep -qF "$ROOT" && echo True || echo False)"

capture "/api/assets?kind=all&limit=50"
check_eq "A19 全量按 mtime 倒序" "True" \
  "$(body_get 'all(d["items"][i]["mtime"] >= d["items"][i+1]["mtime"] for i in range(len(d["items"])-1))')"
check_eq "A19 全量 5 项（3 图 + 2 文档）" "5" "$(body_get 'len(d["items"])')"

echo
echo "== A19-2：文档列表 =="
capture "/api/assets?kind=doc&limit=50"
check_eq "A19 文档列表 200" 200 "$HTTP_CODE"
check_eq "A19 kind=doc 只返回 doc" "True" "$(body_get 'all(x["kind"] == "doc" for x in d["items"])')"
check_eq "A19 文档含 note.md 与 sub/deep.txt" "True" \
  "$(body_get '{"note.md", "sub/deep.txt"} <= {x["name"] for x in d["items"]}')"
check_eq "A19 文档项有 size 与 mtime" "True" \
  "$(body_get 'all(isinstance(x["size"], int) and x["size"] > 0 and "T" in x["mtime"] for x in d["items"])')"
check_eq "A19 允许根外的 secret.txt 不列出" "False" "$(body_get 'any(x["name"] == "secret.txt" for x in d["items"])')"
check_eq "A19 逃逸软链 escape.txt 不列出" "False" "$(body_get 'any("escape.txt" in x["name"] for x in d["items"])')"
check_eq "A19 隐藏文件不列出" "False" "$(body_get 'any("/." in ("/" + x["name"]) for x in d["items"])')"
printf '%s' "$HTTP_BODY" > "$TMP/list-doc.json"

echo
echo "== A19-3：分页（cursor） =="
capture "/api/assets?kind=all&limit=2"
printf '%s' "$HTTP_BODY" > "$TMP/p1.json"
check_eq "A19 第一页 2 项" "2" "$(body_get 'len(d["items"])')"
CUR=$(body_get 'd.get("nextCursor", "")')
check_eq "A19 第一页给 nextCursor" "True" "$( [ -n "$CUR" ] && echo True || echo False )"
capture "/api/assets?kind=all&limit=2&cursor=$CUR"
printf '%s' "$HTTP_BODY" > "$TMP/p2.json"
check_eq "A19 第二页 2 项" "2" "$(body_get 'len(d["items"])')"
check_eq "A19 两页不重叠" "True" \
  "$(python3 - "$TMP/p1.json" "$TMP/p2.json" <<'PY'
import json, sys
a = {x["id"] for x in json.load(open(sys.argv[1]))["items"]}
b = {x["id"] for x in json.load(open(sys.argv[2]))["items"]}
print(len(a & b) == 0)
PY
)"
capture "/api/assets?kind=all&limit=0"
check_eq "A19 limit=0 ⇒ 400" 400 "$HTTP_CODE"
capture "/api/assets?kind=video"
check_eq "A19 kind=video ⇒ 400" 400 "$HTTP_CODE"
capture "/api/assets?kind=all&cursor=bad_cursor_value"
check_eq "A19 坏 cursor ⇒ 400" 400 "$HTTP_CODE"

echo
echo "== A19-4：取字节（Content-Type / 字节一致） =="
IMG_ID=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(x["id"] for x in d["items"] if x["name"].endswith("a.jpg")))' "$TMP/list-image.json")
DOC_ID=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(x["id"] for x in d["items"] if x["name"] == "note.md"))' "$TMP/list-doc.json")
check_eq "A19 从列表取到图片 id" "True" "$( [ -n "$IMG_ID" ] && echo True || echo False )"

curl -sS --max-time 10 -D "$TMP/img.h" -o "$TMP/img.bin" "$BASE/asset/$IMG_ID"
check_eq "A19 取图片 200" 200 "$(code_of "$TMP/img.h")"
check_eq "A19 图片 Content-Type=image/jpeg" "image/jpeg" "$(ctype_of "$TMP/img.h")"
check_contains "A19 图片 Content-Disposition 是 inline" "inline" "$TMP/img.h"
check_eq "A19 图片字节与夹具一致" "$(sha256sum "$ROOT/screens/2026-11-14/a.jpg" | cut -d' ' -f1)" \
  "$(sha256sum "$TMP/img.bin" | cut -d' ' -f1)"
check_eq "A19 图片响应头不回显绝对路径" "False" "$(grep -qF "$ROOT" "$TMP/img.h" && echo True || echo False)"

curl -sS --max-time 10 -D "$TMP/doc.h" -o "$TMP/doc.bin" "$BASE/asset/$DOC_ID"
check_eq "A19 取文档 200" 200 "$(code_of "$TMP/doc.h")"
check_eq "A19 文档 Content-Type=text/plain" "text/plain; charset=utf-8" "$(ctype_of "$TMP/doc.h")"
check_contains "A19 文档 Content-Disposition 是 attachment" "attachment" "$TMP/doc.h"
check_eq "A19 文档字节与夹具一致" "$(sha256sum "$ROOT/reports/note.md" | cut -d' ' -f1)" \
  "$(sha256sum "$TMP/doc.bin" | cut -d' ' -f1)"

curl -sS --max-time 10 -D "$TMP/thumb.h" -o "$TMP/thumb.bin" "$BASE/thumbs/$IMG_ID"
check_eq "A19 缩略图 200" 200 "$(code_of "$TMP/thumb.h")"
check_eq "A19 缩略图 Content-Type=image/jpeg" "image/jpeg" "$(ctype_of "$TMP/thumb.h")"
check_eq "A19 缩略图非空" "True" "$( [ -s "$TMP/thumb.bin" ] && echo True || echo False )"
curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$BASE/thumbs/$DOC_ID" > "$TMP/thumb-doc.code"
check_eq "A19 文档没有缩略图 ⇒ 404" 404 "$(cat "$TMP/thumb-doc.code")"

echo
echo "== A19-5：越界与不存在（403/404 分界，不泄路径） =="
TRAV=$(b64id "../secret.txt")
capture "/asset/$TRAV"
check_eq "A19 ../ 越界 ⇒ 403" 403 "$HTTP_CODE"
check_eq "A19 越界错误体只有稳定错误码" "bad-id" "$(body_get 'd.get("error")')"
check_eq "A19 越界错误体不回显绝对路径" "False" "$(printf '%s' "$HTTP_BODY" | grep -qF "$ROOT" && echo True || echo False)"

TRAV2=$(b64id "screens/../../etc/passwd")
capture "/asset/$TRAV2"
check_eq "A19 screens/../../ 越界 ⇒ 403" 403 "$HTTP_CODE"

ABS=$(b64id "/etc/passwd")
capture "/asset/$ABS"
check_eq "A19 绝对路径 id ⇒ 403" 403 "$HTTP_CODE"

OUT=$(b64id "secret.txt")
capture "/asset/$OUT"
check_eq "A19 根内但在允许根外 ⇒ 403" 403 "$HTTP_CODE"
check_eq "A19 根外错误码是 outside-root" "outside-root" "$(body_get 'd.get("error")')"

LINK=$(b64id "screens/escape.txt")
capture "/asset/$LINK"
check_eq "A19 软链逃逸 ⇒ 403" 403 "$HTTP_CODE"
check_eq "A19 软链逃逸错误码是 outside-root" "outside-root" "$(body_get 'd.get("error")')"
check_eq "A19 软链错误体不回显目标路径" "False" \
  "$(printf '%s' "$HTTP_BODY" | grep -qF '/etc/passwd' && echo True || echo False)"

MISS=$(b64id "screens/nope.jpg")
capture "/asset/$MISS"
check_eq "A19 不存在的文件 ⇒ 404" 404 "$HTTP_CODE"
check_eq "A19 不存在错误体是 not-found" "not-found" "$(body_get 'd.get("error")')"

capture "/asset/bad%%id"
check_eq "A19 非法 id 字符 ⇒ 403" 403 "$HTTP_CODE"
capture "/asset/"
check_eq "A19 空 id ⇒ 403" 403 "$HTTP_CODE"

echo
echo "== A19-6：页面 =="
PAGE_CODE=$(curl -sS --max-time 10 -o "$TMP/page.html" -w '%{http_code}' "$BASE/")
check_eq "A19 GET / = 200" 200 "$PAGE_CODE"
check_contains 'A19 页面含图片容器 id="mediagrid"' 'id="mediagrid"' "$TMP/page.html"
check_contains 'A19 页面含文档容器 id="medialist"' 'id="medialist"' "$TMP/page.html"
check_contains 'A19 前端调 /api/assets' '/api/assets' "$TMP/page.html"
check_contains 'A19 前端用 /thumbs/ 缩略图' '/thumbs/' "$TMP/page.html"
check_contains 'A19 既有投递容器仍在（没破坏老布局）' 'id="deliveries"' "$TMP/page.html"

echo
echo "== A19-7：开关（ui.assets.enabled=false） =="
kill "$UI_PID" 2>/dev/null || true
wait "$UI_PID" 2>/dev/null || true
UI_PID=""
python3 - "$TMP/c.yaml" "$TMP/c-off.yaml" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw.setdefault("ui", {}).setdefault("assets", {})["enabled"] = False
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
HUB_CONFIG="$TMP/c-off.yaml" HUB_ROOT="$ROOT" bash "$HUB/bin/hub-ui" --port 0 >"$TMP/ui-off.out" 2>"$TMP/ui-off.err" &
UI_PID=$!
PORT2=""
for _ in $(seq 1 100); do
  if [ -s "$TMP/ui-off.out" ]; then
    PORT2=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$TMP/ui-off.out" 2>/dev/null || true)
    [ -n "$PORT2" ] && break
  fi
  if ! kill -0 "$UI_PID" 2>/dev/null; then break; fi
  sleep 0.05
done
if [ -z "$PORT2" ]; then
  bad "A19 停用媒体后面板能起服务" "$(cat "$TMP/ui-off.err" 2>/dev/null)"
else
  BASE="http://127.0.0.1:$PORT2"
  capture "/api/assets"
  check_eq "A19 停用后 /api/assets ⇒ 404" 404 "$HTTP_CODE"
  check_eq "A19 停用错误码是 assets-disabled" "assets-disabled" "$(body_get 'd.get("error")')"
fi

echo
echo "RESULT pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
