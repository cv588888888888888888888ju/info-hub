#!/usr/bin/env bash
# phone-mcp 通道 helper 后端验收（README §6）：假 adb 替身，不碰真手机、不碰真库。
# 判据：dispatch 走 SHOW 显式广播且 payload 是整条 JSON（id/title/mode/options/token）、成功才建行；
# helper 未装时 auto 回落 bridge（bridge 不可达 ⇒ 报错且不建行）；dismiss 走 DISMISS 广播；backend 名字非法 ⇒ 报错。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FAKE="$HERE/stubs/adb_fake.sh"
export PYTHONPATH="$ROOT/src"
TMP="$(mktemp -d /tmp/infohub-helper-XXXXXX)"
trap '/usr/bin/rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "$2"; }

mkcfg() { # mkcfg <out> <root> [dotted.k=v ...]
  local out="$1" root="$2"; shift 2
  mkdir -p "$root"
  python3 - "$ROOT/hub.config.yaml" "$out" "$root" "$@" <<'PY'
import sys, yaml
src, dst, root = sys.argv[1], sys.argv[2], sys.argv[3]
raw = yaml.safe_load(open(src, encoding="utf-8"))
raw["root"] = root
for kv in sys.argv[4:]:
    key, value = kv.split("=", 1)
    node = raw
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = yaml.safe_load(value)
yaml.safe_dump(raw, open(dst, "w", encoding="utf-8"), allow_unicode=True)
PY
}

py() { # py <root> <cfg> <python-expr；可用 mod/cfg/st>
  HUB_ROOT="$1" HUB_CONFIG="$2" CH="$ROOT/plugins/channels/phone-mcp/channel.py" python3 - "$3" <<'PY'
import os, sys, json, importlib.util
from infohub.config import load
from infohub.store import Store
cfg = load(); st = Store(cfg.db_path); st.migrate()
spec = importlib.util.spec_from_file_location("phone_channel", os.environ["CH"])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
print(json.dumps(eval(sys.argv[1]), ensure_ascii=False, default=str))
PY
}
jq_() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

R1="$TMP/r1"; C1="$TMP/c1.yaml"
mkcfg "$C1" "$R1" "plugins.phone-mcp.enabled=true" "phone_remind.backend='helper'" \
  "phone_remind.adb='$FAKE'" "phone_remind.device='fake-serial'" "phone_remind.helper_token='tk1'"
export ADB_FAKE_LOG="$TMP/adb.log"; : > "$ADB_FAKE_LOG"; export ADB_FAKE_INSTALLED=1

OUT=$(py "$R1" "$C1" "mod.send({'op':'dispatch','config':cfg,'store':st,'title':'交材料','body':'明天10点','mode':'question','options':[{'key':'ack','label':'知道了'}]})" 2>&1 | tail -1)
ID=$(printf '%s' "$OUT" | jq_ 'd["delivery_id"]' 2>/dev/null)
printf '%s' "$OUT" | grep -q '"ok": true' && pass "T1 helper dispatch 成功" || fail "T1 helper dispatch 成功" "$OUT"
printf '%s' "$OUT" | grep -q '"backend": "helper"' && pass "T2 返回 backend=helper" || fail "T2 返回 backend=helper" "$OUT"
grep -q "am start -n com.chenty.hubreplier/.HubActivity --es op show" "$ADB_FAKE_LOG" && pass "T3 走 am start + op=show" || fail "T3 走 am start + op=show" "$(cat "$ADB_FAKE_LOG")"
B64=$(grep -o "payload_b64 [A-Za-z0-9+/=]*" "$ADB_FAKE_LOG" | tail -1 | awk '{print $2}')
printf '%s' "$B64" | base64 -d 2>/dev/null | grep -qF "\"id\": \"$ID\"" && pass "T4 payload_b64 解出 id" || fail "T4 payload_b64 解出 id" "$(printf '%s' "$B64" | base64 -d 2>/dev/null)"
printf '%s' "$B64" | base64 -d 2>/dev/null | grep -q "tk1" && pass "T5 payload 带 token" || fail "T5 payload 带 token" ""
ROW=$(py "$R1" "$C1" "st.delivery('$ID')" 2>&1 | tail -1)
printf '%s' "$ROW" | grep -q '"channel": "phone"' && pass "T6 行 channel=phone" || fail "T6 行 channel=phone" "$ROW"
printf '%s' "$ROW" | grep -q '"status": "delivered"' && pass "T7 行 status=delivered" || fail "T7 行 status=delivered" "$ROW"

OUT=$(py "$R1" "$C1" "mod.send({'op':'dismiss','config':cfg,'store':st,'delivery_id':'d-x'})" 2>&1 | tail -1)
grep -q "am start -n com.chenty.hubreplier/.HubActivity --es op dismiss" "$ADB_FAKE_LOG" && pass "T8 dismiss 走 op=dismiss" || fail "T8 dismiss 走 op=dismiss" "$OUT"
printf '%s' "$OUT" | grep -q '"ok": true' && pass "T9 dismiss 返回 ok" || fail "T9 dismiss 返回 ok" "$OUT"

R2="$TMP/r2"; C2="$TMP/c2.yaml"
mkcfg "$C2" "$R2" "plugins.phone-mcp.enabled=true" "phone_remind.backend='auto'" \
  "phone_remind.adb='$FAKE'" "phone_remind.device='fake-serial'" "phone_remind.bridge='http://127.0.0.1:9/mcp'" "phone_remind.timeout_s=3"
export ADB_FAKE_INSTALLED=0
OUT=$(py "$R2" "$C2" "mod.send({'op':'dispatch','config':cfg,'store':st,'title':'回落'})" 2>&1 | tail -1)
printf '%s' "$OUT" | grep -qi "PushError\|手机不可达" && pass "T10 auto 未装 helper ⇒ 回落 bridge" || fail "T10 auto 未装 helper ⇒ 回落 bridge" "$OUT"
CNT=$(py "$R2" "$C2" "len(st.pending_deliveries(channel='phone', limit=10))" 2>&1 | tail -1)
[ "$CNT" = "0" ] && pass "T11 失败不留行" || fail "T11 失败不留行" "$CNT"

R3="$TMP/r3"; C3="$TMP/c3.yaml"; mkcfg "$C3" "$R3" "phone_remind.backend='nope'"
OUT=$(py "$R3" "$C3" "mod.send({'op':'dispatch','config':cfg,'store':st,'title':'x'})" 2>&1 | tail -1)
printf '%s' "$OUT" | grep -q "不认识的 backend" && pass "T12 非法 backend 报错" || fail "T12 非法 backend 报错" "$OUT"

echo "---- $PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
