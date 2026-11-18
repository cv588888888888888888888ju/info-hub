#!/usr/bin/env bash
# info-hub M17 验收：手机屏幕使用时间（dumpsys usagestats 日桶）。
# 全程合成文本 + 假 adb，不碰真手机、不碰真库。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"

TMP="$(mktemp -d /tmp/infohub-m17-XXXXXX)"
DAY="2026-10-28"
trap 'rm -rf "$TMP"' EXIT
R="$TMP/root"; CFG="$TMP/cfg.yaml"; mkdir -p "$R"
python3 - "$ROOT/hub.config.yaml" "$CFG" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name, node in raw["collect"].items():
    if isinstance(node, dict) and "enabled" in node:
        node["enabled"] = name == "phone-usage"
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY

# 合成的 dumpsys usagestats：日桶 3 条有效 + 1 条缺 visible；周桶里有一条诱饵
cat > "$TMP/us.txt" <<'EOF'
Stats since: 0ns
user=0
  Last 24 hour events (timeRange="2026/10/27 10:42 – 2026/10/28 10:42" )
    time="2026-10-28 09:12:00" type=ACTIVITY_RESUMED package=com.example.a class=x launchCount=1
  timeRange="2026/10/27 17:04 – 2026/10/28 10:41" 
    packages
      package=kajfosz.antimatterdimensions totalTimeUsed="14:43" lastTimeUsed="2026-10-28 10:40:00" totalTimeVisible="14:43" lastTimeVisible="2026-10-28 10:40:00" appLaunchCount=3
      package=tv.danmaku.bili totalTimeUsed="02:22" lastTimeUsed="2026-10-28 09:00:00" totalTimeVisible="02:22" lastTimeVisible="2026-10-28 09:00:00" appLaunchCount=7
      package=com.example.c totalTimeUsed="00:30:00" lastTimeUsed="2026-10-28 08:00:00" totalTimeVisible="00:30:00" lastTimeVisible="2026-10-28 08:00:00" appLaunchCount=1
      package=com.example.noVisible totalTimeUsed="01:00" lastTimeUsed="2026-10-28 07:00:00" lastTimeVisible="1970-01-01 08:00:00" appLaunchCount=0
    Database Summary:
  timeRange="2026/10/25 17:03 – 2026/10/28 10:41" 
    packages
      package=com.decoy.weekly totalTimeUsed="99:00" lastTimeUsed="2026-10-28 10:41:00" totalTimeVisible="99:00" lastTimeVisible="2026-10-28 10:41:00" appLaunchCount=9
EOF

# 假 adb：`<prog> shell dumpsys usagestats` ⇒ 打印上面那份文本
cat > "$TMP/adb-ok" <<EOF
#!/bin/sh
cat "$TMP/us.txt"
EOF
cat > "$TMP/adb-fail" <<'EOF'
#!/bin/sh
echo "error: no devices/emulators found" >&2
exit 3
EOF
chmod +x "$TMP/adb-ok" "$TMP/adb-fail"

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
ck() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "期望 $3，实际 $2"; fi; }

py() { HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - "$@" <<'PY'
import json, os, sys
from infohub.config import load
from infohub.store import Store
from infohub import events as E
from infohub.sources import CollectorError
from infohub import sources
P = sources.load_module("phone-usage")

cmd = sys.argv[1]; text_path = sys.argv[2] if len(sys.argv) > 2 else ""
text = open(text_path, encoding="utf-8").read() if os.path.exists(text_path) else ""
cfg = load(); store = Store(cfg.db_path); store.migrate()

def show(obj):
    print(json.dumps(obj, ensure_ascii=False))

if cmd == "parse":
    show(P.parse(text) or {})
elif cmd == "parse_topn":
    show(P.parse(text, top_n=2) or {})
elif cmd == "collect":
    cfg.raw["collect"]["phone-usage"]["adb"] = sys.argv[3]
    if len(sys.argv) > 4 and sys.argv[4] == "fresh":     # 清掉节流游标，专测失败语义
        store.conn.execute("delete from collector_state where key='phone-usage'"); store.conn.commit()
    cfg.raw["collect"]["phone-usage"]["enabled"] = True
    try:
        evs = P.collect(cfg, store)
    except CollectorError as exc:
        show({"error": str(exc)}); raise SystemExit
    n = store.insert_events(evs)[0] if evs else 0
    show({"events": len(evs), "inserted": n,
          "payload": evs[0]["payload"] if evs else None})
elif cmd == "collect_twice":                     # 节流：同一轮里第二次必须空
    cfg.raw["collect"]["phone-usage"]["adb"] = sys.argv[3]
    cfg.raw["collect"]["phone-usage"]["enabled"] = True
    first = len(P.collect(cfg, store))
    second = len(P.collect(cfg, store))
    show({"first": first, "second": second})
elif cmd == "count":
    print(store.conn.execute("select count(*) from events where source=?", ("phone-usage",)).fetchone()[0])
elif cmd == "features":
    store.insert_events([E.make_event("phone-usage", "phone.usage",
        {"day": "2026-10-29", "bucket": "x", "total_min": 1226.0, "count": 2,
         "packages": [{"app": "kajfosz.antimatterdimensions", "min": 883.1},
                      {"app": "tv.danmaku.bili", "min": 141.7}]}, ts="2026-10-29T10:41:00")])
    from derived_mod import load as dload
    F = dload("features")
    a = F.build(store, "2026-10-29"); store.features_replace("2026-10-29", "feat-1", a)
    b = F.build(store, "2026-10-29"); store.features_replace("2026-10-29", "feat-1", b)
    rows = store.features_day("2026-10-29", "feat-1")
    show({"min": rows[0]["phone_day_min"], "top": rows[0]["phone_day_top"],
          "all_same": len({r["phone_day_min"] for r in rows}) == 1,
          "idempotent": json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)})
