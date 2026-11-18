#!/usr/bin/env bash
# info-hub M12 验收：画面粗判（本地小 VLM 判「工作 / 玩」）。
# 全程离线：合成截屏（真 JPEG，好验证缩图）+ 临时库 + 临时根 + 假 runner，不调真模型。
# 判据逐条对应 INTERFACE.md §8 M12。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

TMP="$(mktemp -d /tmp/infohub-m12-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }

mkcfg() {  # mkcfg <out> [section.key=value ...]
  local out="$1"; shift
  python3 - "$ROOT/hub.config.yaml" "$out" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name, node in raw["collect"].items():
    if isinstance(node, dict) and "enabled" in node:
        node["enabled"] = False
raw.setdefault("vision", {}).setdefault("vlm", {})
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

py() { HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - "$@" <<'PY'
import json, os, sys
from infohub.config import load
from infohub.store import Store
from infohub import vlm as M, events as E

cmd = sys.argv[1]
cfg = load(); store = Store(cfg.db_path); store.migrate()

def make_jpg(path, w=1280, h=800, color=(20, 30, 40)):
    import cv2
    import numpy as np
    cv2.imwrite(path, np.full((h, w, 3), color, dtype="uint8"))

if cmd == "seed":
    rows = []
    for i, sha in enumerate(("aaa111", "bbb222")):
        p = os.path.join(cfg.root, f"s{i}.jpg")
        make_jpg(p)
        rows.append(E.make_event("screenshot", "shot.saved",
                                 {"sha256": sha, "path": p, "width": 1280, "height": 800},
                                 ts=f"2026-10-20T09:0{i}:00+08:00", privacy="sensitive"))
    print(store.insert_events(rows))

elif cmd == "run":                     # run <原话> [version]
    raw = sys.argv[2]
    ver = sys.argv[3] if len(sys.argv) > 3 else None
    if ver:
        M.VERSION = ver
    res = M.run(cfg, store, runner=lambda _p: raw)
    print(json.dumps(res, ensure_ascii=False))

elif cmd == "run_real":                # 不注入 runner ⇒ 走 resolve()，缺模型必须报错
    res = M.run(cfg, store)
    print(json.dumps(res, ensure_ascii=False))

elif cmd == "count":                   # 输出行数与 pending
    print(json.dumps({"rows": len(store.shot_verdicts(limit=100)),
                      "pending": store.verdict_stats()["pending"],
                      "by": store.verdict_stats()["by_verdict"]}, ensure_ascii=False))

elif cmd == "versions":
    rows = store.conn.execute("SELECT version, verdict, raw, img_w, img_h FROM shot_verdicts ORDER BY version").fetchall()
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False))

elif cmd == "tmpfiles":
    d = os.path.join(cfg.state_dir, "vlm-tmp")
    print(json.dumps({"exists": os.path.isdir(d), "n": len(os.listdir(d)) if os.path.isdir(d) else 0}))
PY
}

R="$TMP/root"; CFG="$TMP/cfg.yaml"; mkdir -p "$R"
mkcfg "$CFG" vision.vlm.enabled=true vision.vlm.model=models/none.gguf \
      vision.vlm.mmproj=models/none-mmproj.gguf vision.vlm.max_width=512

# ---- T1 映射与缩图 ----
out=$(py seed); [[ "$out" == *"(2, 0)"* ]] && pass "T1a 种下 2 张截屏" || fail "T1a 种下 2 张截屏" "$out"
out=$(py run "工作。")
[[ "$out" == *'"work": 2'* ]] && pass "T1b 原话「工作。」⇒ work×2" || fail "T1b 映射 work" "$out"
out=$(py versions)
[[ "$out" == *'"img_w": 512, "img_h": 320'* ]] && pass "T1c 缩到 512×320 再喂模型" || fail "T1c 缩图尺寸" "$out"

