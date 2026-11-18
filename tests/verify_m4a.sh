#!/usr/bin/env bash
# info-hub M4a 验收：通知有效性统计 + 自动降频（门 5b）。判据见 INTERFACE.md §4.6 / PROJECT.md §9。
# 全程离线：合成通知历史 + backend=command 走 tests/notifier_stub.py，不弹真窗口。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
NOTIFY=(python3 -m infohub.entry_notify)
SEED="$HERE/m4a_seed.py"
STUB="$HERE/notifier_stub.py"

TMP="$(mktemp -d /tmp/infohub-m4a-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
stub_lines() { [ -f "$1" ] && wc -l < "$1" || echo 0; }

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

seed() {  # seed <cfg> <root> <kind> <acted> <ignored> <fresh> <today> <cand> [extra]
  local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" python3 "$SEED" "$@" >/dev/null
}

eff() {  # eff <cfg> <root> [extra argv...]
  local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" "${NOTIFY[@]}" --json effectiveness "$@"
}

CFG="$TMP/c.yaml"
cp "$ROOT/hub.config.yaml" "$CFG"

# ---------------------------------------------------------------- T1 忽略率高 ⇒ 降频
R1="$TMP/r1"; seed "$CFG" "$R1" fs-repeat 2 6 0 0 0
O1="$(eff "$CFG" "$R1")"
S="$(printf '%s' "$O1" | json_get "[r for r in d['kinds'] if r['kind']=='fs-repeat'][0]['samples']")"
RATE="$(printf '%s' "$O1" | json_get "[r for r in d['kinds'] if r['kind']=='fs-repeat'][0]['ignored_rate']")"
DOWN="$(printf '%s' "$O1" | json_get "[r for r in d['kinds'] if r['kind']=='fs-repeat'][0]['downshifted']")"
[ "$S" = "8" ] && [ "$RATE" = "0.75" ] && [ "$DOWN" = "True" ] && pass "T1 降频判定 samples=8 rate=0.75" \
  || fail "T1 降频判定" "samples=$S rate=$RATE down=$DOWN"

# ---------------------------------------------------------------- T2 响应率高 ⇒ 不降频
R2="$TMP/r2"; seed "$CFG" "$R2" session-unfinished 5 0 0 0 0
O2="$(eff "$CFG" "$R2")"
D2="$(printf '%s' "$O2" | json_get "[r for r in d['kinds'] if r['kind']=='session-unfinished'][0]['downshifted']")"
R2RATE="$(printf '%s' "$O2" | json_get "[r for r in d['kinds'] if r['kind']=='session-unfinished'][0]['ignored_rate']")"
[ "$D2" = "False" ] && [ "$R2RATE" = "0.0" ] && pass "T2 高响应不降频" \
  || fail "T2 高响应不降频" "down=$D2 rate=$R2RATE"

# ---------------------------------------------------------------- T3 样本不足 ⇒ 不降频
R3="$TMP/r3"; seed "$CFG" "$R3" fs-burst 0 2 0 0 0
O3="$(eff "$CFG" "$R3")"
D3="$(printf '%s' "$O3" | json_get "[r for r in d['kinds'] if r['kind']=='fs-burst'][0]['downshifted']")"
S3="$(printf '%s' "$O3" | json_get "[r for r in d['kinds'] if r['kind']=='fs-burst'][0]['samples']")"
[ "$D3" = "False" ] && [ "$S3" = "2" ] && pass "T3 样本不足不降频" \
  || fail "T3 样本不足不降频" "down=$D3 samples=$S3"

# ---------------------------------------------------------------- T4 判定期内不算忽略
R4="$TMP/r4"; seed "$CFG" "$R4" clipboard-same-domain 0 0 6 0 0
O4="$(eff "$CFG" "$R4")"
P4="$(printf '%s' "$O4" | json_get "[r for r in d['kinds'] if r['kind']=='clipboard-same-domain'][0]['pending']")"
D4="$(printf '%s' "$O4" | json_get "[r for r in d['kinds'] if r['kind']=='clipboard-same-domain'][0]['downshifted']")"
[ "$P4" = "6" ] && [ "$D4" = "False" ] && pass "T4 判定期内 pending=6" \
  || fail "T4 判定期内" "pending=$P4 down=$D4"

