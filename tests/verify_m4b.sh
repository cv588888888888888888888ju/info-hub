#!/usr/bin/env bash
# info-hub M4b 验收：归纳层（可替换的加工层命令）+ 机械审计（证据必须指向真实事件）。
# 全程离线：加工层走 tests/digest_stub.py，不联网、不花 token。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
DIGEST=(python3 -m infohub.entry_digest)
STUB="$HERE/digest_stub.py"

TMP="$(mktemp -d /tmp/infohub-m4b-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

mkcfg() {  # mkcfg <out> <mode> [digest.k=v ...]
  local out="$1" mode="$2"; shift 2
  python3 - "$ROOT/hub.config.yaml" "$out" "$mode" "$STUB" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["digest"].update({"enabled": True, "window_days": 7,
                      "command": ["python3", sys.argv[4], "--mode", sys.argv[3]]})
for kv in sys.argv[5:]:
    k, v = kv.split("=", 1)
    raw["digest"][k] = yaml.safe_load(v)
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
}

seed() {  # seed <root> 造 3 条合成事件 + 1 条含凭据的剪贴板事件 + 1 条待办
  local root="$1"
  HUB_ROOT="$root" HUB_CONFIG="$CFG_OK" python3 - "$root" <<'PY'
import sys
from datetime import datetime, timedelta
from infohub.config import load
from infohub.events import make_event
from infohub.store import Store
cfg = load(); cfg.ensure_dirs()
store = Store(cfg.db_path); store.migrate()
now = datetime.now().astimezone()
iso = lambda d: d.isoformat(timespec="seconds")
events = [make_event("fs", "fs.change", {"root": "/tmp", "path": f"code/x{i}.py",
                                         "mtime": now.timestamp(), "size": 10 + i},
                     ts=iso(now - timedelta(minutes=30 - i)))
          for i in range(3)]
# 凭据形态：提示词里必须被脱敏
events.append(make_event("clipboard", "clip.copy",
                         {"text": "curl 'https://x/api?token=SUPERSECRETVALUE1234567890'",
                          "sha256": "ab" * 32}, ts=iso(now - timedelta(minutes=5))))
store.insert_events(events)
store.upsert_todo({"dedupe_key": "fs-repeat:m4b-synth", "title": "同一文件反复改动",
                   "why": "合成", "confidence": 0.8, "source_event_ids": [1]})
store.close()
print("seeded")
PY
}

runq() {  # runq <cfg> <root> [args...]
  local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" "${DIGEST[@]}" --json "$@"
}

# ---------------------------------------------------------------- T1 关掉就一条都不跑（显式钉 enabled=false，与仓库默认值解耦）
CFG0="$TMP/c0.yaml"; ROOT0="$TMP/r0"
python3 - "$ROOT/hub.config.yaml" "$CFG0" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["digest"]["enabled"] = False          # M4d 起仓库默认 true（每周本地归纳）；这里专测"关掉"的语义
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
O1="$(HUB_ROOT="$ROOT0" HUB_CONFIG="$CFG0" "${DIGEST[@]}" --json run)"
[ "$(printf '%s' "$O1" | json_get "d['reason']")" = "disabled" ] && pass "T1 默认关 ⇒ disabled" \
  || fail "T1 默认关" "out=$O1"

# ---------------------------------------------------------------- T2 dry-run：只组装提示词，不调加工层、不写库
CFG_OK="$TMP/ok.yaml"; ROOT2="$TMP/r2"; mkcfg "$CFG_OK" ok
seed "$ROOT2"
O2="$(runq "$CFG_OK" "$ROOT2" run --dry-run)"
PC="$(printf '%s' "$O2" | json_get "d['reason']")"
HAS_SEED="$(printf '%s' "$O2" | json_get "'code/x0.py' in d['prompt']")"
LEAK="$(printf '%s' "$O2" | json_get "'SUPERSECRETVALUE1234567890' in d['prompt']")"
MASKED="$(printf '%s' "$O2" | json_get "d['redactions']")"
N_DIGEST="$(HUB_ROOT="$ROOT2" HUB_CONFIG="$CFG_OK" python3 -c "
import sys; sys.path.insert(0,'src')
from infohub.config import load
from infohub.store import Store
c=load(); print(len(Store(c.db_path).digests()))")"
[ "$PC" = "dry-run" ] && [ "$HAS_SEED" = "True" ] && [ "$LEAK" = "False" ] \
  && [ "$MASKED" -ge 1 ] && [ "$N_DIGEST" = "0" ] \
  && pass "T2 dry-run 组装提示词、凭据已脱敏、不写库" \
  || fail "T2 dry-run" "reason=$PC seed=$HAS_SEED leak=$LEAK redactions=$MASKED digests=$N_DIGEST"

# ---------------------------------------------------------------- T3 正常归纳：落库 + evidence 指向真实事件
O3="$(runq "$CFG_OK" "$ROOT2" run)"
R3="$(printf '%s' "$O3" | json_get "d['reason']")"
N3="$(printf '%s' "$O3" | json_get "len(d['items'])")"
D3="$(printf '%s' "$O3" | json_get "d['dropped']")"
EV_ALL="$(printf '%s' "$O3" | json_get "all(isinstance(e,int) for i in d['items'] for e in i['evidence'])")"
O3B="$(runq "$CFG_OK" "$ROOT2" run)"
R3B="$(printf '%s' "$O3B" | json_get "d['reason']")"
[ "$R3" = "ok" ] && [ "$N3" = "2" ] && [ "$D3" = "0" ] && [ "$EV_ALL" = "True" ] \
  && [ "$R3B" = "reused" ] && pass "T3 归纳落库 + 同输入复用（不重复花 token）" \
  || fail "T3 归纳落库" "reason=$R3 items=$N3 dropped=$D3 second=$R3B"

