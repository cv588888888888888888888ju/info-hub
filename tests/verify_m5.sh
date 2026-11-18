#!/usr/bin/env bash
# info-hub M5 验收：bilibili 账号监测（可替换来源 → 机械提工具 → KB 对账 → 只出待办）。
# 全程离线：来源走 tests/bili_source_stub.py，KB 是临时目录。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
COLLECT=(python3 -m infohub.entry_collect)
RULES=(python3 -m infohub.entry_rules)
STUB="$HERE/bili_source_stub.py"

TMP="$(mktemp -d /tmp/infohub-m5-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

KB="$TMP/kb"
mkdir -p "$KB"
cat > "$KB/工具清单.md" <<'MD'
# 已经整合进知识库的工具
- ownerA/present-tool（窗口采集 / 已写过笔记）
- 另一个只用名字提到的：nameonly-tool
MD

mkcfg() {  # mkcfg <out> <mode> [key=value ...]  —— key 形如 collect.bilibili.enabled / rules.bili-unintegrated.kb_root
  local out="$1" mode="$2"; shift 2
  python3 - "$ROOT/hub.config.yaml" "$out" "$STUB" "$mode" "$KB" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["collect"]["bilibili"].update({"enabled": True, "source": "command",
                                   "command": ["python3", sys.argv[3], "--mode", sys.argv[4]]})
raw["rules"]["bili-unintegrated"]["kb_root"] = sys.argv[5]
for kv in sys.argv[6:]:
    k, v = kv.split("=", 1)
    node = raw
    parts = k.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = yaml.safe_load(v)
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
}

collectq() {  # collectq <cfg> <root> [args...]
  local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" "${COLLECT[@]}" --json --quiet "$@"
}
rulesq() {
  local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" "${RULES[@]}" --json "$@"
}
sql() {  # sql <root> <python-expr with s=Store, c=Config>
  local root="$1" expr="$2"
  HUB_ROOT="$root" python3 - "$expr" <<'PY'
import sys
sys.path.insert(0, "src")
from infohub.config import load
from infohub.store import Store
c = load(); s = Store(c.db_path)
print(eval(sys.argv[1]))
PY
}

CFG="$TMP/c.yaml"; R="$TMP/r"; mkcfg "$CFG" ok

# ---------------------------------------------------------------- T1 采集：新条目入库、游标挡住重复
O1="$(collectq "$CFG" "$R" --source bilibili)"
EM1="$(printf '%s' "$O1" | tail -1 | json_get "d['sources']['bilibili']['emitted']")"
IG1="$(printf '%s' "$O1" | tail -1 | json_get "d['ingested']")"
[ "$EM1" = "5" ] && [ "$IG1" = "5" ] && pass "T1 首次采集 5 条新条目入库" \
  || fail "T1 首次采集" "emitted=$EM1 ingested=$IG1"
O1B="$(collectq "$CFG" "$R" --source bilibili)"
EM1B="$(printf '%s' "$O1B" | tail -1 | json_get "d['sources']['bilibili']['emitted']")"
[ "$EM1B" = "0" ] && pass "T1b 再采一轮：游标挡住重复（emitted=0）" \
  || fail "T1b 游标" "emitted=$EM1B"

# ---------------------------------------------------------------- T2 规则：只有「KB 里查不到」的那个工具出待办
O2="$(rulesq "$CFG" "$R" derive)"
CR2="$(printf '%s' "$O2" | json_get "len(d['created'])")"
K2="$(printf '%s' "$O2" | json_get "sorted(x['key'].rsplit(':',1)[0] for x in d['items'])")"
HAS_PRESENT="$(printf '%s' "$O2" | json_get "any('present-tool' in (x['key']+x['title']) for x in d['items'])")"
[ "$CR2" = "1" ] && [ "$K2" = "['bili-unintegrated:ownerb/absent-tool']" ] \
  && pass "T2 恰好 1 条待办：ownerB/absent-tool（present/nameonly 静默）" \
  || fail "T2 规则" "created=$CR2 keys=$K2"
[ "$HAS_PRESENT" = "False" ] && pass "T2b 已整合的工具（含只用名字命中的）不派待办" \
  || fail "T2b present 静默" "got=$HAS_PRESENT"

# ---------------------------------------------------------------- T3 幂等：再跑一次不新建
O3="$(rulesq "$CFG" "$R" derive)"
CR3="$(printf '%s' "$O3" | json_get "len(d['created'])")"
RF3="$(printf '%s' "$O3" | json_get "len(d['refreshed'])")"
[ "$CR3" = "0" ] && [ "$RF3" = "1" ] && pass "T3 再跑幂等：created=0 refreshed=1（同一工具的两个视频合成一条）" \
  || fail "T3 幂等" "created=$CR3 refreshed=$RF3"

