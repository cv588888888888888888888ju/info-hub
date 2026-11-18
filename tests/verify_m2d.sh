#!/usr/bin/env bash
# info-hub M2d 验收：通知按钮补全 —— 「打开」只留痕 + 可选开 URL；「稍后」把待办推迟再提示。
# 判据见 INTERFACE.md §2.4 / §4.6.1 / §4.6.3 / §5.5 / §8。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
NOTIFY=(python3 -m infohub.entry_notify)
SEED="$HERE/m2_seed.py"
STUB="$HERE/gdbus_stub.py"

TMP="$(mktemp -d /tmp/infohub-m2d-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

mkdir -p "$TMP/fakebin"
cat > "$TMP/fakebin/gdbus" <<EOF
#!/bin/sh
exec python3 "$STUB" "\$@"
EOF
chmod +x "$TMP/fakebin/gdbus"
FAKE_PATH="$TMP/fakebin:$PATH"

# 「打开」用的替身命令：把收到的 URL 原样写到 $OPEN_LOG
cat > "$TMP/open_stub.sh" <<EOF
#!/bin/sh
printf '%s' "\$1" > "$TMP/open.log"
EOF
chmod +x "$TMP/open_stub.sh"

mkcfg() {  # mkcfg <out> [notify.k=v ...]（值走 yaml.safe_load，可给 list）
  local out="$1"; shift
  python3 - "$ROOT/hub.config.yaml" "$out" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for kv in sys.argv[3:]:
    k, v = kv.split("=", 1)
    raw["notify"][k] = yaml.safe_load(v)
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
}

CFG_ROOT="$TMP/r"; CFG="$TMP/c.yaml"; LOG="$TMP/gdbus.log"
BASE=(enabled=true backend=gdbus dnd_source=off idle_source=off require_ui_event=true
      min_interval_s=0 max_per_hour=0 max_per_day_per_kind=0 suppress_days=7
      snooze_minutes=45 open_url=https://example.invalid/console
      "open_command=[\"$TMP/open_stub.sh\", \"{url}\"]")
mkcfg "$CFG" "${BASE[@]}"

