#!/usr/bin/env bash
# info-hub M0 验收。判据见 INTERFACE.md §8：全过、退出码 0。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
COLLECT=(python3 -m infohub.entry_collect)
STORE=(python3 -m infohub.entry_store)

TMP="$(mktemp -d /tmp/infohub-m0-XXXXXX)"
export HUB_ROOT="$TMP/data"
WATCH="$TMP/watched"
mkdir -p "$WATCH" "$HUB_ROOT"
printf 'hello m0\n' > "$WATCH/a.md"
printf 'ignored\n'  > "$WATCH/b.tmp"

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }

json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# ---------------------------------------------------------------- T1 自检
if "${COLLECT[@]}" --selftest --quiet; then pass "T1 selftest"; else fail "T1 selftest" "exit=$?"; fi

# ---------------------------------------------------------------- T2 配置非法 ⇒ 退出码 1
HUB_CONFIG="$TMP/nope.yaml" "${COLLECT[@]}" --once --quiet >/dev/null 2>&1
[ $? -eq 1 ] && pass "T2 bad-config-exit1" || fail "T2 bad-config-exit1" "expected exit 1"

# ---------------------------------------------------------------- T3 真实采集（fs）
OUT1="$("${COLLECT[@]}" --once --source fs --json)"
N1="$(printf '%s' "$OUT1" | json_get "d['ingested']")"
[ "${N1:-0}" -ge 1 ] && pass "T3 collect-fs" || fail "T3 collect-fs" "ingested=$N1 out=$OUT1"

# ---------------------------------------------------------------- T4 游标抑制重复
OUT2="$("${COLLECT[@]}" --once --source fs --json)"
N2="$(printf '%s' "$OUT2" | json_get "d['ingested']")"
[ "${N2:-x}" = "0" ] && pass "T4 cursor-dedupe" || fail "T4 cursor-dedupe" "ingested=$N2"

# ---------------------------------------------------------------- T5 采集器失败不致命（PATH 里没有 wl-paste）
OUT5="$(env PATH=/nonexistent HUB_ROOT="$HUB_ROOT" /usr/bin/python3 -m infohub.entry_collect --once --source clipboard --json --quiet 2>/dev/null)"
RC5=$?
OK5="$(printf '%s' "$OUT5" | json_get "d['sources']['clipboard']['ok']")"
[ $RC5 -eq 0 ] && [ "$OK5" = "False" ] && pass "T5 collector-failure-not-fatal" \
  || fail "T5 collector-failure-not-fatal" "exit=$RC5 ok=$OK5"

# ---------------------------------------------------------------- T6 spool 重放（模拟崩溃后遗留）
python3 - "$HUB_ROOT/state/bus.jsonl" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)
ev = {"ts": "2026-10-15T00:00:00+08:00", "source": "spool", "kind": "replay",
      "payload": {"n": 1}, "fingerprint": "deadbeefdeadbeef", "privacy": "normal"}
p.write_text(json.dumps(ev, ensure_ascii=False) + "\n", encoding="utf-8")
PY
OUT6="$("${COLLECT[@]}" --once --source nosuchcollector --json)"
N6="$(printf '%s' "$OUT6" | json_get "d['ingested']")"
[ "${N6:-0}" = "1" ] && pass "T6 spool-replay" || fail "T6 spool-replay" "ingested=$N6 out=$OUT6"

# ---------------------------------------------------------------- T7 spool 已截断
SIZE7="$(stat -c %s "$HUB_ROOT/state/bus.jsonl")"
[ "$SIZE7" = "0" ] && pass "T7 spool-truncated" || fail "T7 spool-truncated" "size=$SIZE7"

# ---------------------------------------------------------------- T8 events 查询
OUT8="$("${STORE[@]}" events --limit 5 --json)"
COUNT8="$(printf '%s' "$OUT8" | json_get "len(d)")"
[ "${COUNT8:-0}" -ge 2 ] && pass "T8 events-query" || fail "T8 events-query" "rows=$COUNT8"

# ---------------------------------------------------------------- T9 stats
OUT9="$("${STORE[@]}" stats --json)"
TOTAL9="$(printf '%s' "$OUT9" | json_get "d['events_total']")"
[ "${TOTAL9:-0}" -ge 2 ] && pass "T9 stats" || fail "T9 stats" "total=$TOTAL9"