# ---------------------------------------------------------------- T4 KB 判定有缓存：第二次不再全文检索
AT4="$(sql "$R" "s.state_get('kb:ownerb/absent-tool')['cursor']['at']" | tail -1)"
AT4B="$(sql "$R" "s.state_get('kb:ownerb/absent-tool')['cursor']['at']" | tail -1)"
sleep 1
rulesq "$CFG" "$R" derive >/dev/null
AT4C="$(sql "$R" "s.state_get('kb:ownerb/absent-tool')['cursor']['at']" | tail -1)"
FOUND4="$(sql "$R" "s.state_get('kb:ownerb/absent-tool')['cursor']['found']" | tail -1)"
[ "$AT4" = "$AT4C" ] && [ "$FOUND4" = "False" ] \
  && pass "T4 KB 判定落缓存（found=False，重跑不改 at=不再检索）" \
  || fail "T4 缓存" "at=$AT4 at_after=$AT4C found=$FOUND4"

# ---------------------------------------------------------------- T5 fail-closed：KB 不可读 ⇒ 报错且不派假待办
CFG5="$TMP/c5.yaml"; R5="$TMP/r5"; mkcfg "$CFG5" ok "rules.bili-unintegrated.kb_root=/nonexistent-kb-xyz"
collectq "$CFG5" "$R5" --source bilibili >/dev/null
O5="$(rulesq "$CFG5" "$R5" derive)"
CR5="$(printf '%s' "$O5" | json_get "len(d['created'])")"
ER5="$(printf '%s' "$O5" | json_get "len(d['errors'])")"
[ "$CR5" = "0" ] && [ "${ER5:-0}" -ge 1 ] && pass "T5 KB 读不了 ⇒ 记 error、0 待办（fail-closed）" \
  || fail "T5 fail-closed" "created=$CR5 errors=$ER5"

# ---------------------------------------------------------------- T6 默认关：enabled=false 时一条都不采
CFG6="$TMP/c6.yaml"; R6="$TMP/r6"; mkcfg "$CFG6" ok "collect.bilibili.enabled=false"
O6="$(collectq "$CFG6" "$R6" --source bilibili)"
EM6="$(printf '%s' "$O6" | tail -1 | json_get "d['sources']['bilibili']['emitted']")"
[ "$EM6" = "0" ] && pass "T6 collect.bilibili.enabled=false ⇒ 不采" || fail "T6 默认关" "emitted=$EM6"

# ---------------------------------------------------------------- T7 来源失败不致命（退出码仍 0、记 error）
CFG7="$TMP/c7.yaml"; R7="$TMP/r7"; mkcfg "$CFG7" fail
O7="$(collectq "$CFG7" "$R7" --source bilibili; echo "exit=$?")"
EX7="$(printf '%s' "$O7" | tail -1)"
OK7="$(printf '%s' "$O7" | grep -o '"ok": false' | head -1)"
[ "$EX7" = "exit=0" ] && [ -n "$OK7" ] && pass "T7 来源命令失败 ⇒ 退出码 0 + 记 error（不影响主路）" \
  || fail "T7 来源失败" "ex=$EX7 ok=$OK7"
CFG7B="$TMP/c7b.yaml"; R7B="$TMP/r7b"; mkcfg "$CFG7B" badjson
O7B="$(collectq "$CFG7B" "$R7B" --source bilibili; echo "exit=$?")"
[ "$(printf '%s' "$O7B" | tail -1)" = "exit=0" ] && pass "T7b 来源输出非 JSON ⇒ 同样不致命" \
  || fail "T7b 非 JSON" "out=$O7B"

# ---------------------------------------------------------------- T8 KB 检索本身：大小写不敏感、词边界可用
O8="$(PYTHONPATH="$ROOT/src" python3 - "$KB" <<'PY' | tail -1
import sys
from infohub import kb
root = sys.argv[1]
print(kb.contains(root, "OWNERA/PRESENT-TOOL")[0], kb.contains(root, "nameonly-tool", word=True)[0],
      kb.contains(root, "present", word=True)[0], kb.contains(root, "nameonly", word=True)[0])
PY
)"
[ "$O8" = "True True False False" ] && pass "T8 KB 检索：大小写不敏感 + 词边界生效" || fail "T8 KB 检索" "got=$O8"

# ---------------------------------------------------------------- T9 提工具：只认机械形态（不猜裸名字）
O9="$(PYTHONPATH="$ROOT/src" python3 - <<'PY' | tail -1
from infohub import bili
items = [{"id": "x1", "title": "github.com/o/r/blob/main/a.py", "url": ""},
         {"id": "x2", "title": "2026/10 月度总结", "url": ""},
         {"id": "x3", "title": "see https://github.com/baz/qux.git", "url": ""},
         {"id": "x4", "title": "一个没有斜杠的裸名字 toolname", "url": ""}]
print(sorted(t["key"] for t in bili.extract_tools(items)))
PY
)"
[ "$O9" = "['baz/qux']" ] && pass "T9 提工具只认机械形态（裸名字不猜、日期不误报）" || fail "T9 提工具" "got=$O9"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
