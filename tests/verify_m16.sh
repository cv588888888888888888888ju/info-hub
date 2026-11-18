#!/usr/bin/env bash
# info-hub M16 验收：行为特征层（一小时一行）+ 键盘节奏五指标。
# 全程合成事件，不碰真库、不碰设备。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"

TMP="$(mktemp -d /tmp/infohub-m16-XXXXXX)"
DAY="2026-10-24"
trap 'rm -rf "$TMP"' EXIT
R="$TMP/root"; CFG="$TMP/cfg.yaml"; mkdir -p "$R"
python3 - "$ROOT/hub.config.yaml" "$CFG" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name, node in raw["collect"].items():
    if isinstance(node, dict) and "enabled" in node:
        node["enabled"] = False
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
ck() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "期望 $3，实际 $2"; fi; }

py() { HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - "$@" <<'PY'
import json, sys
from infohub.config import load
from infohub.store import Store
from derived_mod import load as dload
from infohub import events as E
F = dload("features")
I = dload("input")

cmd = sys.argv[1]
cfg = load(); store = Store(cfg.db_path); store.migrate()
DAY = "2026-10-24"

if cmd == "rhythm_fast":                 # 100ms 连打 20 键
    print(json.dumps(I.aggregate([(1, 30, 1, 1.0 + i * 0.1) for i in range(20)], window_s=60.0)))
elif cmd == "rhythm_pause":              # 2.5s 一个键
    print(json.dumps(I.aggregate([(1, 30, 1, 1.0 + i * 2.5) for i in range(6)], window_s=60.0)))
elif cmd == "rhythm_back":               # 8 个普通键 + 1 个退格
    print(json.dumps(I.aggregate([(1, 30, 1, 1.0 + i * 0.1) for i in range(8)] + [(1, 14, 1, 2.5)], window_s=60.0)))
elif cmd == "rhythm_none":
    print(json.dumps(I.aggregate([(2, 0, 5, 1.0)], window_s=60.0)))

elif cmd == "seed":                      # 造一小时：在场/在座/输入/窗口/手机/截图
    rows = []
    for m in range(12):                   # 09 点：12 个 activity 采样，9 个在场
        rows.append(E.make_event("activity", "activity.sample",
                                 {"idle_s": 5.0 if m < 9 else 600.0, "active": m < 9,
                                  "frame_hash": "x", "frame_count": 1, "apps": []},
                                 ts=f"{DAY}T09:{m*4:02d}:00+08:00"))
    for m in range(12):                   # 09 点：10 个 facing
        rows.append(E.make_event("camera", "camera.sample",
                                 {"state": "facing" if m < 10 else "away", "faces": 1,
                                  "face_ratio": 0.2, "yaw": 0.0, "latency_ms": 9,
                                  "w": 640, "h": 480, "version": "cam-1"},
                                 ts=f"{DAY}T09:{m*4:02d}:30+08:00"))
    for m in range(10):                   # 09 点：10 个 60s 窗口，每窗 60 键
        payload = {"secs": 60.0, "keys": 60, "clicks": 3, "wheel": 1, "dist_px": 500,
                   "path_px": 600, "straightness": 0.83, "bs_ratio": 0.05,
                   "iki_median_ms": 120, "iki_cv_ms": 0.3, "pauses": 1, "burst_max": 30,
                   "devices": 20}
        rows.append(E.make_event("input", "input.sample", payload,
                                 ts=f"{DAY}T09:{m*6:02d}:00+08:00"))
    for m in range(4):                    # 窗口切换 4 次
        rows.append(E.make_event("window", "window.snapshot", {"frames": [], "count": 2},
                                 ts=f"{DAY}T09:{m*10:02d}:00+08:00", privacy="sensitive"))
    for m in range(6):                    # 手机 6 条通知，间隔 5 分钟 ⇒ 25 分钟跨度
        rows.append(E.make_event("phone-hub", "notify",
                                 {"app": "QQ", "text": "hi", "title": "群"},
                                 ts=f"{DAY}T09:{m*5:02d}:00+08:00"))
    rows.append(E.make_event("phone-hub", "notify",
                             {"app": "QQ", "text": "hi", "title": "群"},
                             ts=f"{DAY}T11:00:00+08:00"))     # 11 点另一段
    for m in range(3):                    # 截屏语义：两张 code、一张 video
        rows.append(E.make_event("screenshot", "shot.saved",
                                 {"sha256": f"s{m}", "bytes": 100, "path": f"/tmp/{m}.jpg"},
                                 ts=f"{DAY}T09:{m*15:02d}:00+08:00"))
    print(store.insert_events(rows)[0])

elif cmd == "labels":
    store.shot_label_put("s0", f"{DAY}T09:00:00+08:00", "code", 0.9, "[]", "rules", "ocr-2")
    store.shot_label_put("s1", f"{DAY}T09:15:00+08:00", "code", 0.9, "[]", "rules", "ocr-2")
    store.shot_label_put("s2", f"{DAY}T09:30:00+08:00", "video", 0.8, "[]", "rules", "ocr-2")
    print("ok")

elif cmd == "build":
    rows = F.build(store, DAY, "feat-1")
    store.features_replace(DAY, "feat-1", rows)
    h9 = [r for r in rows if r["hour"] == 9][0]
    print(json.dumps({"rows": len(rows), "h9": {k: h9[k] for k in
        ("presence_ratio", "facing_ratio", "switch_count", "keys", "clicks",
         "iki_median_ms", "pauses", "burst_max", "bs_ratio", "phone_notif",
         "phone_notif_min", "vision_cat", "coverage")},
        "h0": {k: rows[0][k] for k in ("presence_ratio", "coverage")}}, default=str))

elif cmd == "build_again":
    rows = F.build(store, DAY, "feat-1")
    store.features_replace(DAY, "feat-1", rows)
    print(len(store.features_day(DAY, "feat-1")))

elif cmd == "build_v2":
    rows = F.build(store, DAY, "feat-2")
    store.features_replace(DAY, "feat-2", rows)
    print(json.dumps({"v1": len(store.features_day(DAY, "feat-1")),
                      "v2": len(store.features_day(DAY, "feat-2"))}))
PY
}

