#!/usr/bin/env bash
# info-hub M3c 验收：待办的**证据链**（每条待办能摊开成原始事件）+「无依据不发」真的落在代码里。
# 判据见 INTERFACE.md §2.2 / §2.6 / §4.6（门 8）、§5.8，PROJECT.md §29。全程离线、不弹真窗口。
#
# 与 M2 系列验收的关键差别：**种子造真事件、由规则引擎自己产出待办**（tests/m3c_seed.py），
# 而不是手写 `source_event_ids=[1,2,3]` —— 否则"证据能摊开"这件事根本没被验到（假 id 查不到就是 missing）。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
STORE=(python3 -m infohub.entry_store)
RULES=(python3 -m infohub.entry_rules)
NOTIFY=(python3 -m infohub.entry_notify)
SEED="$HERE/m3c_seed.py"

TMP="$(mktemp -d /tmp/infohub-m3c-XXXXXX)"
trap 'rm -rf "$TMP"; [ -n "${UI_PID:-}" ] && kill "$UI_PID" 2>/dev/null' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

HUB_ROOT="$TMP/root"; mkdir -p "$HUB_ROOT"
CFG="$TMP/hub.config.yaml"
python3 - "$ROOT/hub.config.yaml" "$CFG" "$HERE/notifier_stub.py" "$TMP/notify.log" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["board"]["enabled"] = False
raw["notify"].update({"enabled": True, "backend": "command",
                      "command": [sys.executable, sys.argv[3], sys.argv[4]],
                      "min_interval_s": 0, "max_per_hour": 50, "max_per_day_per_kind": 50,
                      "idle_max_s": 0, "require_ui_event": True, "respect_dnd": True})
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
export HUB_CONFIG="$CFG" HUB_ROOT

hub_store() { "${STORE[@]}" --config "$CFG" "$@"; }
hub_notify() { "${NOTIFY[@]}" --config "$CFG" "$@"; }
hub_rules() { "${RULES[@]}" --config "$CFG" "$@"; }
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
tail1() { tail -1; }

# ---- 种子：真事件（3 次同文件改动 / 3 条同域剪贴板链接 / 1 条界面事件）→ 规则引擎自己出待办
HUB_ROOT="$HUB_ROOT" HUB_CONFIG="$CFG" python3 "$SEED" >/dev/null
hub_rules --json derive >/dev/null 2>&1
IDS="$(sqlq "[r['id'] for r in s.todos(limit=50) if r['dedupe_key'].startswith('fs-repeat') or r['dedupe_key'].startswith('clipboard-domain-repeat')]" | tail1)"
A="$(echo "$IDS" | python3 -c "import ast,sys;v=ast.literal_eval(sys.stdin.read());print(v[0])")"
B="$(echo "$IDS" | python3 -c "import ast,sys;v=ast.literal_eval(sys.stdin.read());print(v[1])")"
EVID_A="$(sqlq "s.conn.execute('SELECT source_event_ids FROM todos WHERE id = ?', ('$A',)).fetchone()[0]" | tail1)"
EVID_B="$(sqlq "s.conn.execute('SELECT source_event_ids FROM todos WHERE id = ?', ('$B',)).fetchone()[0]" | tail1)"

# ---- T1 每条待办都带证据，且证据能在 events 里逐条查到 -------------------------
n1="$(sqlq "sum(1 for r in s.conn.execute('SELECT source_event_ids FROM todos').fetchall() if not __import__('json').loads(r[0] or '[]'))" | tail1)"
len1="$(sqlq "len(s.todo_evidence('$A'))" | tail1)"
miss1="$(sqlq "sum(1 for r in s.todo_evidence('$A') if r.get('missing'))" | tail1)"
[ "$n1" = "0" ] && [ "$len1" = "3" ] && [ "$miss1" = "0" ] \
  && pass "T1 规则产出的待办都带真证据（A 摊开 3 条、missing=0）" \
  || fail "T1 证据完整" "无证据待办=$n1 len=$len1 missing=$miss1"

# ---- T2 控制台能摊开一条待办（证据 + 改动记录） -------------------------------
UI_OUT="$TMP/ui.out"
python3 -m infohub.entry_ui --config "$CFG" --port 0 >"$UI_OUT" 2>"$TMP/ui.err" &
UI_PID=$!
for _ in $(seq 1 60); do grep -q '"ui"' "$UI_OUT" 2>/dev/null && break; sleep 0.1; done
PORT="$(json_get "d['port']" <"$UI_OUT")"
base="http://127.0.0.1:$PORT"
det="$(curl -s "$base/api/todo/detail?id=$A")"
[ "$(echo "$det" | json_get "d['todo']['id']")" = "$A" ] \
  && [ "$(echo "$det" | json_get "len(d['evidence'])")" -ge 1 ] \
  && [ "$(echo "$det" | json_get "sum(1 for e in d['evidence'] if e.get('missing'))")" = "0" ] \
  && [ "$(echo "$det" | json_get "'revisions' in d")" = "True" ] \
  && pass "T2 /api/todo/detail 摊开证据链（$A）" || fail "T2 detail" "$(echo "$det" | head -c 200)"

# ---- T3 不存在的 id：UI 404、CLI 退出 1 ---------------------------------------
c3="$(curl -s -o /dev/null -w '%{http_code}' "$base/api/todo/detail?id=t-不存在")"
hub_store todo "t-不存在" >/dev/null 2>&1; r3=$?
[ "$c3" = "404" ] && [ "$r3" = "1" ] \
  && pass "T3 未知 id：UI 404 + CLI 退出 1" || fail "T3 未知 id" "ui=$c3 cli=$r3"

