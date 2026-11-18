#!/usr/bin/env bash
# S16 验收：控制台是 view 插件（任务书 A15；契约 INTERFACE.md §4.8 / §10）。
# 断言面：① plugins.ui.enabled=false ⇒ hub-ui 退出码非 0 且 stderr 点名"未启用"；
# ② 启用（缺省）⇒ 真起进程，GET /api/state 返回 200 且 JSON 有 counts。
# 隔离：临时根 + 随机高位端口；不碰真库、不重启服务；可复跑。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-s16-ui-XXXXXX)
export PYTHONPATH="$HUB/src${PYTHONPATH:+:$PYTHONPATH}"
PASS=0
FAIL=0
UI_PID=""

cleanup() {
  if [ -n "$UI_PID" ]; then kill "$UI_PID" 2>/dev/null || true; wait "$UI_PID" 2>/dev/null || true; fi
  /usr/bin/rm -rf "$TMP"
}
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s -- %s\n' "$1" "${2:-}"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want=$2 got=$3"; fi; }

mkcfg() {  # mkcfg <out> <ui_enabled:true|false>
  python3 - "$HUB/hub.config.yaml" "$1" "$2" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw.setdefault("plugins", {}).setdefault("ui", {})["enabled"] = sys.argv[3] == "true"
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
}

# ---- T1 停用 ⇒ rc≠0 且提示 -------------------------------------------------
mkcfg "$TMP/off.yaml" false
OUT=$(HUB_ROOT="$TMP/r1" HUB_CONFIG="$TMP/off.yaml" HUB_ROOT="$TMP/r1" python3 -m infohub.entry_ui --once 2>&1)
RC=$?
check_eq "T1 停用后 hub-ui rc=1" 1 "$RC"
case "$OUT" in
  *"view 插件未启用: ui"*) ok "T1 提示点名 ui 未启用" ;;
  *) bad "T1 提示点名 ui 未启用" "输出: $OUT" ;;
esac

# ---- T2 启用 ⇒ 起服务，/api/state = 200 ------------------------------------
mkcfg "$TMP/on.yaml" true
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
HUB_ROOT="$TMP/r2" HUB_CONFIG="$TMP/on.yaml" "$HUB/bin/hub-ui" --port "$PORT" >"$TMP/ui.log" 2>&1 &
UI_PID=$!
STATE=""; CODE=""
for _ in $(seq 1 50); do
  CODE=$(curl -s -o "$TMP/state.json" -w '%{http_code}' "http://127.0.0.1:$PORT/api/state" 2>/dev/null || true)
  [ "$CODE" = "200" ] && break
  sleep 0.1
done
check_eq "T2 启用后 /api/state=200" 200 "$CODE"
if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if {"system","report"} <= set(d) else 1)' "$TMP/state.json" 2>/dev/null; then
  ok "T2 state JSON 有 system/report"
else
  bad "T2 state JSON 有 system/report" "$(head -c 200 "$TMP/state.json" 2>/dev/null)"
fi
# 停用态在清单里必须是"行在、status=disabled"，不是"行消失"（§10 语义）
if HUB_CONFIG="$TMP/off.yaml" "$HUB/bin/hub-plugin" list --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); rows=[p for p in d["plugins"] if p["name"]=="ui" and p["kind"]=="view"]; sys.exit(0 if rows and rows[0]["status"]=="disabled" else 1)'; then
  ok "T2 停用态在清单里可查（status=disabled）"
else
  bad "T2 停用态在清单里可查（status=disabled）" "（清单缺 ui 行或状态不对）"
fi

printf '\nSUMMARY {"checks":%d,"failed":%d}\n' "$((PASS + FAIL))" "$FAIL"
[ "$FAIL" -eq 0 ]