# ---------------------------------------------------------------- T4 无依据的条目被丢弃
CFG_BOGUS="$TMP/bogus.yaml"; ROOT4="$TMP/r4"; mkcfg "$CFG_BOGUS" bogus
seed "$ROOT4"
O4="$(runq "$CFG_BOGUS" "$ROOT4" run)"
N4="$(printf '%s' "$O4" | json_get "len(d['items'])")"
D4="$(printf '%s' "$O4" | json_get "d['dropped']")"
BAD4="$(printf '%s' "$O4" | json_get "any(99999999 in i['evidence'] for i in d['items'])")"
[ "$N4" = "1" ] && [ "$D4" = "1" ] && [ "$BAD4" = "False" ] \
  && pass "T4 挂不存在事件 id 的条目被丢弃" || fail "T4 丢弃无依据" "items=$N4 dropped=$D4 bad=$BAD4"

# ---------------------------------------------------------------- T5 输出不是 JSON ⇒ 致命、不写库
CFG_BAD="$TMP/nojson.yaml"; ROOT5="$TMP/r5"; mkcfg "$CFG_BAD" nojson
seed "$ROOT5"
O5="$(runq "$CFG_BAD" "$ROOT5" run; echo "exit=$?")"
EX5="$(printf '%s' "$O5" | tail -1)"
N5="$(HUB_ROOT="$ROOT5" HUB_CONFIG="$CFG_BAD" python3 -c "
import sys; sys.path.insert(0,'src')
from infohub.config import load
from infohub.store import Store
c=load(); print(len(Store(c.db_path).digests()))")"
[ "$EX5" = "exit=1" ] && [ "$N5" = "0" ] && pass "T5 非 JSON ⇒ 退出 1 且不写库" \
  || fail "T5 非 JSON" "ex=$EX5 digests=$N5 out=$O5"

# ---------------------------------------------------------------- T6 加工层崩了 ⇒ 退出 1、不写库
CFG_FAIL="$TMP/fail.yaml"; ROOT6="$TMP/r6"; mkcfg "$CFG_FAIL" fail
seed "$ROOT6"
O6="$(runq "$CFG_FAIL" "$ROOT6" run; echo "exit=$?")"
EX6="$(printf '%s' "$O6" | tail -1)"
[ "$EX6" = "exit=1" ] && pass "T6 加工层退出码非 0 ⇒ 退出 1" || fail "T6 加工层失败" "ex=$EX6 out=$O6"

# ---------------------------------------------------------------- T7 空条目也能落库
CFG_EMPTY="$TMP/empty.yaml"; ROOT7="$TMP/r7"; mkcfg "$CFG_EMPTY" empty
seed "$ROOT7"
O7="$(runq "$CFG_EMPTY" "$ROOT7" run)"
[ "$(printf '%s' "$O7" | json_get "d['reason']")" = "ok" ] \
  && [ "$(printf '%s' "$O7" | json_get "len(d['items'])")" = "0" ] \
  && pass "T7 空条目：合法但无内容（不改状态）" || fail "T7 空条目" "out=$O7"

# ---------------------------------------------------------------- T8 报表区块带 evidence
RPT="$TMP/r2/reports/hub-report.md"
HUB_ROOT="$ROOT2" HUB_CONFIG="$CFG_OK" python3 -m infohub.entry_report generate >/dev/null 2>&1
BLK="$(grep -c "hub:auto:begin digest" "$RPT" || true)"
EVLINE="$(grep -c "evidence: #" "$RPT" || true)"
TXT="$(grep -c "测试归纳" "$RPT" || true)"
[ "$BLK" = "1" ] && [ "$EVLINE" -ge 1 ] && [ "$TXT" -ge 1 ] && pass "T8 报表 digest 区块含 evidence" \
  || fail "T8 报表 digest 区块" "block=$BLK evidence_lines=$EVLINE texts=$TXT"

# ---------------------------------------------------------------- T9 真库提示词也能组装（不调加工层）
REAL="$(HUB_ROOT="$HOME/.dsh/info-hub" HUB_CONFIG="$ROOT/hub.config.yaml" python3 -c "
import sys; sys.path.insert(0,'src')
from infohub.config import load
from infohub.store import Store
from infohub import views
c=load(); s=Store(c.db_path)
digest=views.module(c, 'digest')   # S15：归纳实现是 view 插件，按清单取，不再从这里 import 源码
p=digest.build_input(c, s, window_days=30)
prompt=digest.render_prompt(c, p)
print(len(p['events']), len(prompt), p['redactions'])")"
read -r N9 L9 RD9 <<< "$REAL"
[ "${N9:-0}" -gt 0 ] && [ "${L9:-0}" -gt 100 ] && pass "T9 真库可组装提示词（事件 $N9 条/脱敏 $RD9 处）" \
  || fail "T9 真库提示词" "events=$N9 chars=$L9"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
