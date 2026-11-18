#!/usr/bin/env bash
# info-hub M14 验收：在座状态（摄像头三档）+ 校准（自报 vs 三路特征 AUC）。
# 全程合成数据：不碰摄像头、不碰真库、不调模型。
# 判据逐条对应 INTERFACE.md §8 M14。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"

TMP="$(mktemp -d /tmp/infohub-m14-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
ck() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "期望 $3，实际 $2"; fi; }

R="$TMP/root"; CFG="$TMP/cfg.yaml"; mkdir -p "$R"
python3 - "$ROOT/hub.config.yaml" "$CFG" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name, node in raw["collect"].items():
    if isinstance(node, dict) and "enabled" in node:
        node["enabled"] = False
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY

py() { HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - "$@" <<'PY'
import json, sys
from infohub.config import load
from infohub.store import Store
from derived_mod import load as dload
from infohub import events as E
C = dload("camera")
K = dload("calib")

cmd = sys.argv[1]
cfg = load(); store = Store(cfg.db_path); store.migrate()

def face(x, y, w, h, yaw_shift=0.0, score=0.9):
    cx, cy = x + w / 2, y + h / 2
    eye_dx = w * 0.28
    lx, rx = cx - eye_dx / 2, cx + eye_dx / 2
    nx = cx + yaw_shift * eye_dx
    return [x, y, w, h, rx, cy - h * 0.1, lx, cy - h * 0.1, nx, cy + h * 0.1,
            cx + eye_dx / 2, cy + h * 0.28, cx - eye_dx / 2, cy + h * 0.28, score]

if cmd == "classify":
    import numpy as np
    big = np.array([face(200, 120, 220, 220, 0.0)])          # 大脸、正对
    side = np.array([face(200, 120, 220, 220, 0.9)])         # 大脸、侧着
    small = np.array([face(10, 10, 20, 20, 0.0)])            # 小脸（离得远）
    two = np.array([face(10, 10, 20, 20, 0.0), face(300, 100, 200, 200, 0.0)])
    print(json.dumps({
        "none": C.classify(None, 640, 480)["state"],
        "empty": C.classify(np.zeros((0, 15)), 640, 480)["state"],
        "big": C.classify(big, 640, 480)["state"],
        "side": C.classify(side, 640, 480)["state"],
        "small": C.classify(small, 640, 480)["state"],
        "two_state": C.classify(two, 640, 480)["state"],
        "two_ratio": C.classify(two, 640, 480)["face_ratio"],
    }))

elif cmd == "no_model":
    cfg._d = None
    try:
        C.sample(cfg)
        print("no-error")
    except Exception as exc:
        print(type(exc).__name__)

elif cmd == "auc":
    print(json.dumps({
        "perfect": K.auc([9, 8, 7, 1, 2, 3], [1, 1, 1, 0, 0, 0]),
        "reversed": K.auc([1, 2, 3, 9, 8, 7], [1, 1, 1, 0, 0, 0]),
        "tie": K.auc([5, 5], [1, 0]),
        "single_class": None if K.auc([1, 2], [1, 1]) != K.auc([1, 2], [1, 1]) else "nan-like",
    }))

elif cmd == "seed":                  # 造 fake 自报 + 特征，验证 AUC 能算出来
    rows = []
    # 3 条「专注」：每窗口按键 200；3 条「走神」：每窗口按键 20
    for i, (lvl, keys) in enumerate([(1, 200), (1, 210), (1, 190), (3, 20), (2, 100), (1, 205)]):
        ts = f"2026-10-20T1{i}:05:00+08:00"
        rows.append(E.make_event("self", "self.report", {"level": lvl}, ts=ts))
        rows.append(E.make_event("input", "input.sample",
                                 {"secs": 60.0, "keys": keys, "clicks": 5, "wheel": 0,
                                  "dist_px": 1000, "path_px": 1200, "straightness": 0.83,
                                  "devices": 3}, ts=ts))
    print(store.insert_events(rows))

elif cmd == "report":
    print(json.dumps(K.report(store, days=3650, window_min=60), ensure_ascii=False))
PY
}

out=$(py classify)
get() { python3 -c "import json,sys;print(json.loads(sys.argv[1])['$1'])" "$out"; }
ck "T1a 没人 ⇒ away"        "$(get none)" "away"
ck "T1b 空数组 ⇒ away"      "$(get empty)" "away"
ck "T1c 大脸正对 ⇒ facing"  "$(get big)" "facing"
ck "T1d 大脸侧着 ⇒ present" "$(get side)" "present"
ck "T1e 小脸 ⇒ present"     "$(get small)" "present"
ck "T1f 两张脸取大的"       "$(get two_state)" "facing"

ck "T2 缺模型抛 FileNotFoundError" "$(py no_model)" "FileNotFoundError"

out=$(py auc)
get2() { python3 -c "import json,sys;print(json.loads(sys.argv[1])['$1'])" "$out"; }
ck "T3a AUC 完美分离 = 1.0" "$(get2 perfect)" "1.0"
ck "T3b AUC 反向 = 0.0"     "$(get2 reversed)" "0.0"
ck "T3c AUC 并列 = 0.5"     "$(get2 tie)" "0.5"

# T4 自报 + 特征 → report 真能算出 AUC
py seed >/dev/null
out=$(py report)
python3 - "$out" <<'PY' && echo "PASS T4 report 算出按键速率 AUC 明显 > 0.5" || echo "FAIL T4 report"
import json, sys
d = json.loads(sys.argv[1])
a = d["auc"]["keys_per_win"]
assert d["reports"] == 6, d["reports"]
assert a is not None and a > 0.8, a
PY

# T5 CLI 端到端：rate 落库、report 能读
HUB_ROOT="$R" HUB_CONFIG="$CFG" "$ROOT/bin/hub-calib" rate 2 >/dev/null 2>&1
out=$(HUB_ROOT="$R" HUB_CONFIG="$CFG" "$ROOT/bin/hub-calib" --json rate 3 2>&1)
[[ "$out" == *'"level": 3'* ]] && pass "T5 rate 落库并可 --json" || fail "T5 rate" "$out"

# T6 配置键
miss=$(python3 - "$ROOT/hub.config.yaml" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["collect"].get("camera", {})
print(",".join(k for k in ("enabled", "index", "model", "min_face_ratio", "yaw_max") if k not in d))
PY
)
[[ -z "$miss" ]] && pass "T6 collect.camera 五个键齐全" || fail "T6 配置键" "缺 $miss"

# T7 摄像头事件字段白名单（用 classify 的结果代表 payload 形状）
shape=$(HUB_ROOT="$R" HUB_CONFIG="$CFG" PYTHONPATH="$ROOT/src:$ROOT/tests" python3 - <<'PY'
import json
from derived_mod import load as dload
C = dload("camera")
import numpy as np
f = C.classify(None, 640, 480)
print(json.dumps(sorted(f.keys())))
PY
)
ck "T7 在座状态字段白名单（无图像字段）" "$shape" '["face_ratio", "faces", "state", "yaw"]'

printf '\nM14: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