PY
}

echo "== T1 日桶解析 =="
R1="$(py parse "$TMP/us.txt")"
ck "T1a 日期取桶结束日" "$(printf '%s' "$R1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["day"])')" "2026-10-28"
ck "T1b 有效包数（缺 visible 的不算）" "$(printf '%s' "$R1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])')" "3"
ck "T1c 合计分钟（14:43=14.73 + 02:22=2.37 + 00:30:00=30.0）" "$(printf '%s' "$R1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["total_min"])')" "47.1"
ck "T1d 榜首应用（30 分那条）" "$(printf '%s' "$R1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["packages"][0]["app"])')" "com.example.c"

echo "== T2 周桶/诱饵不进结果 =="
ck "T2 没有 com.decoy.weekly" "$(printf '%s' "$R1" | grep -c 'decoy' || true)" "0"

echo "== T3 时长两种写法一致 =="
ck "T3 三段式 00:30:00 = 30.0 分" "$(printf '%s' "$R1" | python3 -c 'import sys,json;d=json.load(sys.stdin);print([p["min"] for p in d["packages"] if p["app"]=="com.example.c"][0])')" "30.0"

echo "== T4 缺 visible 的行被跳过（不是记 0）=="
ck "T4 com.example.noVisible 不在列表" "$(printf '%s' "$R1" | grep -c 'noVisible' || true)" "0"

echo "== T5 top_n 截断 =="
R5="$(py parse_topn "$TMP/us.txt")"
ck "T5a payload 只留 2 个" "$(printf '%s' "$R5" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["packages"]))')" "2"
ck "T5b count 仍是总数 3" "$(printf '%s' "$R5" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])')" "3"

echo "== T6 采集落库 + 节流 =="
R6="$(py collect "$TMP/us.txt" "$TMP/adb-ok")"
ck "T6a 落了 1 条" "$(printf '%s' "$R6" | python3 -c 'import sys,json;print(json.load(sys.stdin)["inserted"])')" "1"
R6b="$(py collect_twice "$TMP/us.txt" "$TMP/adb-ok")"
ck "T6b 第二次被节流返回空" "$(printf '%s' "$R6b" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["first"],d["second"])')" "0 0"

echo "== T7 失败语义：不落半条 =="
R7="$(py collect "$TMP/us.txt" "$TMP/adb-fail" fresh)"
ck "T7a rc!=0 ⇒ CollectorError" "$(printf '%s' "$R7" | python3 -c 'import sys,json;print("error" in json.load(sys.stdin))')" "True"
ck "T7b 失败后行数不变（仍只有 T6 那 1 条）" "$(py count "$TMP/us.txt")" "1"
R7c="$(py collect "$TMP/us.txt" "$TMP/missing-adb" fresh)"
ck "T7c 找不到 adb ⇒ CollectorError" "$(printf '%s' "$R7c" | python3 -c 'import sys,json;print("找不到 adb" in json.load(sys.stdin).get("error",""))')" "True"

echo "== T8 features 两列 =="
R8="$(py features)"
ck "T8a phone_day_min=1226" "$(printf '%s' "$R8" | python3 -c 'import sys,json;print(json.load(sys.stdin)["min"])')" "1226"
ck "T8b phone_day_top 是包名" "$(printf '%s' "$R8" | python3 -c 'import sys,json;print(json.load(sys.stdin)["top"])')" "kajfosz.antimatterdimensions"
ck "T8c 整天每行同值" "$(printf '%s' "$R8" | python3 -c 'import sys,json;print(json.load(sys.stdin)["all_same"])')" "True"
ck "T8d 两次 build 逐字节相同" "$(printf '%s' "$R8" | python3 -c 'import sys,json;print(json.load(sys.stdin)["idempotent"])')" "True"

echo "== T9 没有快照的日子是 NULL =="
r9="$(HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 -c '
from infohub.config import load; from derived_mod import load as dload; from infohub.store import Store
F = dload("features")
s=Store(load().db_path); rows=F.build(s,"2026-10-20")
print(all(r["phone_day_min"] is None and r["phone_day_top"] is None for r in rows))')"
ck "T9 无快照 ⇒ 全 NULL" "$r9" "True"

echo "== T10 配置键齐全 =="
for k in enabled adb serial interval_s top_n timeout_s; do
  if grep -q "^    $k:" "$ROOT/hub.config.yaml" 2>/dev/null || python3 -c "
import sys, yaml
d = yaml.safe_load(open('$ROOT/hub.config.yaml', encoding='utf-8'))
sys.exit(0 if '$k' in d['collect']['phone-usage'] else 1)"; then
    pass "T10 $k 在"
  else
    fail "T10 $k 在" "缺"
  fi
done

echo "== T11 show 里先报一行手机屏幕 =="
t11="$(PYTHONPATH="$ROOT/src:$ROOT/tests" python3 -c '
import io, contextlib
from infohub.cli import _phone_day_line
b=io.StringIO()
with contextlib.redirect_stdout(b):
    _phone_day_line([{"phone_day_min": 1230, "phone_day_top": "kajfosz.antimatterdimensions"}])
    _phone_day_line([{"phone_day_min": None, "phone_day_top": None}])
print(b.getvalue().strip())')"
ck "T11a 有快照 ⇒ 打印分钟+小时+最常用" "$(printf '%s' "$t11" | grep -c '手机屏幕：1230 分 ≈ 20.5 小时，最常用 kajfosz')" "1"
ck "T11b 无快照 ⇒ 只打印一行" "$(printf '%s\n' "$t11" | grep -c .)" "1"

printf '\nM17: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