seed() { HUB_ROOT="$1" HUB_CONFIG="$CFG" python3 "$SEED" "$2" "$3" "${4:-fs-repeat}" >/dev/null; }
notifyq() { GDBUS_STUB_CALL_LOG="$LOG" HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG" "${NOTIFY[@]}" --json "$@"; }
sqlq() {  # sqlq <expr> —— 读写当前库（写完统一 commit）
  HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG" python3 - "$1" <<'PY'
import sys
from infohub.config import load
from infohub.store import Store
c = load(); s = Store(c.db_path)
print(eval(sys.argv[1]))
s.conn.commit()
PY
}
ui_event() {  # 造一条**新的** window 事件（门 4 要它）
  HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG" python3 - <<'PY'
import time
from infohub.config import load
from infohub.store import Store
from infohub import events as E
c = load(); s = Store(c.db_path)
s.insert_events([E.make_event("window", "focus", {"app": "t", "title": "x", "n": time.time_ns()})])
s.conn.commit()
PY
}

# ---------------------------------------------------------------- T1 默认三个按钮
seed "$CFG_ROOT" 1 1 fs-repeat
OUT1="$(PATH="$FAKE_PATH" notifyq dispatch)"
S1="$(printf '%s' "$OUT1" | json_get "d['sent']")"
A1="$(printf '%s' "$OUT1" | json_get "d['actions']")"
TID="$(printf '%s' "$OUT1" | json_get "d['todo']['id']")"
LINE1="$(tail -1 "$LOG")"
[ "$S1" = "1" ] && [ "$A1" = "['打开', '稍后', '忽略此类']" ] \
  && [ "${LINE1#*打开}" != "$LINE1" ] && [ "${LINE1#*稍后}" != "$LINE1" ] \
  && pass "T1 默认带三个按钮（打开/稍后/忽略此类）且真传给 gdbus" \
  || fail "T1 按钮集" "sent=$S1 actions=$A1 line=$LINE1"

# ---------------------------------------------------------------- T2 「稍后」⇒ 推迟 + 清 notified_at，状态仍 open
OUT2="$(PATH="$FAKE_PATH" notifyq act 77 稍后)"
SU2="$(printf '%s' "$OUT2" | json_get "d['snooze_until']")"
SM2="$(printf '%s' "$OUT2" | json_get "d['snooze_minutes']")"
ROW2="$(sqlq "[ (x['status'], x['notified_at'], x['snooze_until'] is not None) for x in s.todos(limit=50) if x['id']=='$TID' ]" | tail -1)"
[ "$SM2" = "45" ] && [ -n "$SU2" ] && [ "$SU2" != "None" ] && [ "$ROW2" = "[('open', None, True)]" ] \
  && pass "T2 「稍后」：推迟到 $SU2，notified_at 清空、状态仍 open" \
  || fail "T2 稍后" "until=$SU2 min=$SM2 row=$ROW2 out=$OUT2"

# ---------------------------------------------------------------- T3 推迟期未到 ⇒ 门拦住（reason=snoozed）
ui_event
OUT3="$(PATH="$FAKE_PATH" notifyq dispatch)"
R3="$(printf '%s' "$OUT3" | json_get "d['reason']")"
[ "$R3" = "snoozed" ] && [ "$(printf '%s' "$OUT3" | json_get "d['sent']")" = "0" ] \
  && pass "T3 推迟未到 ⇒ reason=snoozed（不是 no-pending-todo，看得见原因）" || fail "T3 门" "reason=$R3 out=$OUT3"

# ---------------------------------------------------------------- T4 到点后**同一条**待办会再提示一次
sqlq "s.conn.execute(\"UPDATE todos SET snooze_until = '2020-01-01T00:00:00+08:00' WHERE id = '$TID'\").rowcount" >/dev/null
ui_event
OUT4="$(PATH="$FAKE_PATH" notifyq dispatch)"
[ "$(printf '%s' "$OUT4" | json_get "d['sent']")" = "1" ] \
  && [ "$(printf '%s' "$OUT4" | json_get "d['todo']['id']")" = "$TID" ] \
  && pass "T4 到点后同一条待办再提示一次（不是永久丢掉）" || fail "T4 到点" "out=$OUT4"

# ---------------------------------------------------------------- T5 配置能改按钮集（只留一个）
ui_event
seed "$CFG_ROOT" 1 1 clipboard-domain-repeat   # T4 把唯一那条弹过了 ⇒ 再要一条 pending
CFG5="$TMP/c5.yaml"; mkcfg "$CFG5" "${BASE[@]}" 'actions=[忽略此类]'
LOG5="$TMP/gdbus5.log"; : > "$LOG5"
OUT5="$(GDBUS_STUB_CALL_LOG="$LOG5" HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG5" "${NOTIFY[@]}" --json dispatch)"
L5="$(tail -1 "$LOG5")"
[ "$(printf '%s' "$OUT5" | json_get "d['actions']")" = "['忽略此类']" ] \
  && [ "${L5#*稍后}" = "$L5" ] && [ "${L5#*打开}" = "$L5" ] \
  && pass "T5 notify.actions 能只留一个按钮（不再传另外两个）" || fail "T5 配置按钮" "line=$L5 out=$OUT5"

# ---------------------------------------------------------------- T6 「打开」：留痕 + 真执行（{url} 替换）
ui_event
PATH="$FAKE_PATH" notifyq dispatch >/dev/null          # 先有一条新通知（dbus_id=77）
rm -f "$TMP/open.log"
OUT6="$(PATH="$FAKE_PATH" notifyq act 77 打开)"
OP6="$(printf '%s' "$OUT6" | json_get "d['opened']")"
URL6="$(cat "$TMP/open.log" 2>/dev/null)"
NID6="$(printf '%s' "$OUT6" | json_get "d['notification_id']")"
ACT6="$(sqlq "[r['action'] for r in s.notifications(50) if r['id']==$NID6][0]" | tail -1)"
DUMP6="$(sqlq "[(r['id'], r['dbus_id'], r['action']) for r in s.notifications(3)]" | tail -1)"
ST6="$(sqlq "[x['status'] for x in s.todos(limit=50) if x['id']=='$TID']" | tail -1)"
[ "$OP6" = "True" ] && [ "$URL6" = "https://example.invalid/console" ] && [ "$ACT6" = "open" ] \
  && [ "$ST6" = "['open']" ] \
  && pass "T6 「打开」真执行替身命令（{url} 正确替换）、台账记 open、待办状态不变" \
  || fail "T6 打开" "opened=$OP6 url=$URL6 action=$ACT6 status=$ST6 近三条=$DUMP6"

