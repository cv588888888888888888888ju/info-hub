#!/usr/bin/env bash
# info-hub M2f 验收：通知的**触发面** —— 只有这一轮真的收进了新的 window 事件，才评估一次通知。
# 判据见 INTERFACE.md §4.6「触发面」/ PROJECT.md §34。
# 全程不弹真窗口：backend=command 走 tests/notifier_stub.py；采集器换成假注册表（$FIX）。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
COLLECT=(python3 -m infohub.entry_collect)
STUB="$HERE/notifier_stub.py"
SEED="$HERE/m3c_seed.py"

TMP="$(mktemp -d /tmp/infohub-m2f-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
stub_lines() { [ -f "$1" ] && wc -l < "$1" || echo 0; }

# 假采集器：注册表被换成单个 "fake"，它每次发出 1 条 window 事件 / 1 条 fs 事件 / 什么都没有。
FIX="$TMP/fake_round.py"
cat > "$FIX" <<'PY'
import os, sys
import infohub.sources as C
from infohub.events import make_event

kind = sys.argv[1]                 # window | fs | empty
args = sys.argv[2:]

def fake(config, store):
    if kind == "empty":
        return []
    if kind == "window":
        return [make_event("window", "window.snapshot",
                           {"frames": [{"app": "msedge", "title": "M2f " + os.urandom(4).hex()}],
                            "count": 1})]
    return [make_event("fs", "fs.change",
                       {"root": "/tmp", "path": "m2f/f-" + os.urandom(4).hex() + ".py",
                        "mtime": 0, "size": 1})]

C.registry = lambda config=None, **kw: {"fake": fake}
import infohub.cli as cli
sys.exit(cli.main_collect(args))
PY

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

# 种子：造真事件（3 条同文件 fs + 3 条同域剪贴板 + 1 条 window）→ 规则自己产出带证据的待办。
seed() {  # seed <root> <cfg>
  HUB_ROOT="$1" HUB_CONFIG="$2" python3 "$SEED" >/dev/null
  HUB_ROOT="$1" HUB_CONFIG="$2" python3 -m infohub.entry_rules --quiet derive >/dev/null
}

BASE=(enabled=true backend=command dnd_source=off idle_source=off require_ui_event=true
      min_interval_s=0 max_per_hour=0 max_per_day_per_kind=0)

# ---------------------------------------------------------------- T1 不带 --notify ⇒ 不评估（显式 opt-in）
CFG1="$TMP/c1.yaml"; ROOT1="$TMP/r1"; LOG1="$TMP/s1.log"
mkcfg "$CFG1" "${BASE[@]}" "command=['python3','$STUB','$LOG1','{summary}','{body}','{kind}']"
seed "$ROOT1" "$CFG1"
OUT1="$(HUB_ROOT="$ROOT1" HUB_CONFIG="$CFG1" python3 "$FIX" window --once --source fake --json)"
W1="$(printf '%s' "$OUT1" | json_get "d.get('window_new')")"
N1="$(printf '%s' "$OUT1" | json_get "'notify' in d")"
[ "$W1" = "1" ] && [ "$N1" = "False" ] && [ "$(stub_lines "$LOG1")" = "0" ] \
  && pass "T1 不带 --notify：事件照收（window_new=1）但不评估通知" \
  || fail "T1 opt-in" "window_new=$W1 has_notify=$N1 stub=$(stub_lines "$LOG1")"

# ---------------------------------------------------------------- T2 带 --notify + 这轮收了 window 事件 ⇒ 评估并弹 1 条
OUT2="$(HUB_ROOT="$ROOT1" HUB_CONFIG="$CFG1" python3 "$FIX" window --once --source fake --json --notify)"
RC2=$?
S2="$(printf '%s' "$OUT2" | json_get "d['notify']['sent']")"
[ "$RC2" = "0" ] && [ "$S2" = "1" ] && [ "$(stub_lines "$LOG1")" = "1" ] \
  && pass "T2 收了新 window 事件 ⇒ 采完顺手评估一次（sent=1，退出码仍 0）" \
  || fail "T2 trigger-fires" "rc=$RC2 sent=$S2 stub=$(stub_lines "$LOG1") out=$OUT2"

# ---------------------------------------------------------------- T3 这轮只收 fs 事件 ⇒ 不评估（触发点钉在界面事件上）
CFG3="$TMP/c3.yaml"; ROOT3="$TMP/r3"; LOG3="$TMP/s3.log"
mkcfg "$CFG3" "${BASE[@]}" "command=['python3','$STUB','$LOG3','{summary}','{body}','{kind}']"
seed "$ROOT3" "$CFG3"
OUT3="$(HUB_ROOT="$ROOT3" HUB_CONFIG="$CFG3" python3 "$FIX" fs --once --source fake --json --notify)"
W3="$(printf '%s' "$OUT3" | json_get "d.get('window_new')")"
R3="$(printf '%s' "$OUT3" | json_get "d['notify']['reason']")"
[ "$W3" = "0" ] && [ "$R3" = "no-new-ui-event" ] && [ "$(stub_lines "$LOG3")" = "0" ] \
  && pass "T3 只收 fs 事件：window_new=0 ⇒ reason=no-new-ui-event，不弹" \
  || fail "T3 fs-does-not-trigger" "window_new=$W3 reason=$R3 stub=$(stub_lines "$LOG3")"

