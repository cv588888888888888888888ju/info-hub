#!/usr/bin/env bash
# info-hub M3 验收：报表层（自动区块 + 人改留痕 + 与系统数据分离）与本地控制台。
# 判据见 PROJECT.md §9 / INTERFACE.md §4.7、§2.5、§2.6。全程离线、不弹窗。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
REPORT=(python3 -m infohub.entry_report)
STORE=(python3 -m infohub.entry_store)
SEED="$HERE/m3_seed.py"

TMP="$(mktemp -d /tmp/infohub-m3-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

HUB_ROOT="$TMP/root"
mkdir -p "$HUB_ROOT"
CFG="$TMP/hub.config.yaml"
python3 - "$ROOT/hub.config.yaml" "$CFG" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["notify"]["enabled"] = False
raw["board"]["enabled"] = False
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
export HUB_CONFIG="$CFG" HUB_ROOT

RPT="$HUB_ROOT/reports/hub-report.md"
hub_report() { "${REPORT[@]}" --config "$CFG" "$@"; }
hub_store()  { "${STORE[@]}" --config "$CFG" "$@"; }
revs()       { hub_report --json revisions | json_get "len(d)"; }

seed() { HUB_ROOT="$HUB_ROOT" HUB_CONFIG="$CFG" python3 "$SEED" "$1" "$2" "$3" >/dev/null; }

# ---- T1 生成：建文件 + 三个自动区块 ------------------------------------------
seed 3 2 1
gen1="$(hub_report --json generate)"
[ "$(echo "$gen1" | json_get "d['changed']")" = "True" ] \
  && [ "$(echo "$gen1" | json_get "d['rev']")" = "1" ] \
  && [ -f "$RPT" ] && pass "T1 generate 建报表（rev=1）" || fail "T1 generate" "$gen1"
blocks=0
for b in stats todos notifications; do grep -q "hub:auto:begin $b" "$RPT" && blocks=$((blocks+1)); done
[ "$blocks" = 3 ] && pass "T1b 三个自动区块都在" || fail "T1b 自动区块" "blocks=$blocks"

# ---- T2 报表读的是系统数据 ----------------------------------------------------
grep -q "合成待办 M3-" "$RPT" && grep -q "事件总数：" "$RPT" && grep -q "M3 种子通知" "$RPT" \
  && pass "T2 自动区块含系统数据（待办/事件/通知）" || fail "T2 系统数据未进报表"

# ---- T3 幂等：没变化不写、不留痕 ----------------------------------------------
gen2="$(hub_report --json generate)"
[ "$(echo "$gen2" | json_get "d['changed']")" = "False" ] && [ "$(revs)" = "1" ] \
  && pass "T3 重复 generate 幂等（changed=False，修订仍 1）" || fail "T3 幂等" "$gen2 revs=$(revs)"

# ---- T4 人手改（自由段）+ 留痕 ------------------------------------------------
hub_report --json write --append $'## 我的手记\n今天看论文 2410.12361（Proactive Agent）' --reason 手记 >/dev/null
[ "$(revs)" = "2" ] && grep -q "我的手记" "$RPT" \
  && pass "T4 人改自由段并留痕（rev=2）" || fail "T4 人改" "revs=$(revs)"

# ---- T5 系统数据变了：再生成只重写自动区块，人的文字原样保留 -------------------
seed 1 1 0
gen3="$(hub_report --json generate)"
ok1="no"; grep -q "我的手记" "$RPT" && grep -q "Proactive Agent" "$RPT" && ok1="yes"
ok2="no"; grep -q "合成待办 M3-" "$RPT" && ok2="yes"
[ "$(echo "$gen3" | json_get "d['changed']")" = "True" ] && [ "$ok1" = yes ] && [ "$ok2" = yes ] \
  && [ "$(echo "$gen3" | json_get "d['author']")" = "hub" ] && [ "$(revs)" = "3" ] \
  && pass "T5 再生成：自动区块重写、人的文字不动（rev=3）" || fail "T5 再生成" "$gen3 ok1=$ok1 ok2=$ok2"

# ---- T6 修订序列作者分明 ------------------------------------------------------
authors="$(hub_report --json revisions | json_get "','.join(r['author'] for r in d)")"
[ "$authors" = "hub,human,hub" ] && pass "T6 修订作者序列 hub,human,hub" || fail "T6 作者序列" "$authors"

# ---- T7 diff 可查 --------------------------------------------------------------
hub_report diff --rev 2 | grep -q "我的手记" && pass "T7 diff --rev 2 含人改内容" || fail "T7 diff"

