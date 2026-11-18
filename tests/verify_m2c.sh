#!/usr/bin/env bash
# info-hub M2c 验收：「忽略此类」—— 不只是一条待办，而是把**整个通知类别**压住若干天
# （到期自动恢复；可解除；被压住的类别不许堵住别的类别 ⇒ 选候选时就跳过）。
# 判据见 INTERFACE.md §2.4 / §4.6 门 6 / §5.6 / §8。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
NOTIFY=(python3 -m infohub.entry_notify)
SEED="$HERE/m2_seed.py"
STUB="$HERE/gdbus_stub.py"

TMP="$(mktemp -d /tmp/infohub-m2c-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# 假 gdbus（tests/gdbus_stub.py）：call 回 (uint32 N,)、monitor 吐注入的信号行
mkdir -p "$TMP/fakebin"
cat > "$TMP/fakebin/gdbus" <<EOF
#!/bin/sh
exec python3 "$STUB" "\$@"
EOF
chmod +x "$TMP/fakebin/gdbus"
FAKE_PATH="$TMP/fakebin:$PATH"

mkcfg() {  # mkcfg <out> [notify.k=v ...]
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

CFG_ROOT="$TMP/r"; CFG="$TMP/c.yaml"
BASE=(enabled=true backend=gdbus dnd_source=off idle_source=off require_ui_event=true
      min_interval_s=0 max_per_hour=0 max_per_day_per_kind=0 suppress_days=7)
mkcfg "$CFG" "${BASE[@]}"

seed() { HUB_ROOT="$1" HUB_CONFIG="$CFG" python3 "$SEED" "$2" "$3" "${4:-fs-repeat}" >/dev/null; }
notifyq() { HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG" "${NOTIFY[@]}" --json "$@"; }
sqlq() {  # sqlq <expr>  —— 读当前 $CFG/$CFG_ROOT（表达式若写了库，结束后统一 commit）
  HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG" python3 - "$1" <<'PY'
import sys
from infohub.config import load
from infohub.store import Store
c = load(); s = Store(c.db_path)
print(eval(sys.argv[1]))
s.conn.commit()
PY
}

# ---------------------------------------------------------------- T1 先正常弹一条 fs-repeat
seed "$CFG_ROOT" 1 1 fs-repeat
OUT1="$(PATH="$FAKE_PATH" notifyq dispatch)"
S1="$(printf '%s' "$OUT1" | json_get "d['sent']")"
K1="$(printf '%s' "$OUT1" | json_get "d['kind']")"
D1="$(printf '%s' "$OUT1" | json_get "d['dbus_id']")"
[ "$S1" = "1" ] && [ "$K1" = "fs-repeat" ] && [ "$D1" = "77" ] \
  && pass "T1 先弹一条 fs-repeat（dbus_id=77）" || fail "T1 前置" "sent=$S1 kind=$K1 out=$OUT1"

# ---------------------------------------------------------------- T2 点「忽略此类」⇒ 类别被压住 + 这一条也 dropped
OUT2="$(PATH="$FAKE_PATH" notifyq act 77 ignore-kind)"
K2="$(printf '%s' "$OUT2" | json_get "d['kind']")"
U2="$(printf '%s' "$OUT2" | json_get "d['suppressed_until']")"
DY2="$(printf '%s' "$OUT2" | json_get "d['days']")"
TID2="$(printf '%s' "$OUT2" | json_get "d['todo_id']")"
ROW2="$(sqlq "[ (x['status'], x['human_override']) for x in s.todos(limit=200) if x['id']=='$TID2' ]" | tail -1)"
[ "$K2" = "fs-repeat" ] && [ -n "$U2" ] && [ "$DY2" = "7" ] && [ "$ROW2" = "[('dropped', 1)]" ] \
  && pass "T2 忽略此类：类别+到期时间落库，这条待办也 dropped（人点的）" \
  || fail "T2 忽略此类" "kind=$K2 until=$U2 days=$DY2 row=$ROW2 out=$OUT2"

# ---------------------------------------------------------------- T3 台账记下动作（留痕）
A3="$(sqlq "s.notifications(1)[0]['action']" | tail -1)"
[ "$A3" = "ignore-kind" ] && pass "T3 台账记下动作 ignore-kind" || fail "T3 台账" "action=$A3"

# ---------------------------------------------------------------- T4 同类再出现 ⇒ 门 6 直接拦住
seed "$CFG_ROOT" 3 1 fs-repeat
OUT4="$(PATH="$FAKE_PATH" notifyq dispatch)"
R4="$(printf '%s' "$OUT4" | json_get "d['reason']")"
S4="$(printf '%s' "$OUT4" | json_get "d['sent']")"
[ "$R4" = "kind-suppressed" ] && [ "$S4" = "0" ] \
  && pass "T4 同类再出现 ⇒ 门 6 拦住（reason=kind-suppressed）" || fail "T4 门 6" "reason=$R4 sent=$S4 out=$OUT4"

# ---------------------------------------------------------------- T5 别的类别不被堵住（关键：选候选时就跳过被压住的类别）
seed "$CFG_ROOT" 2 1 session-stale
OUT5="$(PATH="$FAKE_PATH" notifyq dispatch)"
S5="$(printf '%s' "$OUT5" | json_get "d['sent']")"
K5="$(printf '%s' "$OUT5" | json_get "d['kind']")"
[ "$S5" = "1" ] && [ "$K5" = "session-stale" ] \
  && pass "T5 别的类别不被堵住（弹 session-stale）" || fail "T5 饿死检查" "sent=$S5 kind=$K5 out=$OUT5"

# ---------------------------------------------------------------- T6 列出抑制
L6="$(PATH="$FAKE_PATH" notifyq suppressions)"
H6="$(printf '%s' "$L6" | json_get "[r['kind'] for r in d]")"
[ "$H6" = "['fs-repeat']" ] && pass "T6 suppressions 列出生效中的类别" || fail "T6 列表" "kinds=$H6 out=$L6"

# ---------------------------------------------------------------- T7 解除抑制 ⇒ 同类又能弹
U7="$(PATH="$FAKE_PATH" notifyq unsuppress fs-repeat)"
seed "$CFG_ROOT" 4 1 fs-repeat
OUT7="$(PATH="$FAKE_PATH" notifyq dispatch)"
[ "$(printf '%s' "$U7" | json_get "d['removed']")" = "True" ] \
  && [ "$(printf '%s' "$OUT7" | json_get "d['sent']")" = "1" ] \
  && pass "T7 unsuppress 后同类又能弹" || fail "T7 解除" "u=$U7 out=$OUT7"

# ---------------------------------------------------------------- T8 坏参数打回
OUT8="$(PATH="$FAKE_PATH" notifyq suppress fs-repeat --days 0; echo "exit=$?")"
[ "$(printf '%s' "$OUT8" | tail -1)" = "exit=1" ] && pass "T8 --days 0 打回（退出码 1）" || fail "T8 坏参数" "$OUT8"

# ---------------------------------------------------------------- T9 到期自动恢复（不是永久屏蔽）
PATH="$FAKE_PATH" notifyq suppress fs-repeat --days 7 >/dev/null
sqlq "s.conn.execute(\"UPDATE kind_suppressions SET until = '2020-01-01T00:00:00+08:00' WHERE kind = 'fs-repeat'\").rowcount" >/dev/null
G9="$(sqlq "s.kind_suppressed('fs-repeat')")"
seed "$CFG_ROOT" 5 1 fs-repeat
OUT9="$(PATH="$FAKE_PATH" notifyq dispatch)"
[ "$G9" = "None" ] && [ "$(printf '%s' "$OUT9" | json_get "d['sent']")" = "1" ] \
  && pass "T9 到期即自动恢复（不是永久屏蔽）" || fail "T9 到期" "kind_suppressed=$G9 out=$OUT9"

# ---------------------------------------------------------------- T10 status 摊开抑制现状
OUT10="$(PATH="$FAKE_PATH" notifyq status)"
SD="$(printf '%s' "$OUT10" | json_get "d['suppress_days']")"
NS="$(printf '%s' "$OUT10" | json_get "len(d['suppressions'])")"
[ "$SD" = "7" ] && [ "$NS" = "0" ] \
  && pass "T10 status 摊开抑制现状（suppress_days=7、生效中 0 条）" || fail "T10 status" "days=$SD active=$NS"

# ---------------------------------------------------------------- T11 真按钮路径：信号行带「忽略此类」⇒ 类别被压住
seed "$CFG_ROOT" 6 1 clipboard-domain-repeat
OUT11D="$(GDBUS_STUB_ID=78 PATH="$FAKE_PATH" notifyq dispatch)"
[ "$(printf '%s' "$OUT11D" | json_get "d['dbus_id']")" = "78" ] || fail "T11 前置" "out=$OUT11D"
printf "ActionInvoked(uint32 78, '忽略此类')\n" > "$TMP/mon11.txt"
OUT11="$(GDBUS_STUB_MONITOR="$TMP/mon11.txt" GDBUS_STUB_HOLD=0 PATH="$FAKE_PATH" notifyq listen --once)"
SUP11="$(printf '%s' "$OUT11" | json_get "d['suppressed']")"
KIND11="$(sqlq "s.kind_suppressed('clipboard-domain-repeat') is not None")"
[ "$SUP11" = "1" ] && [ "$KIND11" = "True" ] \
  && pass "T11 listen 收到「忽略此类」⇒ 类别被压住、计数 suppressed=1" \
  || fail "T11 按钮路径" "suppressed=$SUP11 kind_suppressed=$KIND11 out=$OUT11"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
