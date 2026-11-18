#!/usr/bin/env bash
# info-hub M13 验收：输入节奏（键盘/鼠标计数）。
# 全程用合成事件，不碰真设备、不需要 input 组、不碰真库。
# 判据逐条对应 INTERFACE.md §8 M13。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"

TMP="$(mktemp -d /tmp/infohub-m13-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
ck() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "期望 $3，实际 $2"; fi; }

py() { HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - "$@" <<'PY'
import json, os, sys
from derived_mod import load as dload
from infohub import events as E
I = dload("input")
from infohub.config import load
from infohub.store import Store

cmd = sys.argv[1]
cfg = load(); store = Store(cfg.db_path); store.migrate()

if cmd == "agg":                       # 合成事件 → 计数
    ev = []
    ev += [(1, 30, 1), (1, 30, 0)]           # A 按下 + 抬起 ⇒ keys +1
    ev += [(1, 48, 1), (1, 48, 2), (1, 48, 2)]  # B 按下 + 两次自动重复 ⇒ keys +1
    ev += [(1, 0x110, 1), (1, 0x111, 1), (1, 0x112, 0)]  # 左键、右键按下；中键抬起不计 ⇒ clicks 2
    ev += [(2, 0, 30), (2, 1, 40)]           # 光标位移 30,40 ⇒ 50 px
    ev += [(2, 8, -2), (2, 8, 3)]            # 滚轮取绝对值 ⇒ 5
    ev += [(3, 0, 999)]                      # EV_ABS 不计
    print(json.dumps(I.aggregate(ev, window_s=60)))

elif cmd == "sampler_no_dev":
    try:
        I.Sampler(paths=[])
        print("no-error")
    except PermissionError as exc:
        print("PermissionError")

elif cmd == "seed":
    rows = []
    for i, (k, c, d) in enumerate([(120, 30, 4000), (90, 25, 3500)]):
        ev = [(1, 30, 1)] * k + [(1, 0x110, 1)] * c + [(2, 0, d)]
        payload = I.aggregate(ev, window_s=60.0)          # 用真实聚合口径造 payload
        payload["devices"] = 3
        rows.append(E.make_event("input", "input.sample", payload,
                                 ts=f"2026-10-20T09:0{i}:00+08:00", privacy="normal"))
    # 一条活动采样与片段，让 day 有东西可打
    rows.append(E.make_event("activity", "activity.sample",
                             {"idle_s": 5.0, "active": True, "frame_hash": "aa", "frame_count": 1,
                              "apps": ["ptyxis"]},
                             ts="2026-10-20T09:00:30+08:00", privacy="normal"))
    rows.append(E.make_event("activity", "activity.sample",
                             {"idle_s": 6.0, "active": True, "frame_hash": "aa", "frame_count": 1,
                              "apps": ["ptyxis"]},
                             ts="2026-10-20T09:01:30+08:00", privacy="normal"))
    print(store.insert_events(rows))

elif cmd == "keys_of_payload":
    ev = store.events(source="input", limit=5)[0]
    print(json.dumps(sorted(ev["payload"].keys())))

elif cmd == "keycode_leak":
    # 断言：payload 里任何值都不像按键码（不该出现 0x110 这类）
    import re
    ev = store.events(source="input", limit=5)[0]
    blob = json.dumps(ev["payload"])
    print("LEAK" if re.search(r'"(272|273|274)"', blob) else "CLEAN")
PY
}

R="$TMP/root"; CFG="$TMP/cfg.yaml"; mkdir -p "$R"
python3 - "$ROOT/hub.config.yaml" "$CFG" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name, node in raw["collect"].items():
    if isinstance(node, dict) and "enabled" in node:
        node["enabled"] = False
raw["collect"]["input"]["enabled"] = True
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY

# ---- T1 计数口径 ----
out=$(py agg)
get() { python3 -c "import json,sys;print(json.loads(sys.argv[1])['$1'])" "$out"; }
ck "T1a 只数按下（忽略抬起与自动重复）" "$(get keys)" "2"
ck "T1b 左/右键按下计点击，抬起不计" "$(get clicks)" "2"
ck "T1c 滚轮取绝对值" "$(get wheel)" "5"
ck "T1d 光标位移取欧氏长度" "$(get dist_px)" "50"
ck "T1e 窗口秒数落进 payload" "$(get secs)" "60.0"

# ---- T2 没设备 ⇒ PermissionError（不静默返回空）----
ck "T2 无设备抛 PermissionError" "$(py sampler_no_dev)" "PermissionError"

# ---- T3 payload 字段白名单 ----
py seed >/dev/null
ck "T3a payload 字段白名单（M14 加 path_px/straightness）" "$(py keys_of_payload)" '["bs_ratio", "burst_max", "clicks", "devices", "dist_px", "iki_cv_ms", "iki_median_ms", "keys", "path_px", "pauses", "secs", "straightness", "wheel"]'
ck "T3b payload 无 keycode 泄漏" "$(py keycode_leak)" "CLEAN"

# ---- T4 hub-input stats 数字与库一致 ----
out=$(HUB_ROOT="$R" HUB_CONFIG="$CFG" "$ROOT/bin/hub-input" --json stats --day 2026-10-20 2>&1)
ck "T4a stats 按键合计" "$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['keys'])" "$out")" "210"
ck "T4b stats 点击合计" "$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['clicks'])" "$out")" "55"
ck "T4c stats 光标合计" "$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['dist_px'])" "$out")" "7500"

# ---- T5 hub-activity day 打出「手上」一行 ----
out=$(HUB_ROOT="$R" HUB_CONFIG="$CFG" "$ROOT/bin/hub-activity" --day x day --date 2026-10-20 2>&1 || true)
out=$(HUB_ROOT="$R" HUB_CONFIG="$CFG" "$ROOT/bin/hub-activity" day --date 2026-10-20 2>&1)
[[ "$out" == *"手上"* && "$out" == *"按键 210"* ]] && pass "T5 day 打出「手上」一行" || fail "T5 day 手上" "$out"

# ---- T6 配置键齐全 ----
miss=$(python3 - "$ROOT/hub.config.yaml" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["collect"].get("input", {})
print(",".join(k for k in ("enabled", "window_s") if k not in d))
PY
)
[[ -z "$miss" ]] && pass "T6 collect.input 配置键齐全" || fail "T6 配置键" "缺 $miss"

# ---- T7 probe 在本环境（无权限）也退 0 ----
if "$ROOT/bin/hub-input" probe >/dev/null 2>&1; then pass "T7 probe 退出码 0"; else fail "T7 probe"; fi

# ---- T8 disabled ⇒ listen 直接退出 0 ----
CFG2="$TMP/cfg2.yaml"
python3 - "$ROOT/hub.config.yaml" "$CFG2" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["collect"]["input"]["enabled"] = False
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
if HUB_ROOT="$R" HUB_CONFIG="$CFG2" "$ROOT/bin/hub-input" --quiet listen >/dev/null 2>&1; then
  pass "T8 enabled=false ⇒ listen 退出 0（不反复重启）"
else
  fail "T8 关掉后应退出 0"
fi

printf '\nM13: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
