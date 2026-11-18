#!/usr/bin/env bash
# info-hub M2b 验收：系统通知「按钮真的有用」——动作回调 → 待办状态回写 + 台账留痕。
# 全程假 gdbus（tests/gdbus_stub.py），不弹真窗口。判据见 INTERFACE.md §2.4 / §4.6 / §5.5。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
NOTIFY=(python3 -m infohub.entry_notify)
SEED="$HERE/m2_seed.py"
STUB="$HERE/gdbus_stub.py"
CMDSTUB="$HERE/notifier_stub.py"

TMP="$(mktemp -d /tmp/infohub-m2b-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# 假 gdbus：monitor 吐信号、call 返回 (uint32 N,)
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

seed() { HUB_ROOT="$1" HUB_CONFIG="$CFG" python3 "$SEED" "$2" "$3" "${4:-fs-repeat}" >/dev/null; }
notifyq() { HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG" "${NOTIFY[@]}" --json "$@"; }
sqlq() {  # sqlq <expr>  —— 读当前 $CFG/$CFG_ROOT
  HUB_ROOT="$CFG_ROOT" HUB_CONFIG="$CFG" python3 - "$1" <<'PY'
import sys
from infohub.config import load
from infohub.store import Store
c = load(); s = Store(c.db_path)
print(eval(sys.argv[1]))
PY
}

CFG_ROOT="$TMP/r"; CFG="$TMP/c.yaml"
BASE=(enabled=true backend=gdbus dnd_source=off idle_source=off require_ui_event=true
      min_interval_s=0 max_per_hour=0 max_per_day_per_kind=0)
mkcfg "$CFG" "${BASE[@]}"
: > "$TMP/gdbus.log"

# ---------------------------------------------------------------- T1 通知真的带上按钮 + 台账记下系统通知 id
seed "$CFG_ROOT" 1 1
OUT1="$(GDBUS_STUB_CALL_LOG="$TMP/gdbus.log" PATH="$FAKE_PATH" notifyq dispatch)"
S1="$(printf '%s' "$OUT1" | json_get "d['sent']")"
D1="$(printf '%s' "$OUT1" | json_get "d['dbus_id']")"
[ "$S1" = "1" ] && [ "$D1" = "77" ] && pass "T1 弹通知成功且记下 dbus_id=77" \
  || fail "T1 弹通知" "sent=$S1 dbus_id=$D1 out=$OUT1"
DB1="$(sqlq "s.notifications(1)[0]['dbus_id']" | tail -1)"
[ "$DB1" = "77" ] && pass "T1b 台账落 dbus_id（能按它回查）" || fail "T1b 台账" "dbus_id=$DB1"
# 默认按钮集是 M2d 定的三个（打开/稍后/忽略此类）；这里只断言「按钮真的传给了 Notify」。
grep -q "打开" "$TMP/gdbus.log" && grep -q "忽略此类" "$TMP/gdbus.log" \
  && pass "T1c Notify 真的带了按钮（默认三个：打开/稍后/忽略此类）" || fail "T1c 按钮" "$(tail -1 "$TMP/gdbus.log")"

# ---------------------------------------------------------------- T2 点「完成」⇒ 待办 done + 留痕
OUT2="$(PATH="$FAKE_PATH" notifyq act 77 acted)"
ST2="$(printf '%s' "$OUT2" | json_get "d['status']")"
[ "$ST2" = "done" ] && pass "T2 act 77 acted ⇒ status=done" || fail "T2 act" "out=$OUT2"
TID2="$(printf '%s' "$OUT2" | json_get "d['todo_id']")"
ROW2="$(sqlq "([ (x['status'], x['human_override']) for x in s.todos(limit=200) if x['id']=='$TID2' ][0], s.notifications(1)[0]['action'])" | tail -1)"
[ "$ROW2" = "(('done', 1), 'acted')" ] && pass "T2b 待办 done + human_override=1 + 台账 action=acted" \
  || fail "T2b 回写" "got=$ROW2"

# ---------------------------------------------------------------- T3 有效性样本把「点了」算成 acted
OUT3="$(PATH="$FAKE_PATH" notifyq effectiveness)"
ACT3="$(printf '%s' "$OUT3" | json_get "d['kinds'][0]['acted'] if d['kinds'] else None")"
IGN3="$(printf '%s' "$OUT3" | json_get "d['kinds'][0]['ignored'] if d['kinds'] else None")"
[ "$ACT3" = "1" ] && [ "$IGN3" = "0" ] && pass "T3 有效性：acted=1 ignored=0（M4a 的输入正确）" \
  || fail "T3 有效性" "acted=$ACT3 ignored=$IGN3"

# ---------------------------------------------------------------- T4/T5 坏输入必须打回
OUT4="$(PATH="$FAKE_PATH" notifyq act 999 acted; echo "exit=$?")"
[ "$(printf '%s' "$OUT4" | tail -1)" = "exit=1" ] && pass "T4 未知 dbus_id ⇒ 退出码 1" \
  || fail "T4 未知 id" "out=$OUT4"
OUT5="$(PATH="$FAKE_PATH" notifyq act 77 bogus; echo "exit=$?")"
[ "$(printf '%s' "$OUT5" | tail -1)" = "exit=1" ] && pass "T5 不认识的动作键 ⇒ 退出码 1" \
  || fail "T5 坏动作键" "out=$OUT5"

# ---------------------------------------------------------------- T6 点「忽略」⇒ 待办 dropped
seed "$CFG_ROOT" 1 1 session-stale
OUT6D="$(GDBUS_STUB_ID=78 PATH="$FAKE_PATH" notifyq dispatch)"
[ "$(printf '%s' "$OUT6D" | json_get "d['dbus_id']")" = "78" ] || fail "T6 前置" "out=$OUT6D"
OUT6="$(PATH="$FAKE_PATH" notifyq act 78 ignore)"
TID6="$(printf '%s' "$OUT6" | json_get "d['todo_id']")"
ROW6="$(sqlq "[ (x['status'], x['human_override']) for x in s.todos(limit=200) if x['id']=='$TID6' ]" | tail -1)"
[ "$(printf '%s' "$OUT6" | json_get "d['status']")" = "dropped" ] && [ "$ROW6" = "[('dropped', 1)]" ] \
  && pass "T6 act 78 ignore ⇒ 待办 dropped（人点的，规则不许翻回）" \
  || fail "T6 忽略" "out=$OUT6 row=$ROW6"

# ---------------------------------------------------------------- T7 真监听路径：信号行 → 回写
seed "$CFG_ROOT" 1 1 clipboard-domain-repeat
OUT7D="$(GDBUS_STUB_ID=79 PATH="$FAKE_PATH" notifyq dispatch)"
[ "$(printf '%s' "$OUT7D" | json_get "d['dbus_id']")" = "79" ] || fail "T7 前置" "out=$OUT7D"
printf "ActionInvoked(uint32 79, 'acted')\n" > "$TMP/mon7.txt"
OUT7="$(GDBUS_STUB_MONITOR="$TMP/mon7.txt" GDBUS_STUB_HOLD=0 PATH="$FAKE_PATH" notifyq listen --once)"
A7="$(printf '%s' "$OUT7" | json_get "d['acted']")"
TID7="$(printf '%s' "$OUT7" | json_get "d['handled'][0]['todo_id']")"
R7="$(sqlq "[ (x['status'], x['human_override']) for x in s.todos(limit=200) if x['id']=='$TID7' ]" | tail -1)"
[ "$A7" = "1" ] && [ "$R7" = "[('done', 1)]" ] && pass "T7 listen：ActionInvoked ⇒ 待办 done" \
  || fail "T7 listen" "acted=$A7 row=$R7 out=$OUT7"

# ---------------------------------------------------------------- T8 竞态：通知已关/不认识 ⇒ 记 errors 但不失败
printf "ActionInvoked(uint32 9999, 'acted')\n" > "$TMP/mon8.txt"
OUT8="$(GDBUS_STUB_MONITOR="$TMP/mon8.txt" GDBUS_STUB_HOLD=0 PATH="$FAKE_PATH" notifyq listen --once; echo "exit=$?")"
E8="$(printf '%s' "$OUT8" | head -1 | json_get "len(d['errors'])")"
[ "$E8" -ge 1 ] && [ "$(printf '%s' "$OUT8" | tail -1)" = "exit=0" ] \
  && pass "T8 找不到对应通知 ⇒ 记 errors、退出码仍 0（竞态容忍）" \
  || fail "T8 竞态" "errors=$E8 out=$OUT8"

# ---------------------------------------------------------------- T9 关闭信号不算动作
printf "NotificationClosed(uint32 79, uint32 2)\n" > "$TMP/mon9.txt"
OUT9="$(GDBUS_STUB_MONITOR="$TMP/mon9.txt" GDBUS_STUB_HOLD=0 PATH="$FAKE_PATH" notifyq listen --once)"
C9="$(printf '%s' "$OUT9" | json_get "d['closed']")"
A9="$(printf '%s' "$OUT9" | json_get "d['acted']")"
[ "$C9" = "1" ] && [ "$A9" = "0" ] && pass "T9 NotificationClosed 只计数、不改状态" \
  || fail "T9 关闭信号" "closed=$C9 acted=$A9"

# ---------------------------------------------------------------- T10 monitor 起不来 ⇒ 退出码 1
OUT10="$(GDBUS_STUB_MONITOR_FAIL=1 PATH="$FAKE_PATH" notifyq listen --once 2>&1; echo "exit=$?")"
[ "$(printf '%s' "$OUT10" | tail -1)" = "exit=1" ] && pass "T10 monitor 起不来 ⇒ 退出码 1" \
  || fail "T10 monitor 失败" "out=$OUT10"

# ---------------------------------------------------------------- T11 老数据没有 todo_id ⇒ 明确报错
sqlq "s.add_notification('fs-repeat', '老的', [1], shown=True, dbus_id=555)" >/dev/null
OUT11="$(PATH="$FAKE_PATH" notifyq act 555 acted; echo "exit=$?")"
[ "$(printf '%s' "$OUT11" | tail -1)" = "exit=1" ] && pass "T11 通知没有 todo_id 指针 ⇒ 退出码 1（不瞎猜）" \
  || fail "T11 无指针" "out=$OUT11"

# ---------------------------------------------------------------- T12 其他后端不受影响（command 后端没有 dbus_id）
CFG12="$TMP/c12.yaml"; CFG_ROOT12="$TMP/r12"
python3 - "$ROOT/hub.config.yaml" "$CFG12" "$CMDSTUB" "$TMP/cmd12.log" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["notify"].update({"enabled": True, "backend": "command", "dnd_source": "off",
                      "idle_source": "off", "require_ui_event": True, "min_interval_s": 0,
                      "max_per_hour": 0, "max_per_day_per_kind": 0,
                      "command": ["python3", sys.argv[3], sys.argv[4], "{summary}", "{body}", "{kind}"]})
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
HUB_ROOT="$CFG_ROOT12" HUB_CONFIG="$CFG12" python3 "$SEED" 1 1 >/dev/null
OUT12="$(HUB_ROOT="$CFG_ROOT12" HUB_CONFIG="$CFG12" "${NOTIFY[@]}" --json dispatch)"
[ "$(printf '%s' "$OUT12" | json_get "d['sent']")" = "1" ] \
  && [ "$(printf '%s' "$OUT12" | json_get "d['dbus_id']")" = "None" ] \
  && pass "T12 backend=command 照常发，dbus_id=None（不假装有回调）" \
  || fail "T12 command 后端" "out=$OUT12"

# ---------------------------------------------------------------- T13 解析与常量（单元级）
OUT13="$(python3 - <<'PY' | tail -1
from infohub import notify
print(notify.parse_dbus_id("(uint32 70,)"), notify.parse_dbus_id("garbage"),
      notify.parse_dbus_id("(uint32 1234,)"), notify.MONITOR_ARGS == ["gdbus", "monitor", "--session", "--dest", "org.freedesktop.Notifications"],
      notify.ACTION_STATUS == {"acted": "done", "完成": "done", "ignore": "dropped", "忽略": "dropped"})
PY
)"
[ "$OUT13" = "70 None 1234 True True" ] && pass "T13 parse_dbus_id / MONITOR_ARGS / 动作映射正确" \
  || fail "T13 单元" "got=$OUT13"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
