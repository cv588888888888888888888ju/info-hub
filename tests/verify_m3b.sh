#!/usr/bin/env bash
# info-hub M3b 验收：清单也能在控制台上手改，且**每次改动留痕**（与报表的改动记录对称）。
# 判据见 INTERFACE.md §2.6 / §5.7、PROJECT.md §28。全程离线、不弹窗、不碰看板。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
STORE=(python3 -m infohub.entry_store)
RULES=(python3 -m infohub.entry_rules)
SEED="$HERE/m2_seed.py"

TMP="$(mktemp -d /tmp/infohub-m3b-XXXXXX)"
trap 'rm -rf "$TMP"; [ -n "${UI_PID:-}" ] && kill "$UI_PID" 2>/dev/null' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

HUB_ROOT="$TMP/root"; mkdir -p "$HUB_ROOT"
CFG="$TMP/hub.config.yaml"
python3 - "$ROOT/hub.config.yaml" "$CFG" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["notify"]["enabled"] = False
raw["board"]["enabled"] = False
raw["ui"]["todo_revisions_limit"] = 8
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
export HUB_CONFIG="$CFG" HUB_ROOT

hub_store() { "${STORE[@]}" --config "$CFG" "$@"; }
sqlq() {
  HUB_ROOT="$HUB_ROOT" HUB_CONFIG="$CFG" python3 - "$1" <<'PY'
import sys
from infohub.config import load
from infohub.store import Store
c = load(); s = Store(c.db_path)
print(eval(sys.argv[1]))
s.conn.commit()
PY
}

# 两条待办（fs-repeat），id 由种子的幂等键决定
HUB_ROOT="$HUB_ROOT" HUB_CONFIG="$CFG" python3 "$SEED" 2 1 fs-repeat >/dev/null
T1ID="$(sqlq "[r['id'] for r in s.todos(limit=50)][1]" | tail -1)"
T2ID="$(sqlq "[r['id'] for r in s.todos(limit=50)][0]" | tail -1)"
REV_BEFORE="$(sqlq "len(s.todo_revisions(limit=999))" | tail -1)"

# ---- T1 控制台快照带上「清单改动记录」空表 ------------------------------------
UI_OUT="$TMP/ui.out"
python3 -m infohub.entry_ui --config "$CFG" --port 0 >"$UI_OUT" 2>"$TMP/ui.err" &
UI_PID=$!
for _ in $(seq 1 60); do grep -q '"ui"' "$UI_OUT" 2>/dev/null && break; sleep 0.1; done
PORT="$(json_get "d['port']" <"$UI_OUT")"
if [ -z "${PORT:-}" ]; then
  fail "T1 控制台未起来" "$(cat "$TMP/ui.err" 2>/dev/null)"
  printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"; exit 1
fi
base="http://127.0.0.1:$PORT"
state="$(curl -s "$base/api/state")"
[ "$(echo "$state" | json_get "d['system']['todo_revisions']")" = "[]" ] \
  && [ "$(echo "$state" | json_get "len(d['system']['todos'])")" = "2" ] \
  && pass "T1 /api/state 带上待办与改动记录（初始为空）" \
  || fail "T1 快照" "$(echo "$state" | head -c 200)"

# ---- T2 在控制台上把一条待办标完成 --------------------------------------------
out2="$(curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"id\":\"$T1ID\",\"status\":\"done\",\"reason\":\"ui\"}" "$base/api/todo")"
[ "$(echo "$out2" | json_get "d['ok']")" = "True" ] \
  && [ "$(echo "$out2" | json_get "d['todo']['status']")" = "done" ] \
  && [ "$(echo "$out2" | json_get "d['todo']['human_override']")" = "1" ] \
  && pass "T2 控制台改状态（done + human_override=1）" || fail "T2 改状态" "$out2"

# ---- T3 改动留痕：author=human、before→after、reason=ui ------------------------
rev3="$(curl -s "$base/api/todo/revisions?id=$T1ID")"
[ "$(echo "$rev3" | json_get "len(d['revisions'])")" = "1" ] \
  && [ "$(echo "$rev3" | json_get "d['revisions'][0]['author']")" = "human" ] \
  && [ "$(echo "$rev3" | json_get "d['revisions'][0]['field']")" = "status" ] \
  && [ "$(echo "$rev3" | json_get "d['revisions'][0]['before']")" = "open" ] \
  && [ "$(echo "$rev3" | json_get "d['revisions'][0]['after']")" = "done" ] \
  && [ "$(echo "$rev3" | json_get "d['revisions'][0]['reason']")" = "ui" ] \
  && pass "T3 改动留痕（human / status / open→done / ui）" || fail "T3 留痕" "$rev3"