# ---------------------------------------------------------------- T10 prune 可跑
OUT10="$("${STORE[@]}" prune --json)"
DEL10="$(printf '%s' "$OUT10" | json_get "d['deleted']")"
[ -n "${DEL10:-}" ] && pass "T10 prune" || fail "T10 prune" "out=$OUT10"

# ---------------------------------------------------------------- T11 doctor 未就绪 ⇒ 退出码 1
cat > "$TMP/bad.config.yaml" <<YAML
root: $HUB_ROOT
db: hub.db
bus: state/bus.jsonl
reports_dir: reports
log_dir: logs
log_name: hub.log
retention_days: 180
sessions_root: $TMP/definitely-missing
collect:
  fs: {enabled: true, roots: [$WATCH]}
YAML
HUB_CONFIG="$TMP/bad.config.yaml" "${STORE[@]}" doctor --json >/dev/null 2>&1
[ $? -eq 1 ] && pass "T11 doctor-not-ready-exit1" || fail "T11 doctor-not-ready-exit1" "expected exit 1"
# ---------------------------------------------------------------- T12 doctor 真实配置（记录性，不判失败）
"${STORE[@]}" doctor --json > "$TMP/doctor.json" 2>/dev/null
python3 -c "import json;d=json.load(open('$TMP/doctor.json'));assert any(i['name']=='config' and i['ok'] for i in d['items'])"
[ $? -eq 0 ] && pass "T12 doctor-config-ok" || fail "T12 doctor-config-ok"

# ---------------------------------------------------------------- T13 并发锁：锁被占时本轮跳过且退出 0
python3 - "$HUB_ROOT/state/collect.lock" <<'PY' &
import fcntl, sys, time, pathlib
p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)
fh = p.open("a"); fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
time.sleep(8)
PY
HOLDER=$!
sleep 0.6
OUT13="$("${COLLECT[@]}" --once --source fs --json)"
RC13=$?
SKIP13="$(printf '%s' "$OUT13" | json_get "d.get('skipped')")"
kill "$HOLDER" 2>/dev/null; wait "$HOLDER" 2>/dev/null
[ $RC13 -eq 0 ] && [ "$SKIP13" = "locked" ] && pass "T13 concurrent-lock-skip" \
  || fail "T13 concurrent-lock-skip" "exit=$RC13 skipped=$SKIP13"

# ---------------------------------------------------------------- T14 剪贴板连续失败 ⇒ 冷却（不再每轮白等 timeout）
FAKEBIN="$TMP/fakebin"; mkdir -p "$FAKEBIN"
printf '#!/bin/sh\nexec /bin/sleep 30\n' > "$FAKEBIN/wl-paste"; chmod +x "$FAKEBIN/wl-paste"
CFG14="$TMP/c14.yaml"
python3 - "$ROOT/hub.config.yaml" "$CFG14" <<'PYX'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["collect"]["clipboard"].update({"timeout_s": 0.3, "backoff_after": 2, "backoff_s": 600})
for name in ("fs", "window", "dsh-session", "bilibili"):
    (raw["collect"].get(name) or {}).update({"enabled": False})
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PYX
run14() { env PATH="$FAKEBIN:/usr/bin:/bin" HUB_ROOT="$TMP/data14" \
          "${COLLECT[@]}" --config "$CFG14" --once --source clipboard --json 2>/dev/null; }
A14="$(run14)"; B14="$(run14)"
S="$(date +%s%N)"; D14="$(run14)"; E=$(( ($(date +%s%N) - S) / 1000000 ))
OKA="$(printf '%s' "$A14" | json_get "d['sources']['clipboard']['ok']")"
OKB="$(printf '%s' "$B14" | json_get "d['sources']['clipboard']['ok']")"
OKD="$(printf '%s' "$D14" | json_get "d['sources']['clipboard']['ok']")"
ERRB="$(printf '%s' "$B14" | json_get "d['sources']['clipboard']['error']")"
case "$OKA$OKB$OKD" in
  FalseFalseTrue)
    if [ "$E" -lt 250 ]; then pass "T14 clipboard-backoff"
    else fail "T14 clipboard-backoff" "第 3 轮仍耗时 ${E}ms（应被冷却立即跳过）"; fi ;;
  *) fail "T14 clipboard-backoff" "ok 序列=$OKA/$OKB/$OKD errB=$ERRB" ;;
esac

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
rm -rf "$TMP"
[ "$FAIL" -eq 0 ]
