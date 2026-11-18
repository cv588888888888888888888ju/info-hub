#!/usr/bin/env bash
# S17 验收：派生层是插件（任务书 A14；契约 INTERFACE.md §10）。
# 断言面：① hub-plugin list --json 有 7 行 kind=derived 且 status=ok；
#          ② 每个插件停用后，对应命令退出码 1 且 stderr 点名「derived 插件未启用: <name>」；
#          ③ 启用（缺省）时每个命令退出码 0；
#          ④ 实现不在本体：src/infohub/<name>.py 七个都不存在。
# 隔离：临时根 + 临时配置；不碰真库；不重启服务。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-s17-derived-XXXXXX)
export PYTHONPATH="$HUB/src${PYTHONPATH:+:$PYTHONPATH}"
PASS=0
FAIL=0

cleanup() { /usr/bin/rm -rf "$TMP"; }
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s -- %s\n' "$1" "${2:-}"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want=$2 got=$3"; fi; }

mkcfg() {  # mkcfg <out> [<name>=false ...]
  python3 - "$HUB/hub.config.yaml" "$1" "${@:2}" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw.setdefault("plugins", {})
for kv in sys.argv[3:]:
    name, val = kv.split("=", 1)
    raw["plugins"].setdefault(name, {})["enabled"] = (val == "true")
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
}

# ---- T1 清单 ---------------------------------------------------------------
rows=$(cd "$HUB" && bin/hub-plugin list --json 2>/dev/null)
if printf '%s' "$rows" | python3 -c '
import json, sys
rows = [p for p in json.load(sys.stdin)["plugins"] if p.get("kind") == "derived"]
want = {"activity", "vision", "calib", "features", "input", "camera", "screencast"}
got = {p["name"] for p in rows if p.get("status") == "ok"}
sys.exit(0 if want <= got else 1)'; then
  ok "T1 7 个 derived 插件都在清单里且 status=ok"
else
  bad "T1 7 个 derived 插件都在清单里且 status=ok" "清单里缺项或状态不对"
fi

# ---- T2/T3 停用 rc1 点名；启用 rc0 ---------------------------------------
mkcfg "$TMP/on.yaml"
mkcfg "$TMP/camera-on.yaml" camera=true
# 注意 camera 的开关在 collect.camera.enabled，不在 plugins.camera.enabled
python3 - "$TMP/camera-on.yaml" <<'PY'
import sys, yaml
p = sys.argv[1]
raw = yaml.safe_load(open(p, encoding="utf-8"))
raw.setdefault("collect", {}).setdefault("camera", {})["enabled"] = True
yaml.safe_dump(raw, open(p, "w", encoding="utf-8"), allow_unicode=True)
PY

run_cmd() {  # run_cmd <cfg> <subcmd> <args...>
  local cfg="$1"; shift
  local sub="$1"; shift
  HUB_ROOT="$TMP/root" HUB_CONFIG="$cfg" "$HUB/bin/$sub" "$@" 2>&1
}

# name|命令|停用后要看见的短语
CASES=(
  "activity|hub-activity --json day|derived 插件未启用: activity"
  "vision|hub-vision classify hello|derived 插件未启用: vision"
  "calib|hub-calib --json report|derived 插件未启用: calib"
  "features|hub-features --json show|derived 插件未启用: features"
  "input|hub-input --json probe|derived 插件未启用: input"
  "camera|hub-activity --json sample|derived 插件未启用: camera"
  "screencast|hub-screencast --json status|derived 插件未启用: screencast"
)

for item in "${CASES[@]}"; do
  IFS='|' read -r name cmd phrase <<<"$item"
  mkcfg "$TMP/off.yaml" "$name=false"
  cfg="$TMP/off.yaml"
  [ "$name" = "camera" ] && cfg="$TMP/camera-on.yaml" && mkcfg "$TMP/camera-on-off.yaml" camera=false && python3 - "$TMP/camera-on-off.yaml" <<'PY'
import sys, yaml
p = sys.argv[1]
raw = yaml.safe_load(open(p, encoding="utf-8"))
raw.setdefault("collect", {}).setdefault("camera", {})["enabled"] = True
yaml.safe_dump(raw, open(p, "w", encoding="utf-8"), allow_unicode=True)
PY
  [ "$name" = "camera" ] && cfg="$TMP/camera-on-off.yaml"
  set -- $cmd
  out=$(HUB_ROOT="$TMP/root" HUB_CONFIG="$cfg" "$HUB/bin/$1" "${@:2}" 2>&1); rc=$?
  check_eq "T2 $name 停用后 rc=1" 1 "$rc"
  case "$out" in
    *"$phrase"*) ok "T2 $name 提示点名未启用" ;;
    *) bad "T2 $name 提示点名未启用" "输出: ${out:0:200}" ;;
  esac
done

for item in "${CASES[@]}"; do
  IFS='|' read -r name cmd phrase <<<"$item"
  cfg="$TMP/on.yaml"
  [ "$name" = "camera" ] && cfg="$TMP/camera-on.yaml"
  set -- $cmd
  HUB_ROOT="$TMP/root" HUB_CONFIG="$cfg" "$HUB/bin/$1" "${@:2}" >/dev/null 2>&1; rc=$?
  check_eq "T3 $name 启用后 rc=0" 0 "$rc"
done

# ---- T4 实现不在本体 -------------------------------------------------------
missing=0
for m in activity vision calib features inputs camera screencast; do
  [ -e "$HUB/src/infohub/$m.py" ] && missing=$((missing + 1))
done
check_eq "T4 七个实现文件都不在 src/infohub/" 0 "$missing"

# ---- T5 目录缺失（隔离根拷贝，不碰仓库） -----------------------------------
# 断言：把某插件目录挪走 ⇒ 对应命令 rc1 且点名「derived 插件未安装: <name>」。
ISO="$TMP/iso"
mkdir -p "$ISO"
cp -a "$HUB/src" "$HUB/bin" "$HUB/plugins" "$HUB/hub.config.yaml" "$ISO/"
mkiso() {  # mkiso <enable-camera true|false>
  python3 - "$ISO/hub.config.yaml" "$1" <<'PY'
import sys, yaml
p, cam = sys.argv[1], sys.argv[2] == "true"
raw = yaml.safe_load(open(p, encoding="utf-8"))
raw.setdefault("collect", {}).setdefault("camera", {})["enabled"] = cam
yaml.safe_dump(raw, open(p, "w", encoding="utf-8"), allow_unicode=True)
PY
}
mkiso true
for item in "${CASES[@]}"; do
  IFS='|' read -r name cmd phrase <<<"$item"
  away="$TMP/away-$name"
  mv "$ISO/plugins/derived/$name" "$away"
  set -- $cmd
  out=$(cd "$ISO" && HUB_ROOT="$TMP/root" HUB_CONFIG="$ISO/hub.config.yaml" "$ISO/bin/$1" "${@:2}" 2>&1); rc=$?
  mv "$away" "$ISO/plugins/derived/$name"
  check_eq "T5 $name 目录缺失 rc=1" 1 "$rc"
  case "$out" in
    *"derived 插件未安装: $name"*) ok "T5 $name 点名未安装" ;;
    *) bad "T5 $name 点名未安装" "输出: ${out:0:200}" ;;
  esac
done

printf '\nSUMMARY {"checks":%d,"failed":%d}\n' "$((PASS + FAIL))" "$FAIL"
[ "$FAIL" -eq 0 ]