# ---------------------------------------------------------------- T7 打开命令失败 ⇒ 只记 open_error，点击不算失败
CFG7="$TMP/c7.yaml"; mkcfg "$CFG7" "${BASE[@]}" 'open_command=["/nonexistent/cmd-xyz", "{url}"]'
OUT7="$(HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG7" "${NOTIFY[@]}" --json act 77 open)"; RC7=$?
E7="$(printf '%s' "$OUT7" | json_get "d['open_error']")"
[ "$RC7" = "0" ] && [ -n "$E7" ] && [ "$E7" != "None" ] \
  && pass "T7 打开命令起不来 ⇒ 退出码仍 0、记 open_error（fail-soft）" || fail "T7 open 失败" "$OUT7"

# ---------------------------------------------------------------- T8 listen 认「稍后」
ui_event
seed "$CFG_ROOT" 1 1 window-appear          # 新规则 ⇒ 新待办（保证有 pending 可弹）
OUT8D="$(GDBUS_STUB_ID=78 PATH="$FAKE_PATH" notifyq dispatch)"
printf "ActionInvoked(uint32 78, '稍后')\n" > "$TMP/mon8.txt"
OUT8="$(GDBUS_STUB_MONITOR="$TMP/mon8.txt" GDBUS_STUB_HOLD=0 PATH="$FAKE_PATH" notifyq listen --once)"
[ "$(printf '%s' "$OUT8D" | json_get "d['sent']")" = "1" ] || fail "T8 前置：dispatch 没发出" "$OUT8D"
N8="$(sqlq "len([x for x in s.todos(limit=50) if x['snooze_until']])" | tail -1)"
[ "$(printf '%s' "$OUT8" | json_get "d['snoozed']")" = "1" ] && [ "$N8" != "0" ] \
  && pass "T8 listen 收到「稍后」⇒ snoozed=1 且库里有 snooze_until" || fail "T8 listen 稍后" "n=$N8 out=$OUT8 d=$OUT8D"

# ---------------------------------------------------------------- T9 listen 认「打开」
ui_event
seed "$CFG_ROOT" 1 1 session-stale          # 再换一个规则 ⇒ 又一条新待办
OUT9D="$(GDBUS_STUB_ID=79 PATH="$FAKE_PATH" notifyq dispatch)"
printf "ActionInvoked(uint32 79, '打开')\n" > "$TMP/mon9.txt"
OUT9="$(GDBUS_STUB_MONITOR="$TMP/mon9.txt" GDBUS_STUB_HOLD=0 PATH="$FAKE_PATH" notifyq listen --once)"
[ "$(printf '%s' "$OUT9D" | json_get "d['sent']")" = "1" ] || fail "T9 前置：dispatch 没发出" "$OUT9D"
[ "$(printf '%s' "$OUT9" | json_get "d['opened']")" = "1" ] && [ "$(printf '%s' "$OUT9" | json_get "d['acted']")" = "0" ] \
  && pass "T9 listen 收到「打开」⇒ opened=1（没被算成 done）" || fail "T9 listen 打开" "out=$OUT9 d=$OUT9D"

# ---------------------------------------------------------------- T10 未知键仍打回
OUT10="$(PATH="$FAKE_PATH" notifyq act 77 nosuch; echo "exit=$?")"
[ "$(printf '%s' "$OUT10" | tail -1)" = "exit=1" ] && pass "T10 未知动作键打回（退出码 1）" || fail "T10 未知键" "$OUT10"

# ---------------------------------------------------------------- T11 status 摊开按钮与稍后时长
OUT11="$(PATH="$FAKE_PATH" notifyq status)"
[ "$(printf '%s' "$OUT11" | json_get "d['snooze_minutes']")" = "45" ] \
  && [ "$(printf '%s' "$OUT11" | json_get "len(d['actions'])")" = "3" ] \
  && pass "T11 status 带上按钮集与 snooze_minutes" || fail "T11 status" "$OUT11"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