# ---------------------------------------------------------------- T4 库里本来就有 window 事件、但这轮没有新的 ⇒ 仍不评估
CFG4="$TMP/c4.yaml"; ROOT4="$TMP/r4"; LOG4="$TMP/s4.log"
mkcfg "$CFG4" "${BASE[@]}" "command=['python3','$STUB','$LOG4','{summary}','{body}','{kind}']"
seed "$ROOT4" "$CFG4"     # 种子里已经有 1 条 window 事件
OUT4="$(HUB_ROOT="$ROOT4" HUB_CONFIG="$CFG4" python3 "$FIX" empty --once --source fake --json --notify)"
W4="$(printf '%s' "$OUT4" | json_get "d.get('window_new')")"
R4="$(printf '%s' "$OUT4" | json_get "d['notify']['reason']")"
[ "$W4" = "0" ] && [ "$R4" = "no-new-ui-event" ] && [ "$(stub_lines "$LOG4")" = "0" ] \
  && pass "T4 库里有旧 window 事件不算数：看的是**这一轮**有没有新的" \
  || fail "T4 stale-ui-event" "window_new=$W4 reason=$R4 stub=$(stub_lines "$LOG4")"

# ---------------------------------------------------------------- T5 收敛：池子里的待办弹完就不再弹（每次仍只弹 1 条）
# 种子里有 2 条待办（fs-repeat + clipboard-domain-repeat）⇒ T2 弹掉 1 条，这里再弹 1 条，然后归零。
OUT5="$(HUB_ROOT="$ROOT1" HUB_CONFIG="$CFG1" python3 "$FIX" window --once --source fake --json --notify)"
S5="$(printf '%s' "$OUT5" | json_get "d['notify']['sent']")"
OUT5B="$(HUB_ROOT="$ROOT1" HUB_CONFIG="$CFG1" python3 "$FIX" window --once --source fake --json --notify)"
S5B="$(printf '%s' "$OUT5B" | json_get "d['notify']['sent']")"
[ "$S5" = "1" ] && [ "$S5B" = "0" ] && [ "$(stub_lines "$LOG1")" = "2" ] \
  && pass "T5 收敛：2 条待办各弹 1 次后归零（stub 2 行，再跑 sent=0）" \
  || fail "T5 convergence" "sent=$S5 sent3=$S5B lines=$(stub_lines "$LOG1")"

# ---------------------------------------------------------------- T6 开关仍是门 1：enabled=false ⇒ 评估了但不弹
CFG6="$TMP/c6.yaml"; ROOT6="$TMP/r6"; LOG6="$TMP/s6.log"
mkcfg "$CFG6" "enabled=false" backend=command dnd_source=off idle_source=off require_ui_event=true \
      "command=['python3','$STUB','$LOG6','{summary}','{body}','{kind}']"
seed "$ROOT6" "$CFG6"
OUT6="$(HUB_ROOT="$ROOT6" HUB_CONFIG="$CFG6" python3 "$FIX" window --once --source fake --json --notify)"
R6="$(printf '%s' "$OUT6" | json_get "d['notify']['reason']")"
W6="$(printf '%s' "$OUT6" | json_get "d.get('window_new')")"
[ "$W6" = "1" ] && [ "$R6" = "disabled" ] && [ "$(stub_lines "$LOG6")" = "0" ] \
  && pass "T6 notify.enabled=false：触发面照走，门 1 挡住（reason=disabled）" \
  || fail "T6 gate1" "window_new=$W6 reason=$R6 stub=$(stub_lines "$LOG6")"

# ---------------------------------------------------------------- T7 采集失败/无事件都不影响退出码
RC7=0
HUB_ROOT="$ROOT6" HUB_CONFIG="$CFG6" python3 "$FIX" empty --once --source fake --quiet --notify >/dev/null 2>&1 || RC7=$?
[ "$RC7" = "0" ] && pass "T7 没有新事件时退出码仍 0" || fail "T7 exit-code" "rc=$RC7"

# ---------------------------------------------------------------- T8 装的单元真的带 --notify
[ "$(grep -c -- '--notify' "$ROOT/packaging/info-hub-collect.service")" -ge 1 ] \
  && pass "T8 info-hub-collect.service 的 ExecStart 带 --notify" \
  || fail "T8 unit-wiring" "packaging/info-hub-collect.service 缺 --notify"

# ---------------------------------------------------------------- T9 帮助面：--notify 出现在用法里
HUB_ROOT="$ROOT1" HUB_CONFIG="$CFG1" python3 -m infohub.entry_collect --help 2>&1 | grep -q -- '--notify' \
  && pass "T9 hub-collect --help 里有 --notify" || fail "T9 help" "--help 没列 --notify"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