# ---- T8 报表写入不碰系统数据 ---------------------------------------------------
tot_before="$(hub_store stats --json | json_get "d['events_total']")"
todos_before="$(python3 - "$HUB_ROOT/hub.db" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM todos").fetchone()[0])
PY
)"
hub_report --json write --text "$(cat "$RPT")"$'\n\n一句话脚注。\n' --reason 脚注 >/dev/null
tot_after="$(hub_store stats --json | json_get "d['events_total']")"
todos_after="$(python3 - "$HUB_ROOT/hub.db" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM todos").fetchone()[0])
PY
)"
[ "$tot_before" = "$tot_after" ] && [ "$todos_before" = "$todos_after" ] \
  && pass "T8 改报表不动系统数据（events/todos 不变）" \
  || fail "T8 分离" "events $tot_before→$tot_after todos $todos_before→$todos_after"

# ---- T9 报表名逃逸被拒 ---------------------------------------------------------
out="$(hub_report --report ../evil show 2>&1)"; rc=$?
[ "$rc" = "1" ] && echo "$out" | grep -q "报表名非法" \
  && pass "T9 报表名逃逸被拒（../evil）" || fail "T9 逃逸" "rc=$rc $out"

# ---- T10~T13 本地控制台 --------------------------------------------------------
UI_OUT="$TMP/ui.out"
python3 -m infohub.entry_ui --config "$CFG" --port 0 >"$UI_OUT" 2>"$TMP/ui.err" &
UI_PID=$!
for _ in $(seq 1 60); do grep -q '"ui"' "$UI_OUT" 2>/dev/null && break; sleep 0.1; done
PORT="$(json_get "d['port']" <"$UI_OUT")"
if [ -z "${PORT:-}" ]; then
  fail "T10 UI 未起来" "$(cat "$TMP/ui.err" 2>/dev/null)"
else
  base="http://127.0.0.1:$PORT"
  state="$(curl -s "$base/api/state")"
  [ "$(echo "$state" | json_get "'text' in d['report']")" = "True" ] \
    && [ "$(echo "$state" | json_get "d['system']['stats']['events_total']")" = "$tot_after" ] \
    && pass "T10 UI /api/state 只读快照可用" || fail "T10 /api/state" "$(echo "$state" | head -c 200)"

  newtext="$(python3 - "$RPT" <<'PY'
import json, sys
print(json.dumps({"report": "hub-report",
                  "text": open(sys.argv[1], encoding="utf-8").read() + "\n\nUI 手改：记一笔。\n",
                  "reason": "ui"}, ensure_ascii=False))
PY
)"
  saved="$(curl -s -X POST -H 'Content-Type: application/json' -d "$newtext" "$base/api/save")"
  [ "$(echo "$saved" | json_get "d['changed']")" = "True" ] \
    && [ "$(echo "$saved" | json_get "d['author']")" = "human" ] \
    && grep -q "UI 手改：记一笔" "$RPT" \
    && pass "T11 UI 保存报表并留痕（author=human）" || fail "T11 UI 保存" "$saved"

  gen_ui="$(curl -s -X POST -H 'Content-Type: application/json' \
      -d '{"report":"hub-report"}' "$base/api/generate")"
  [ "$(echo "$gen_ui" | json_get "d['author']")" = "hub" ] \
    && pass "T12 UI 可触发重写自动区块（author=hub）" || fail "T12 UI generate" "$gen_ui"

  code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "$base/api/state")"
  bad="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
      -d '{}' "$base/api/nope")"
  [ "$code" = "403" ] && [ "$bad" = "404" ] \
    && pass "T13 UI Host 守卫 403 + 未知路由 404" || fail "T13 UI 守卫" "host=$code route=$bad"

  listen="$(ss -ltn 2>/dev/null | grep ":$PORT " || true)"
  echo "$listen" | grep -q "127.0.0.1:$PORT" && ! echo "$listen" | grep -q "0.0.0.0:$PORT" \
    && pass "T14 UI 只绑 loopback（无 0.0.0.0）" || fail "T14 绑定" "$listen"
fi
kill "$UI_PID" 2>/dev/null; wait "$UI_PID" 2>/dev/null

# ---- T15 UI 保存后系统数据仍不被改 --------------------------------------------
tot_ui="$(hub_store stats --json | json_get "d['events_total']")"
todos_ui="$(python3 - "$HUB_ROOT/hub.db" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM todos").fetchone()[0])
PY
)"
[ "$tot_before" = "$tot_ui" ] && [ "$todos_before" = "$todos_ui" ] \
  && pass "T15 UI 走一圈后系统数据仍未变" || fail "T15 UI 分离" "$tot_before→$tot_ui / $todos_before→$todos_ui"

# ---- 汇总 ---------------------------------------------------------------------
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
