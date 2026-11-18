#!/usr/bin/env bash
# info-hub M2 验收：通知层五道门 + 发送后端 + 台账。判据见 PROJECT.md §9 / INTERFACE.md §4.6。
# 全程不弹真窗口：backend=command 走 tests/notifier_stub.py。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
NOTIFY=(python3 -m infohub.entry_notify)
STUB="$HERE/notifier_stub.py"
SEED="$HERE/m2_seed.py"

TMP="$(mktemp -d /tmp/infohub-m2-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

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

seed() {  # seed <root> <todos> <window_events> [rule]
  HUB_ROOT="$1" HUB_CONFIG="${SEED_CFG:?}" python3 "$SEED" "$2" "$3" "${4:-fs-repeat}" >/dev/null
}

stub_lines() { [ -f "$1" ] && wc -l < "$1" || echo 0; }

# 一组配置：认证闸门全开，只留下被测的那道门
BASE=(enabled=true backend=command dnd_source=off idle_source=off require_ui_event=true
      min_interval_s=0 max_per_hour=0 max_per_day_per_kind=0)

# ---------------------------------------------------------------- T1 默认关（自带仓库配置）
CFG1="$TMP/c1.yaml"; ROOT1="$TMP/r1"
cp "$ROOT/hub.config.yaml" "$CFG1"
LOG1="$TMP/s1.log"
HUB_ROOT="$ROOT1" HUB_CONFIG="$CFG1" python3 "$SEED" 1 1 >/dev/null
OUT1="$(HUB_ROOT="$ROOT1" HUB_CONFIG="$CFG1" "${NOTIFY[@]}" --json dispatch)"
[ "$(printf '%s' "$OUT1" | json_get "d['reason']")" = "disabled" ] && [ "$(stub_lines "$LOG1")" = "0" ] \
  && pass "T1 default-off" || fail "T1 default-off" "out=$OUT1"

# ---------------------------------------------------------------- T2 没有界面事件 ⇒ 不弹
CFG2="$TMP/c2.yaml"; ROOT2="$TMP/r2"; LOG2="$TMP/s2.log"
mkcfg "$CFG2" "${BASE[@]}" "command=['python3','$STUB','$LOG2','{summary}','{body}','{kind}']"
SEED_CFG="$CFG2"; seed "$ROOT2" 1 0
OUT2="$(HUB_ROOT="$ROOT2" HUB_CONFIG="$CFG2" "${NOTIFY[@]}" --json dispatch)"
R2="$(printf '%s' "$OUT2" | json_get "d['reason']")"
[ "$R2" = "no-ui-event" ] && [ "$(stub_lines "$LOG2")" = "0" ] && pass "T2 no-ui-event" \
  || fail "T2 no-ui-event" "reason=$R2 out=$OUT2"

# ---------------------------------------------------------------- T3/T4 有界面事件 ⇒ 弹一次并消费该事件
CFG3="$TMP/c3.yaml"; ROOT3="$TMP/r3"; LOG3="$TMP/s3.log"
mkcfg "$CFG3" "${BASE[@]}" "command=['python3','$STUB','$LOG3','{summary}','{body}','{kind}']"
SEED_CFG="$CFG3"; seed "$ROOT3" 2 1
OUT3="$(HUB_ROOT="$ROOT3" HUB_CONFIG="$CFG3" "${NOTIFY[@]}" --json dispatch)"
S3="$(printf '%s' "$OUT3" | json_get "d['sent']")"
[ "$S3" = "1" ] && [ "$(stub_lines "$LOG3")" = "1" ] && pass "T3 sent-on-ui-event" \
  || fail "T3 sent-on-ui-event" "sent=$S3 out=$OUT3"
LED3="$(HUB_ROOT="$ROOT3" HUB_CONFIG="$CFG3" python3 -c "
from infohub.config import load
from infohub.store import Store
c=load(); s=Store(c.db_path)
n=s.notifications(limit=5); t=s.todos(status='open')
print(len([r for r in n if r['shown']]), sum(1 for r in t if r['notified_at']))")"
[ "$LED3" = "1 1" ] && pass "T3b ledger-and-mark" || fail "T3b ledger-and-mark" "ledger=$LED3"

OUT4="$(HUB_ROOT="$ROOT3" HUB_CONFIG="$CFG3" "${NOTIFY[@]}" --json dispatch)"
R4="$(printf '%s' "$OUT4" | json_get "d['reason']")"
[ "$R4" = "no-ui-event" ] && [ "$(stub_lines "$LOG3")" = "1" ] && pass "T4 ui-event-consumed" \
  || fail "T4 ui-event-consumed" "reason=$R4 lines=$(stub_lines "$LOG3")"