# ---- T4 CLI 打印依据与改动记录 ------------------------------------------------
out4="$(hub_store todo "$A")"
echo "$out4" | grep -q "依据 3 条事件" && echo "$out4" | grep -q "改动记录 0 条" \
  && pass "T4 hub-store todo <id> 打印依据与改动记录" || fail "T4 CLI 详情" "$(echo "$out4" | head -5)"

# ---- T5 证据空洞如实暴露（missing 不静默吞掉），验完恢复 ----------------------
sqlq "s.conn.execute(\"UPDATE todos SET source_event_ids = ? WHERE id = ?\", ('[\\\"999999\\\"]', '$A'))" >/dev/null
det5="$(curl -s "$base/api/todo/detail?id=$A")"
[ "$(echo "$det5" | json_get "d['evidence'][0]['missing']")" = "True" ] \
  && pass "T5 证据 id 查不到 ⇒ 如实标 missing（不假装有依据）" || fail "T5 证据空洞" "$(echo "$det5" | head -c 200)"
sqlq "s.conn.execute('UPDATE todos SET source_event_ids = ? WHERE id = ?', ('$EVID_A', '$A'))" >/dev/null

# ---- T6 证据链与清单留痕合流：人改一次 ⇒ detail 里能同时看到状态与依据 --------
hub_store todo-set "$A" done >/dev/null
det6="$(curl -s "$base/api/todo/detail?id=$A")"
[ "$(echo "$det6" | json_get "len(d['revisions'])")" = "1" ] \
  && [ "$(echo "$det6" | json_get "d['revisions'][0]['field']")" = "status" ] \
  && [ "$(echo "$det6" | json_get "d['revisions'][0]['author']")" = "human" ] \
  && [ "$(echo "$det6" | json_get "d['revisions'][0]['before']")" = "open" ] \
  && [ "$(echo "$det6" | json_get "d['revisions'][0]['after']")" = "done" ] \
  && [ "$(echo "$det6" | json_get "d['todo']['human_override']")" = "1" ] \
  && [ "$(echo "$det6" | json_get "len(d['evidence'])")" = "3" ] \
  && pass "T6 摊开的是「状态改动 + 原始依据」两样，不是二选一" \
  || fail "T6 留痕合流" "$(echo "$det6" | head -c 300)"
hub_store todo-set "$A" open >/dev/null

# ---- T7 摊开证据是只读的（events/todos 一个字节都不动） -----------------------
before7="$(sqlq "(s.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0], s.conn.execute('SELECT COUNT(*) FROM todos').fetchone()[0], s.conn.execute('SELECT SUM(LENGTH(source_event_ids)) FROM todos').fetchone()[0], s.conn.execute('SELECT COUNT(*) FROM todo_revisions').fetchone()[0])" | tail1)"
curl -s "$base/api/todo/detail?id=$B" >/dev/null
hub_store todo "$B" >/dev/null
after7="$(sqlq "(s.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0], s.conn.execute('SELECT COUNT(*) FROM todos').fetchone()[0], s.conn.execute('SELECT SUM(LENGTH(source_event_ids)) FROM todos').fetchone()[0], s.conn.execute('SELECT COUNT(*) FROM todo_revisions').fetchone()[0])" | tail1)"
[ "$before7" = "$after7" ] \
  && pass "T7 证据链只读（events/todos/改动记录均未变：$after7）" || fail "T7 只读" "$before7 → $after7"

# ---- T8 门 8「无依据不发」：候选全都没证据 ⇒ 一条都不发 ----------------------
sqlq "s.conn.execute('UPDATE todos SET source_event_ids = ?', ('[]',))" >/dev/null
sqlq "s.conn.execute('UPDATE todos SET notified_at = NULL, snooze_until = NULL')" >/dev/null
: > "$TMP/notify.log"
out8="$(hub_notify --json dispatch)"
[ "$(echo "$out8" | json_get "d['sent']")" = "0" ] \
  && [ "$(echo "$out8" | json_get "d.get('skip') or d.get('reason')")" = "no-evidence" ] \
  && [ "$(sqlq "s.conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]" | tail1)" = "0" ] \
  && pass "T8 门 8：候选无依据 ⇒ reason=no-evidence、一条不发" \
  || fail "T8 无依据不发" "$(echo "$out8" | head -c 300)"

# ---- T9 无依据的候选**不许饿死**别的候选：只给 B 依据、并把 A 排到最前 -------
sqlq "s.conn.execute('UPDATE todos SET source_event_ids = ?, created_at = ? WHERE id = ?', ('$EVID_B', '2099-01-01T00:00:00+08:00', '$B'))" >/dev/null
out9="$(hub_notify --json dispatch)"
[ "$(echo "$out9" | json_get "d['sent']")" = "1" ] \
  && [ "$(echo "$out9" | json_get "d['todo']['id']")" = "$B" ] \
  && [ "$(echo "$out9" | json_get "d['evidence']")" = "3" ] \
  && [ "$(echo "$out9" | json_get "d['kind']")" = "clipboard-domain-repeat" ] \
  && pass "T9 无依据候选被跳过、有依据的照常发（发给 $B，evidence=3）" \
  || fail "T9 不饿死别的候选" "$(echo "$out9" | head -c 300)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