# ---- T2 表外词 ⇒ unknown，且保留原话 ----
out=$(py run "美食" "vlm-probe"); echo "$out" >/dev/null
out=$(py versions)
[[ "$out" == *'"verdict": "unknown", "raw": "美食"'* ]] && pass "T2 表外词记 unknown 且留原话" || fail "T2 表外词" "$out"

# ---- T3 缺模型 ⇒ error 且零写入（新库）----
R2="$TMP/root2"; mkdir -p "$R2"; CFG2="$TMP/cfg2.yaml"
mkcfg "$CFG2" vision.vlm.enabled=true vision.vlm.model=models/none.gguf vision.vlm.mmproj=models/none.gguf
R="$R2" CFG="$CFG2" py seed >/dev/null
out=$(R="$R2" CFG="$CFG2" py run_real)
[[ "$out" == *'"error"'* ]] && pass "T3a 缺模型返回 error" || fail "T3a error 字段" "$out"
out=$(R="$R2" CFG="$CFG2" py count)
[[ "$out" == *'"rows": 0'* ]] && pass "T3b 缺模型零写入" || fail "T3b 零写入" "$out"

# ---- T4 同版本幂等 ----
out=$(py run "玩" "vlm-1")
[[ "$out" == *'"pending": 0'* ]] || fail "T4a 已判过的图不该重复pending" "$out"
out=$(py count)
[[ "$out" == *'"rows": 4'* ]] && pass "T4 同版本重跑不新增行（2 行 vlm-1 + 2 行 vlm-probe）" || fail "T4 行数" "$out"

# ---- T5 换版本 ⇒ 两行并存 ----
out=$(py run "工作" "vlm-2")
out=$(py versions)
n=$(python3 - "$out" <<'PY'
import json,sys
rows=json.loads(sys.argv[1])
print(len([r for r in rows if r["version"]=="vlm-2"]))
PY
)
[[ "$n" == "2" ]] && pass "T5 换版本两行并存（vlm-2 2 行了）" || fail "T5 版本并存" "$out"

# ---- T6 缩图临时文件不残留 ----
out=$(py tmpfiles)
[[ "$out" == *'"exists": false'* ]] && pass "T6 缩图临时目录跑完即删" || fail "T6 临时文件" "$out"

# ---- T7 act 汇总 ----
out=$(HUB_ROOT="$R" "$ROOT/bin/hub-vision" --config "$CFG" act 2>&1)
[[ "$out" == *"已粗判"* && "$out" == *"工作"* ]] && pass "T7 act 打出汇总" || fail "T7 act" "$out"

# ---- T8 derive 在缺模型时 rc=0 且摘要带 error ----
out=$(HUB_ROOT="$R2" HUB_CONFIG="$CFG2" "$ROOT/bin/hub-rules" --quiet --json derive 2>/dev/null | tail -1)
[[ "$out" == *'"vlm"'* && "$out" == *'"error"'* ]] && pass "T8 derive 记 error 但继续" || fail "T8 derive" "$out"

# ---- T9 配置键齐全 ----
miss=$(python3 - "$ROOT/hub.config.yaml" <<'PY'
import sys, yaml
v = (yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["vision"]).get("vlm", {})
need = ["enabled","model","mmproj","max_width","jpeg_quality","threads","timeout_s","max_per_run","bin_dir"]
print(",".join(k for k in need if k not in v))
PY
)
[[ -z "$miss" ]] && pass "T9 vision.vlm 九个键齐全" || fail "T9 配置键" "缺 $miss"

# ---- T10 未开时整段跳过 ----
CFG3="$TMP/cfg3.yaml"
mkcfg "$CFG3" vision.vlm.enabled=false
out=$(R="$TMP/root3" CFG="$CFG3" py run "工作")
mkdir -p "$TMP/root3"
[[ "$out" == *'"skipped"'* ]] && pass "T10 enabled=false ⇒ 整段跳过" || fail "T10 跳过" "$out"

printf '\nM12: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