# ---- T4 规则引擎不许把人改的翻回去 --------------------------------------------
"${RULES[@]}" --config "$CFG" --json derive >/dev/null 2>&1
ST4="$(sqlq "[r['status'] for r in s.todos(limit=50) if r['id']=='$T1ID'][0]" | tail -1)"
N4="$(sqlq "len([r for r in s.todo_revisions(limit=99) if r['todo_id']=='$T1ID'])" | tail -1)"
[ "$ST4" = "done" ] && [ "$N4" = "1" ] \
  && pass "T4 跑完规则仍是 done（人改不被翻回，也没多写记录）" || fail "T4 规则不翻回" "status=$ST4 revs=$N4"

# ---- T5 「稍后」：推迟 + 状态不变 + 另记一条留痕 -------------------------------
out5="$(curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"id\":\"$T2ID\",\"snooze_minutes\":60,\"reason\":\"ui\"}" "$base/api/todo")"
sn5="$(sqlq "[r['snooze_until'] for r in s.todos(limit=50) if r['id']=='$T2ID'][0]" | tail -1)"
f5="$(sqlq "[r['field'] for r in s.todo_revisions(todo_id='$T2ID', limit=5)][0]" | tail -1)"
[ "$(echo "$out5" | json_get "d['todo']['status']")" = "open" ] \
  && [ -n "$sn5" ] && [ "$sn5" != "None" ] && [ "$f5" = "snooze_until" ] \
  && pass "T5 「稍后」：snooze_until=$sn5、状态仍 open、留痕 field=snooze_until" || fail "T5 稍后" "$out5 / $sn5 / $f5"

# ---- T6 参数校验：非法状态 400、未知 id 404、越界 400 -------------------------
c6a="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "{\"id\":\"$T2ID\",\"status\":\"whatever\"}" "$base/api/todo")"
c6b="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d '{"id":"t-不存在","status":"done"}' "$base/api/todo")"
c6c="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "{\"id\":\"$T2ID\",\"snooze_minutes\":999999}" "$base/api/todo")"
[ "$c6a" = "400" ] && [ "$c6b" = "404" ] && [ "$c6c" = "400" ] \
  && pass "T6 参数校验（非法状态 400 / 未知 id 404 / 越界 400）" || fail "T6 校验" "$c6a $c6b $c6c"

# ---- T7 Host 守卫对写入口同样生效 ---------------------------------------------
c7="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Host: evil.example' \
  -H 'Content-Type: application/json' -d "{\"id\":\"$T2ID\",\"status\":\"done\"}" "$base/api/todo")"
[ "$c7" = "403" ] && pass "T7 写入口也有 Host 守卫（403）" || fail "T7 守卫" "code=$c7"

# ---- T8 CLI 改动同样留痕（reason=cli） ----------------------------------------
hub_store todo-set "$T2ID" dropped >/dev/null
row8="$(hub_store todo-revisions "$T2ID" --json | json_get "[(r['author'], r['before'], r['after'], r['reason']) for r in d][0]")"
[ "$row8" = "('human', 'open', 'dropped', 'cli')" ] \
  && pass "T8 CLI 改动也留痕（human / open→dropped / cli）" || fail "T8 CLI 留痕" "$row8"

# ---- T9 清单改动不写报表的改动记录（两张账本分离） ---------------------------
NR9="$(sqlq "s.conn.execute('SELECT COUNT(*) FROM report_revisions').fetchone()[0]" | tail -1)"
TOT9="$(sqlq "len(s.todo_revisions(limit=99))" | tail -1)"
[ "$NR9" = "0" ] && [ "$TOT9" -ge 3 ] \
  && pass "T9 清单留痕进 todo_revisions（$TOT9 条），报表账本仍是 0（分离）" \
  || fail "T9 分离" "report=$NR9 todo=$TOT9（改动前 $REV_BEFORE）"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
