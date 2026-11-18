#!/usr/bin/env bash
# info-hub M10 验收：转译层（数据 → 什么时候开始干活 / 多专注 / 多久）。
# 全程离线：合成采样事件 + 临时库 + 临时根，不碰真库、不碰键鼠、不弹窗、不推送。
# 判据逐条对应 INTERFACE.md §8 M10。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"
ACT=(python3 -m infohub.entry_activity)

TMP="$(mktemp -d /tmp/infohub-m10-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

mkcfg() {  # mkcfg <out> [section.key=value ...]
  local out="$1"; shift
  python3 - "$ROOT/hub.config.yaml" "$out" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name, node in raw["collect"].items():
    if isinstance(node, dict) and "enabled" in node:
        node["enabled"] = False
for kv in sys.argv[3:]:
    k, v = kv.split("=", 1)
    parts = k.split(".")
    node = raw
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = yaml.safe_load(v)
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
}

seed() {  # seed <root> <cfg>：造 2026-10-20 的采样（确定性，不依赖当前时间）
  HUB_ROOT="$1" HUB_CONFIG="$2" python3 - <<'PY'
import sys
from datetime import datetime, timedelta, timezone
from infohub.config import load
from infohub.store import Store
from infohub import events as E

config = load()
store = Store(config.db_path)
store.migrate()
TZ = timezone(timedelta(hours=8))

def at(h, m):
    return datetime(2026, 10, 20, h, m, tzinfo=TZ).isoformat(timespec="seconds")

rows = []
def add(h, m, active=True, hart="h1"):
    rows.append(E.make_event("activity", "activity.sample",
                             {"idle_s": 3.0 if active else 900.0, "active": active,
                              "frame_hash": hart, "frame_count": 1, "apps": ["x"]},
                             ts=at(h, m)))

# 段 A：10:00–10:09 连续在场，第 6 条起换窗口集合（切 1 次）
for i in range(10):
    add(10, i, True, "h1" if i < 5 else "h2")
# 空档 11 分钟（> gap_s=300）⇒ 断段；段 B：10:20–10:22
for i in range(3):
    add(10, 20 + i, True, "h2")
# 8 分钟后一条离开采样 ⇒ 单独成段但 active_s=0，不落库
add(10, 30, False, "h2")
# 段 C/D：离开把段切开（11:00–11:01 在场，11:02 离开，11:03–11:04 在场）
add(11, 0, True, "h1")
add(11, 1, True, "h1")
add(11, 2, False, "h1")
add(11, 3, True, "h1")
add(11, 4, True, "h1")

inserted, dupes = store.insert_events(rows)
print(f"seeded={inserted} dupes={dupes}")
PY
}

sql() {  # sql <root> <cfg> <expr>
  HUB_ROOT="$1" HUB_CONFIG="$2" python3 - "$3" <<'PY' | tail -1
import sys
from infohub.config import load
from infohub.store import Store
c = load(); s = Store(c.db_path)
print(eval(sys.argv[1]))
PY
}

R="$TMP/r"; CFG="$TMP/c.yaml"; mkcfg "$CFG"
seed "$R" "$CFG"

events_before="$(sql "$R" "$CFG" "s.conn.execute(\"SELECT COUNT(*) FROM events\").fetchone()[0]")"

# ---------------------------------------------------------------- T1 切段与三因子
ep() { HUB_ROOT="$R" HUB_CONFIG="$CFG" "${ACT[@]}" --json episodes --day 2026-10-20 2>/dev/null; }
J1="$(ep)"
N1="$(printf '%s' "$J1" | json_get "len(d['episodes'])")"
A1="$(printf '%s' "$J1" | json_get "d['episodes'][0]['active_s']")"
F1="$(printf '%s' "$J1" | json_get "d['episodes'][0]['focus']")"
A2="$(printf '%s' "$J1" | json_get "d['episodes'][1]['active_s']")"
SW1="$(printf '%s' "$J1" | json_get "d['episodes'][0]['switch_count']")"
[ "$N1" = "4" ] && pass "T1 切段：断段/离开收段后共 4 段（实际 $N1）" || fail "T1 切段" "n=$N1"
[ "$A1" = "600" ] && pass "T1 在场时长 = 10 条 × 60s = 600s" || fail "T1 在场时长" "a1=$A1"
# presence=1, stability=1-1/(10×1.0)=0.9, continuity=1-60/300=0.8 ⇒ 100*(0.5+0.27+0.16)=93
[ "$F1" = "93" ] && pass "T1 专注度 = 三因子加权 = 95（公式与 §4.15 一致）" || fail "T1 专注度" "f1=$F1"
[ "$A2" = "180" ] && pass "T1 段 B 在场 180s（空档 11 分钟已断段）" || fail "T1 段 B" "a2=$A2"
[ "$SW1" = "1" ] && pass "T1 切换数按窗口集合哈希变化计 = 1" || fail "T1 切换数" "sw1=$SW1"

# ---------------------------------------------------------------- T2 太短不落库
SHORT="$(printf '%s' "$J1" | json_get "sum(1 for e in d['episodes'] if e['active_s'] == 0)")"
[ "$SHORT" = "0" ] && pass "T2 active_s=0 的段不落库（min_active_s 生效）" || fail "T2 短段" "n=$SHORT"

# ---------------------------------------------------------------- T3 幂等 + events 不动
J2="$(ep)"
[ "$J1" = "$J2" ] && pass "T3 同一输入重算两次，输出逐字节相同" || fail "T3 幂等"
events_after="$(sql "$R" "$CFG" "s.conn.execute(\"SELECT COUNT(*) FROM events\").fetchone()[0]")"
[ "$events_before" = "$events_after" ] && pass "T3 重算不动 events（$events_after 条）" \
  || fail "T3 events 被改" "$events_before→$events_after"