# T1 键盘节奏（纯函数）
out=$(py rhythm_fast); g() { python3 -c "import json,sys;print(json.loads(sys.argv[1])['$1'])" "$out"; }
ck "T1a 100ms 连打 ⇒ 中位 100ms" "$(g iki_median_ms)" "100"
ck "T1b 100ms 连打 ⇒ 连击 20"    "$(g burst_max)" "20"
ck "T1c 100ms 连打 ⇒ 停顿 0"     "$(g pauses)" "0"
out=$(py rhythm_pause); g() { python3 -c "import json,sys;print(json.loads(sys.argv[1])['$1'])" "$out"; }
ck "T1d 2.5s 间隔 ⇒ 停顿 5 次"   "$(g pauses)" "5"
ck "T1e 2.5s 间隔 ⇒ 连击 1"      "$(g burst_max)" "1"
out=$(py rhythm_back); g() { python3 -c "import json,sys;print(json.loads(sys.argv[1])['$1'])" "$out"; }
ck "T1f 9 键含 1 退格 ⇒ 退格比 0.111" "$(g bs_ratio)" "0.111"
out=$(py rhythm_none); g() { python3 -c "import json,sys;print(json.loads(sys.argv[1])['$1'])" "$out"; }
ck "T1g 无按键 ⇒ 中位 0"         "$(g iki_median_ms)" "0"
ck "T1h 无按键 ⇒ 连击 0"         "$(g burst_max)" "0"

# T2 特征表
py seed >/dev/null; py labels >/dev/null
out=$(py build); g2() { python3 -c "import json,sys;d=json.loads(sys.argv[1])['h9'];print(d['$1'])" "$out"; }
ck "T2a 在场比例 0.75（9/12）"   "$(g2 presence_ratio)" "0.75"
ck "T2b 看屏比例 0.833（10/12）" "$(g2 facing_ratio)" "0.833"
ck "T2c 切换 4 次"               "$(g2 switch_count)" "4"
ck "T2d 按键合计 600"            "$(g2 keys)" "600"
ck "T2e 节奏中位 120ms"          "$(g2 iki_median_ms)" "120"
ck "T2f 停顿合计 10"             "$(g2 pauses)" "10"
ck "T2g 手机通知 6 条"           "$(g2 phone_notif)" "6"
ck "T2h 手机使用 25 分钟"        "$(g2 phone_notif_min)" "25"
ck "T2i 截图类别众数 code"       "$(g2 vision_cat)" "code"
ck "T2j coverage 六位全 1"       "$(g2 coverage)" "111111"
ck "T2k 空小时 presence 为 null" "$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['h0']['presence_ratio'])" "$out")" "None"
ck "T2l 空小时 coverage 全 0"    "$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['h0']['coverage'])" "$out")" "000000"
ck "T2m 24 行"                   "24" "$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['rows'])" "$out")"

# T3 幂等 / 版本隔离
ck "T3a 重跑仍是 24 行"          "$(py build_again)" "24"
out=$(py build_v2)
ck "T3b feat_version 隔离（新旧各 24）" "$(python3 -c "import json,sys;d=json.loads(sys.argv[1]);print(d['v1'],d['v2'])" "$out")" "24 24"

# T4 CLI 端到端
HUB_ROOT="$R" HUB_CONFIG="$CFG" "$ROOT/bin/hub-features" build --day "$DAY" --quiet >/dev/null 2>&1 \
  && pass "T4a hub-features build 退出 0" || fail "T4a build"
out=$(HUB_ROOT="$R" HUB_CONFIG="$CFG" "$ROOT/bin/hub-features" show --day "$DAY" --json 2>&1)
python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert len(d['features'])==24; assert d['features'][9]['hour']==9" "$out" \
  && pass "T4b show --json 24 行按小时升序" || fail "T4b show"

# T5 配置键
miss=$(python3 - "$ROOT/hub.config.yaml" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
print("feat_version" if "feat_version" not in d.get("behavior", {}) else "")
PY
)
[[ -z "$miss" ]] && pass "T5 behavior.feat_version 齐全" || fail "T5 配置键" "缺 $miss"

printf '\nM16: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