# ---------------------------------------------------------------- T5 最小间隔
CFG5="$TMP/c5.yaml"; ROOT5="$TMP/r5"; LOG5="$TMP/s5.log"
mkcfg "$CFG5" "${BASE[@]}" min_interval_s=3600 "command=['python3','$STUB','$LOG5','{summary}','{body}','{kind}']"
SEED_CFG="$CFG5"; seed "$ROOT5" 2 1
HUB_ROOT="$ROOT5" HUB_CONFIG="$CFG5" "${NOTIFY[@]}" --json dispatch >/dev/null
HUB_ROOT="$ROOT5" HUB_CONFIG="$CFG5" python3 "$SEED" 0 1 >/dev/null
OUT5="$(HUB_ROOT="$ROOT5" HUB_CONFIG="$CFG5" "${NOTIFY[@]}" --json dispatch)"
R5="$(printf '%s' "$OUT5" | json_get "d['reason']")"
[ "$R5" = "throttled-min-interval" ] && [ "$(stub_lines "$LOG5")" = "1" ] && pass "T5 min-interval" \
  || fail "T5 min-interval" "reason=$R5"

# ---------------------------------------------------------------- T6 同类每天上限
CFG6="$TMP/c6.yaml"; ROOT6="$TMP/r6"; LOG6="$TMP/s6.log"
mkcfg "$CFG6" "${BASE[@]}" max_per_day_per_kind=1 "command=['python3','$STUB','$LOG6','{summary}','{body}','{kind}']"
SEED_CFG="$CFG6"; seed "$ROOT6" 2 1
HUB_ROOT="$ROOT6" HUB_CONFIG="$CFG6" "${NOTIFY[@]}" --json dispatch >/dev/null
HUB_ROOT="$ROOT6" HUB_CONFIG="$CFG6" python3 "$SEED" 0 1 >/dev/null
OUT6="$(HUB_ROOT="$ROOT6" HUB_CONFIG="$CFG6" "${NOTIFY[@]}" --json dispatch)"
R6="$(printf '%s' "$OUT6" | json_get "d['reason']")"
[ "$R6" = "throttled-day" ] && [ "$(stub_lines "$LOG6")" = "1" ] && pass "T6 per-day-kind-cap" \
  || fail "T6 per-day-kind-cap" "reason=$R6"

# ---------------------------------------------------------------- T7 每小时上限
CFG7="$TMP/c7.yaml"; ROOT7="$TMP/r7"; LOG7="$TMP/s7.log"
mkcfg "$CFG7" "${BASE[@]}" max_per_hour=1 "command=['python3','$STUB','$LOG7','{summary}','{body}','{kind}']"
SEED_CFG="$CFG7"; seed "$ROOT7" 2 1
HUB_ROOT="$ROOT7" HUB_CONFIG="$CFG7" "${NOTIFY[@]}" --json dispatch >/dev/null
HUB_ROOT="$ROOT7" HUB_CONFIG="$CFG7" python3 "$SEED" 0 1 >/dev/null
OUT7="$(HUB_ROOT="$ROOT7" HUB_CONFIG="$CFG7" "${NOTIFY[@]}" --json dispatch)"
R7="$(printf '%s' "$OUT7" | json_get "d['reason']")"
[ "$R7" = "throttled-hour" ] && pass "T7 per-hour-cap" || fail "T7 per-hour-cap" "reason=$R7"

# ---------------------------------------------------------------- T8 勿扰（假 gsettings 走真解析路径）
mkdir -p "$TMP/fakebin"
printf '#!/bin/sh\necho false\n' > "$TMP/fakebin/gsettings"
printf '#!/bin/sh\necho "(uint64 0,)"\n' > "$TMP/fakebin/gdbus"
chmod +x "$TMP/fakebin/gsettings" "$TMP/fakebin/gdbus"
CFG8="$TMP/c8.yaml"; ROOT8="$TMP/r8"; LOG8="$TMP/s8.log"
mkcfg "$CFG8" "${BASE[@]}" dnd_source=gsettings "command=['python3','$STUB','$LOG8','{summary}','{body}','{kind}']"
SEED_CFG="$CFG8"; seed "$ROOT8" 1 1
OUT8="$(PATH="$TMP/fakebin:$PATH" HUB_ROOT="$ROOT8" HUB_CONFIG="$CFG8" "${NOTIFY[@]}" --json dispatch)"
R8="$(printf '%s' "$OUT8" | json_get "d['reason']")"
[ "$R8" = "dnd" ] && [ "$(stub_lines "$LOG8")" = "0" ] && pass "T8 dnd-blocks" \
  || fail "T8 dnd-blocks" "reason=$R8 out=$OUT8"