# ---------------------------------------------------------------- T4 day 白话输出
DAY_OUT="$(HUB_ROOT="$R" HUB_CONFIG="$CFG" "${ACT[@]}" day --date 2026-10-20 2>/dev/null)"
printf '%s' "$DAY_OUT" | grep -q "开始干活" && pass "T4 day 给出「开始干活」时刻" || fail "T4 开始" "$DAY_OUT"
printf '%s' "$DAY_OUT" | grep -q "在场合计" && pass "T4 day 给出在场合计" || fail "T4 合计"
printf '%s' "$DAY_OUT" | grep -q "平均专注" && pass "T4 day 给出平均专注度" || fail "T4 专注"
printf '%s' "$DAY_OUT" | grep -q "不读屏幕内容" && pass "T4 day 自带「分不清写代码和刷视频」声明" \
  || fail "T4 声明"

# ---------------------------------------------------------------- T5 无采样日要诚实
RC5=0; OUT5="$(HUB_ROOT="$R" HUB_CONFIG="$CFG" "${ACT[@]}" day --date 2020-01-01 2>&1)" || RC5=$?
[ "$RC5" -eq 0 ] && printf '%s' "$OUT5" | grep -q "这一天没有活动采样" \
  && pass "T5 没有采样的日子：退出码 0 + 明说没有采样" || fail "T5 无采样日" "rc=$RC5 $OUT5"

# ---------------------------------------------------------------- T6 探针失败：rc=1 且不落半条
EMPTY="$TMP/emptybin"; mkdir -p "$EMPTY"
R6="$TMP/r6"; CFG6="$TMP/c6.yaml"; mkcfg "$CFG6" "collect.activity.interval_s=0"   # 关掉节流，让探针一定跑
RC6=0; PATH="$EMPTY" HUB_ROOT="$R6" HUB_CONFIG="$CFG6" \
  /usr/bin/python3 -m infohub.entry_activity sample >/dev/null 2>&1 || RC6=$?
n_after="$(sql "$R6" "$CFG6" "s.conn.execute(\"SELECT COUNT(*) FROM events WHERE source='activity'\").fetchone()[0]")"
n_before=0
[ "$RC6" -eq 1 ] && pass "T6 探针失败：退出码 1" || fail "T6 退出码" "rc=$RC6"
[ "$n_before" = "$n_after" ] && pass "T6 探针失败不落半条（活动事件仍 $n_after 条）" \
  || fail "T6 落了半条" "$n_before→$n_after"

# ---------------------------------------------------------------- T7 换口径版本不互相覆盖
CFG2="$TMP/c2.yaml"; mkcfg "$CFG2" "behavior.def_version=ep-4"
ep2() { HUB_ROOT="$R" HUB_CONFIG="$CFG2" "${ACT[@]}" --json episodes --day 2026-10-20 2>/dev/null; }
ep2 >/dev/null
VERS="$(sql "$R" "$CFG" "sorted({r[0] for r in s.conn.execute('SELECT def_version FROM episodes')})")"
[ "$VERS" = "['ep-3', 'ep-4']" ] && pass "T7 def_version 换号：新旧片段共存（$VERS）" \
  || fail "T7 版本共存" "$VERS"

# ---------------------------------------------------------------- T8 采集器落库条件
HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - <<'PY' > "$TMP/t8.out" 2>&1
from infohub.config import load
from infohub.store import Store
from derived_mod import load as dload
A = dload("activity")

class Cfg:
    def collector_get(self, name, key, default=None):
        return {"interval_s": 0}.get(key, default)
    def get(self, key, default=None):
        return {"behavior.idle_active_s": 60, "behavior.heartbeat_s": 900}.get(key, default)

class Env:
    idle = 3.0
    def idle_s(self):
        return Env.idle

A.Env = Env
A.W.frames = lambda cfg: [{"app": "ptyxis", "title": "t"}]

config = load()
store = Store(config.db_path); store.migrate()
store.state_put("activity", cursor={}, ok=True)

def step(idle):
    Env.idle = idle
    return len(A.collect(Cfg(), store))

a = step(3.0)        # 在场 → 必落
b = step(3.0)        # 仍在场 → 必落
c = step(900.0)      # 在场 → 离开：边界落一条
d = step(900.0)      # 离开期、还没到心跳 → 不落
print(a, b, c, d)
PY
T8="$(tail -1 "$TMP/t8.out")"
[ "$T8" = "1 1 1 0" ] && pass "T8 落库条件：在场必落/边界一条/心跳未到不落（$T8）" || fail "T8 落库条件" "$T8"

# ---------------------------------------------------------------- T9 哈希不含标题
HASH_Q="$(HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 -c "
from derived_mod import install; install()
import _d_activity as _A
app_hash = _A.app_hash
a = app_hash([{'app':'firefox','title':'写代码 - 12:03'}, {'app':'ptyxis','title':'x'}])
b = app_hash([{'app':'firefox','title':'写代码 - 12:04'}, {'app':'ptyxis','title':'y'}])
c = app_hash([{'app':'firefox','title':'z'}])
print(a == b, a != c)
" 2>&1 | tail -1)"
[ "$HASH_Q" = "True True" ] && pass "T9 哈希只取应用集合：标题带计时器不算切换" || fail "T9 哈希" "$HASH_Q"

printf '\nM10: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