# ---------------------------------------------------------------- T5 老数据无指针 ⇒ 不进样本
R5="$TMP/r5"; seed "$CFG" "$R5" fs-legacy 0 0 0 0 0 unlinked
O5="$(eff "$CFG" "$R5")"
U5="$(printf '%s' "$O5" | json_get "[r for r in d['kinds'] if r['kind']=='fs-legacy'][0]['unlinked']")"
S5="$(printf '%s' "$O5" | json_get "[r for r in d['kinds'] if r['kind']=='fs-legacy'][0]['samples']")"
D5="$(printf '%s' "$O5" | json_get "[r for r in d['kinds'] if r['kind']=='fs-legacy'][0]['downshifted']")"
[ "$U5" = "6" ] && [ "$S5" = "0" ] && [ "$D5" = "False" ] && pass "T5 无指针不进样本" \
  || fail "T5 无指针不进样本" "unlinked=$U5 samples=$S5 down=$D5"

# ---------------------------------------------------------------- T6 降频门生效（5b）
BASE=(enabled=true backend=command dnd_source=off idle_source=off require_ui_event=false
      min_interval_s=0 max_per_hour=100 max_per_day_per_kind=3)
CFG6="$TMP/c6.yaml"; R6="$TMP/r6"; L6="$TMP/s6.log"
mkcfg "$CFG6" "${BASE[@]}" "command=['python3','$STUB','$L6','{summary}','{body}','{kind}']"
seed "$CFG6" "$R6" fs-repeat 2 6 0 1 1
O6="$(HUB_ROOT="$R6" HUB_CONFIG="$CFG6" "${NOTIFY[@]}" --json dispatch)"
RE6="$(printf '%s' "$O6" | json_get "d['reason']")"
RD6="$(printf '%s' "$O6" | json_get "d.get('downshifted',{}).get('ignored_rate')")"
[ "$RE6" = "throttled-downshifted" ] && [ "$RD6" = "0.75" ] && [ "$(stub_lines "$L6")" = "0" ] \
  && pass "T6 降频门拦住（reason=throttled-downshifted）" \
  || fail "T6 降频门" "reason=$RE6 rate=$RD6 stub=$(stub_lines "$L6") out=$O6"

# ---------------------------------------------------------------- T7 对照：阈值调高 ⇒ 正常弹
CFG7="$TMP/c7.yaml"; R7="$TMP/r7"; L7="$TMP/s7.log"
mkcfg "$CFG7" "${BASE[@]}" "downshift_threshold=0.9" \
  "command=['python3','$STUB','$L7','{summary}','{body}','{kind}']"
seed "$CFG7" "$R7" fs-repeat 2 6 0 1 1
O7="$(HUB_ROOT="$R7" HUB_CONFIG="$CFG7" "${NOTIFY[@]}" --json dispatch)"
S7="$(printf '%s' "$O7" | json_get "d.get('sent',0)")"
[ "$S7" = "1" ] && [ "$(stub_lines "$L7")" = "1" ] && pass "T7 阈值 0.9 不降频 ⇒ 正常弹" \
  || fail "T7 对照不降频" "sent=$S7 stub=$(stub_lines "$L7") out=$O7"

# ---------------------------------------------------------------- T8 status 摊开降频类别
O8="$(HUB_ROOT="$R6" HUB_CONFIG="$CFG6" "${NOTIFY[@]}" --json status)"
D8="$(printf '%s' "$O8" | json_get "'fs-repeat' in d.get('downshifted',[])")"
[ "$D8" = "True" ] && pass "T8 status 列出降频类别" || fail "T8 status 降频类别" "out=$O8"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