# ---------------------------------------------------------------- T9 空闲（人不在）
printf '#!/bin/sh\necho true\n' > "$TMP/fakebin/gsettings"
printf '#!/bin/sh\necho "(uint64 999999,)"\n' > "$TMP/fakebin/gdbus"
CFG9="$TMP/c9.yaml"; ROOT9="$TMP/r9"; LOG9="$TMP/s9.log"
mkcfg "$CFG9" "${BASE[@]}" dnd_source=gsettings idle_source=mutter idle_max_s=60 \
  "command=['python3','$STUB','$LOG9','{summary}','{body}','{kind}']"
SEED_CFG="$CFG9"; seed "$ROOT9" 1 1
OUT9="$(PATH="$TMP/fakebin:$PATH" HUB_ROOT="$ROOT9" HUB_CONFIG="$CFG9" "${NOTIFY[@]}" --json dispatch)"
R9="$(printf '%s' "$OUT9" | json_get "d['reason']")"
[ "$R9" = "idle" ] && [ "$(stub_lines "$LOG9")" = "0" ] && pass "T9 idle-blocks" \
  || fail "T9 idle-blocks" "reason=$R9 out=$OUT9"

# ---------------------------------------------------------------- T10 dry-run 不发不写
CFG10="$TMP/c10.yaml"; ROOT10="$TMP/r10"; LOG10="$TMP/s10.log"
mkcfg "$CFG10" "${BASE[@]}" "command=['python3','$STUB','$LOG10','{summary}','{body}','{kind}']"
SEED_CFG="$CFG10"; seed "$ROOT10" 1 1
OUT10="$(HUB_ROOT="$ROOT10" HUB_CONFIG="$CFG10" "${NOTIFY[@]}" --json --dry-run dispatch)"
W10="$(printf '%s' "$OUT10" | json_get "d.get('would_send')")"
PEND10="$(HUB_ROOT="$ROOT10" HUB_CONFIG="$CFG10" python3 -c "
from infohub.config import load
from infohub.store import Store
c=load(); s=Store(c.db_path)
print(len(s.pending_notify_todos(10)), sum(1 for r in s.notifications(10) if r['shown']))")"
[ "$W10" = "True" ] && [ "$(stub_lines "$LOG10")" = "0" ] && [ "$PEND10" = "1 0" ] \
  && pass "T10 dry-run" || fail "T10 dry-run" "would=$W10 stub=$(stub_lines "$LOG10") db=$PEND10"

# ---------------------------------------------------------------- T11 发送失败不标记（下轮还会试）
CFG11="$TMP/c11.yaml"; ROOT11="$TMP/r11"; LOG11="$TMP/s11.log"
mkcfg "$CFG11" "${BASE[@]}" "command=['python3','$STUB','$LOG11','{summary}','{body}','{kind}']"
SEED_CFG="$CFG11"; seed "$ROOT11" 1 1
OUT11="$(NOTIFY_STUB_EXIT=7 HUB_ROOT="$ROOT11" HUB_CONFIG="$CFG11" "${NOTIFY[@]}" --json dispatch)"
E11="$(printf '%s' "$OUT11" | json_get "bool(d.get('error'))")"
ST11="$(HUB_ROOT="$ROOT11" HUB_CONFIG="$CFG11" python3 -c "
from infohub.config import load
from infohub.store import Store
c=load(); s=Store(c.db_path)
print(len(s.pending_notify_todos(10)), s.notifications(1)[0]['shown'])")"
[ "$E11" = "True" ] && [ "$ST11" = "1 0" ] && pass "T11 send-failure-retryable" \
  || fail "T11 send-failure-retryable" "error=$E11 db=$ST11"

# ---------------------------------------------------------------- T12 status 摊开现状
OUT12="$(HUB_ROOT="$ROOT3" HUB_CONFIG="$CFG3" "${NOTIFY[@]}" --json status)"
K12="$(printf '%s' "$OUT12" | json_get "'|'.join(sorted(k for k in ('enabled','backend','pending_todos','shown_total','latest_window_event','ui_cursor') if k in d))")"
[ "$K12" = "backend|enabled|latest_window_event|pending_todos|shown_total|ui_cursor" ] \
  && pass "T12 status" || fail "T12 status" "keys=$K12"

