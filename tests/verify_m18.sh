#!/usr/bin/env bash
# info-hub M18 验收：手机「游戏在前台」逐小时时段（usagestats 事件段配对）。
# 全程合成文本，不碰真手机。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"

TMP="$(mktemp -d /tmp/infohub-m18-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
R="$TMP/root"; CFG="$TMP/cfg.yaml"; mkdir -p "$R"
cp "$ROOT/hub.config.yaml" "$CFG"

cat > "$TMP/us.txt" <<'EOF'
Stats since: 0ns
user=0
  Last 24 hour events (timeRange="2026/10/27 10:42 – 2026/10/28 10:42" )
    time="2026-10-27 14:04:00" type=ACTIVITY_RESUMED package=kajfosz.antimatterdimensions class=x launchCount=1
    time="2026-10-27 14:04:00" type=ACTIVITY_RESUMED package=com.tencent.mm class=y launchCount=1
    time="2026-10-27 15:11:00" type=ACTIVITY_PAUSED package=kajfosz.antimatterdimensions class=x
    time="2026-10-27 15:12:00" type=ACTIVITY_RESUMED package=kajfosz.antimatterdimensions class=x
    time="2026-10-27 16:56:00" type=ACTIVITY_PAUSED package=kajfosz.antimatterdimensions class=x
    time="2026-10-27 14:10:00" type=ACTIVITY_PAUSED package=com.tencent.mm class=y
    time="2026-10-27 17:00:00" type=ACTIVITY_RESUMED package=com.tencent.tmgp.pubgmhd class=z
  timeRange="2026/10/27 17:04 – 2026/10/28 10:41" 
    packages
      package=kajfosz.antimatterdimensions totalTimeUsed="10:00" lastTimeUsed="2026-10-28 10:40:00" totalTimeVisible="10:00" lastTimeVisible="2026-10-28 10:40:00" appLaunchCount=3
EOF

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
ck() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "期望 $3，实际 $2"; fi; }

py() { HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - "$@" <<'PY'
import json, sys
from datetime import datetime
from infohub.config import load
from infohub.store import Store
from infohub import sources
P = sources.load_module("phone-usage")

cmd, path = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
games = ["kajfosz.antimatterdimensions", "com.tencent.tmgp"]
store = Store(load().db_path); store.migrate()

if cmd == "hours":
    now = datetime(2026, 10, 27, 17, 30, 0)
    print(json.dumps(P.parse_hours(text, games, now=now), ensure_ascii=False, sort_keys=True))
elif cmd == "hours_nogames":
    print(json.dumps(P.parse_hours(text, [], now=datetime(2026, 10, 27, 17, 30, 0)), ensure_ascii=False, sort_keys=True))
PY
}

echo "== T1 事件段配对 → 小时桶 =="
H1="$(py hours "$TMP/us.txt")"
ck "T1a 14 点游戏 59 分（14:04–15:11 跨整点切分）" "$(printf '%s' "$H1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hours"]["2026-10-27T14"]["kajfosz.antimatterdimensions"])')" "56.0"
ck "T1b 15 点游戏 60 分（15:00–15:11 + 15:12–16:00）" "$(printf '%s' "$H1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hours"]["2026-10-27T15"]["kajfosz.antimatterdimensions"])')" "59.0"
ck "T1c 16 点游戏 56 分" "$(printf '%s' "$H1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hours"]["2026-10-27T16"]["kajfosz.antimatterdimensions"])')" "56.0"
ck "T1d 未闭合段按 now 截止（17:00–17:30 = 30 分）" "$(printf '%s' "$H1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hours"]["2026-10-27T17"]["com.tencent.tmgp.pubgmhd"])')" "30.0"

echo "== T2 非游戏包不进结果 =="
ck "T2 com.tencent.mm 不在小时桶里" "$(printf '%s' "$H1" | grep -c 'com.tencent.mm' || true)" "0"

echo "== T3 白名单为空 ⇒ 什么都不算（不猜游戏）=="
H3="$(py hours_nogames "$TMP/us.txt")"
ck "T3 hours 为空" "$(printf '%s' "$H3" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hours"])')" "{}"

echo "== T4 前缀匹配 =="
ck "T4 com.tencent.tmgp 前缀命中 pubgmhd" "$(printf '%s' "$H1" | grep -c 'pubgmhd' || true)" "1"

echo "== T5 features 的 phone_game_min =="
HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - > "$TMP/t5.out" 2>&1 <<'PY'
from infohub.config import load
from infohub.store import Store
from derived_mod import load as dload
from infohub import events as E
F = dload("features")
s = Store(load().db_path); s.migrate()
s.insert_events([E.make_event("phone-usage", "phone.usage.hours",
    {"day": "2026-10-27", "games": ["x"],
     "hours": {"2026-10-27T14": {"x": 40.0}, "2026-10-27T15": {"x": 5.0}}},
    ts="2026-10-28T10:00:00")])
rows = {r["hour"]: r for r in F.build(s, "2026-10-27")}
print(rows[14]["phone_game_min"], rows[15]["phone_game_min"], rows[16]["phone_game_min"])
PY
ck "T5a 14 点 40 分 / 15 点 5 分 / 16 点 NULL" "$(cat "$TMP/t5.out")" "40 5 None"

echo "== T6 跨零点的小时键归到事件那一天 =="
HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - > "$TMP/t6.out" 2>&1 <<'PY'
from infohub.config import load
from infohub.store import Store
from derived_mod import load as dload
F = dload("features")
s = Store(load().db_path); s.migrate()
rows = {r["hour"]: r for r in F.build(s, "2026-10-28")}
print(rows[0]["phone_game_min"], rows[14]["phone_game_min"])
PY
ck "T6 10-28 的 0 点没有游戏数据（不串到 10-27 的 14 点）" "$(cat "$TMP/t6.out")" "None None"

echo "== T7 扣掉手机游戏（clean_minutes）=="
t7="$(PYTHONPATH="$ROOT/src:$ROOT/tests" python3 -c '
from infohub import behavior as B
ep=[{"started_at":"2026-10-27T10:00:00","ended_at":"2026-10-27T11:00:00","active_s":3600}]
print(B.clean_minutes(ep, {10: 60})) 
print(B.clean_minutes(ep, {10: 30}))
print(B.clean_minutes(ep, {10: 15}))
print(B.clean_minutes(ep, {}))')"
ck "T7a 整小时挂游戏 ⇒ 净 0"  "$(printf '%s' "$t7" | sed -n 1p | python3 -c 'import sys,ast;print(ast.literal_eval(sys.stdin.read())["clean"])')" "0"
ck "T7b 挂 30 分 ⇒ 扣 30 净 30" "$(printf '%s' "$t7" | sed -n 2p | python3 -c 'import sys,ast;print(ast.literal_eval(sys.stdin.read())["clean"])')" "30"
ck "T7c 挂 15 分 ⇒ 扣 15 净 45" "$(printf '%s' "$t7" | sed -n 3p | python3 -c 'import sys,ast;print(ast.literal_eval(sys.stdin.read())["clean"])')" "45"
ck "T7d 没有游戏数据 ⇒ 一分不扣" "$(printf '%s' "$t7" | sed -n 4p | python3 -c 'import sys,ast;print(ast.literal_eval(sys.stdin.read())["clean"])')" "60"

printf '\nM18: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