# ---------------------------------------------------------------- T13 自检 test：真弹一条 + 台账 +1
# 判据（PROJECT.md §15 的 M2 行）：`hub-notify test` → 弹 1 条且台账 +1。
# 刻意 enabled=false：自检要回答"这台机器弹不弹得出来"，不能依赖开关、也不能依赖用户恰好切了界面。
CFG13="$TMP/c13.yaml"; ROOT13="$TMP/r13"; LOG13="$TMP/s13.log"
mkcfg "$CFG13" "enabled=false" "backend=command" "dnd_source=off" "idle_source=off" "require_ui_event=true" \
      "command=['python3','$STUB','$LOG13','{summary}','{body}','{kind}']"
OUT13="$(HUB_ROOT="$ROOT13" HUB_CONFIG="$CFG13" "${NOTIFY[@]}" --json test)"
S13="$(printf '%s' "$OUT13" | json_get "d['sent']")"
DB13="$(HUB_ROOT="$ROOT13" HUB_CONFIG="$CFG13" python3 -c "
from infohub.config import load
from infohub.store import Store
c=load(); s=Store(c.db_path)
r=s.notifications(1)[0]
print(r['kind'], r['shown'], r['todo_id'] is None, len(s.todos(50)))")"
[ "$S13" = "True" ] && [ "$(stub_lines "$LOG13")" = "1" ] && [ "$DB13" = "probe 1 True 0" ] \
  && pass "T13 自检：enabled=false 也真弹 + 台账 kind=probe / todo_id 空 / 不碰待办" \
  || fail "T13 自检" "sent=$S13 stub=$(stub_lines "$LOG13") db=$DB13"

# ---------------------------------------------------------------- T14 自检 --dry-run：不弹不记账
CFG14="$TMP/c14.yaml"; ROOT14="$TMP/r14"; LOG14="$TMP/s14.log"
mkcfg "$CFG14" "enabled=true" "backend=command" "dnd_source=off" "idle_source=off" \
      "command=['python3','$STUB','$LOG14','{summary}','{body}','{kind}']"
OUT14="$(HUB_ROOT="$ROOT14" HUB_CONFIG="$CFG14" "${NOTIFY[@]}" --json test --dry-run)"
S14="$(printf '%s' "$OUT14" | json_get "d['sent']")"
N14="$(HUB_ROOT="$ROOT14" HUB_CONFIG="$CFG14" python3 -c "
from infohub.config import load
from infohub.store import Store
c=load(); s=Store(c.db_path)
print(len(s.notifications(10)))")"
[ "$S14" = "False" ] && [ "$(stub_lines "$LOG14")" = "0" ] && [ "$N14" = "0" ] \
  && pass "T14 自检 --dry-run：没弹、台账 0 行" \
  || fail "T14 自检 --dry-run" "sent=$S14 stub=$(stub_lines "$LOG14") ledger=$N14"

# ---------------------------------------------------------------- T15 空闲解析（真机格式 uint64）
# 真机回的是 `(uint64 29866,)`。早期实现把位宽 "64" 也当数字吃掉 ⇒ 6429.866 秒 ⇒ 永远判"人不在"。
printf '#!/bin/sh\necho true\n' > "$TMP/fakebin/gsettings"
printf '#!/bin/sh\necho "(uint64 29866,)"\n' > "$TMP/fakebin/gdbus"
CFG15="$TMP/c15.yaml"; ROOT15="$TMP/r15"; LOG15="$TMP/s15.log"
mkcfg "$CFG15" "${BASE[@]}" dnd_source=gsettings idle_source=mutter idle_max_s=60 \
  "command=['python3','$STUB','$LOG15','{summary}','{body}','{kind}']"
SEED_CFG="$CFG15"; seed "$ROOT15" 1 1
OUT15="$(PATH="$TMP/fakebin:$PATH" HUB_ROOT="$ROOT15" HUB_CONFIG="$CFG15" "${NOTIFY[@]}" --json status)"
IDLE15="$(printf '%s' "$OUT15" | json_get "round(float(d['idle_s']), 3)")"
OUT15B="$(PATH="$TMP/fakebin:$PATH" HUB_ROOT="$ROOT15" HUB_CONFIG="$CFG15" "${NOTIFY[@]}" --json dispatch)"
S15="$(printf '%s' "$OUT15B" | json_get "d['sent']")"
[ "$IDLE15" = "29.866" ] && [ "$S15" = "1" ] \
  && pass "T15 空闲解析：uint64 29866ms ⇒ 29.866s（<60s ⇒ 人在，门 3 放行）" \
  || fail "T15 空闲解析" "idle=$IDLE15 sent=$S15"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
