#!/usr/bin/env bash
# info-hub M4d 验收：每周归纳（本地确定性后端 → 接进归纳层 → 单元/安装面）。
# 全程离线、可重复：临时 HUB_ROOT/HUB_CONFIG，不联网、不弹通知、不碰真库（~/.dsh/info-hub 零访问）。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
LOCAL=("$ROOT/bin/hub-digest-local")
DIGEST=("$ROOT/bin/hub-digest")

TMP="$(mktemp -d /tmp/infohub-m4d-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }

mkcfg() {  # mkcfg <out> [k=v ...] —— 从仓库配置派生（digest.local.* 缺省也能跑）
  local out="$1"; shift
  python3 - "$ROOT/hub.config.yaml" "$out" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for kv in sys.argv[3:]:
    k, v = kv.split("=", 1)
    node = raw
    parts = k.split(".")
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = yaml.safe_load(v)
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
}

seed() {  # seed <root> <cfg> —— 5 条事件（fs×3、clipboard×2）+ 1 条 open 待办（含 1 个不存在的事件 id）
  local root="$1" cfg="$2"
  HUB_ROOT="$root" HUB_CONFIG="$cfg" python3 - <<'PY'
from datetime import datetime, timedelta
from infohub.config import load
from infohub.events import make_event
from infohub.store import Store
cfg = load(); cfg.ensure_dirs()
store = Store(cfg.db_path); store.migrate()
now = datetime.now().astimezone()
iso = lambda d: d.isoformat(timespec="seconds")
events = [make_event("fs", "fs.change", {"path": f"/tmp/m4d/x{i}.py"},
                     ts=iso(now - timedelta(minutes=30 - i))) for i in range(3)]
events += [make_event("clipboard", "clip.copy", {"text": f"m4d 样本 {i}"},
                      ts=iso(now - timedelta(minutes=10 - i))) for i in range(2)]
store.insert_events(events)
ids = sorted(int(r["id"]) for r in store.events(limit=100))
store.upsert_todo({"dedupe_key": "m4d:synthetic", "title": "M4d 合成待办标题",
                   "why": "合成", "confidence": 0.5,
                   "source_event_ids": ids[:3] + [99999999]})
store.close()
print("seeded", len(ids))
PY
}

dcount() {  # dcount <cfg> <root> —— "<digests 行数> <最新一行 items 条数>"
  HUB_ROOT="$2" python3 - "$1" <<'PY'
import sys
from infohub.config import load
from infohub.store import Store
c = load(sys.argv[1]); s = Store(c.db_path)
rows = s.digests(limit=10)
print(len(rows), len(rows[0]["items"]) if rows else 0)
PY
}

# ---------------------------------------------------------------- T1 后端契约：合法 JSON + 证据真实 + 条数/字数上限 + 只引用提示词里的 id
CFG1="$TMP/c1.yaml"; R1="$TMP/r1"; mkcfg "$CFG1" "digest.enabled=true"
seed "$R1" "$CFG1" >/dev/null
# 真实提示词（走归纳层自己组装），不是手写的：本地后端只许引用提示词里出现过的事件 id
HUB_ROOT="$R1" HUB_CONFIG="$CFG1" "${DIGEST[@]}" --quiet --json run --dry-run --window-days 7 \
  >"$TMP/p1.json" 2>"$TMP/ep1.txt"
python3 -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['prompt'],end='')" \
  "$TMP/p1.json" >"$TMP/p1.txt"
HUB_ROOT="$R1" "${LOCAL[@]}" --window-days 7 --config "$CFG1" <"$TMP/p1.txt" >"$TMP/o1.json" 2>"$TMP/e1.txt"
EX1=$?
T1="$(python3 - "$R1" "$TMP/o1.json" "$CFG1" "$TMP/p1.txt" <<'PY'
import glob, json, os, re, sqlite3, sys, yaml
root, out_path, cfg_path, prompt_path = sys.argv[1:5]
problems = []
try:
    data = json.load(open(out_path, encoding="utf-8"))
except Exception as exc:
    print(f"输出不是合法 JSON: {exc}"); raise SystemExit
items = data.get("items") if isinstance(data, dict) else None
if not isinstance(items, list):
    print("输出缺少 items 数组"); raise SystemExit
cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
max_items = int(cfg["digest"]["max_items"])
if len(items) > max_items:
    problems.append(f"条数 {len(items)} > max_items {max_items}")
if not items:
    problems.append("有内容却产出 0 条")
ids = set()
dbs = glob.glob(os.path.join(root, "*.db"))
if dbs:
    conn = sqlite3.connect(dbs[0])
    try:
        ids = {int(r[0]) for r in conn.execute("SELECT id FROM events")}
    finally:
        conn.close()
else:
    problems.append("临时库里没有 db 文件")
prompt_ids = {int(m) for m in re.findall(r"^- (\d+) \|",
              open(prompt_path, encoding="utf-8").read(), re.M)}
for i, item in enumerate(items):
    ev = item.get("evidence") or []
    if not ev:
        problems.append(f"#{i} evidence 为空")
    for e in ev:
        if not isinstance(e, int) or isinstance(e, bool) or e not in ids:
            problems.append(f"#{i} evidence {e!r} 不在 events 表")
        elif e not in prompt_ids:
            problems.append(f"#{i} evidence {e!r} 不在提示词里（审计会丢）")
    if len(str(item.get("text") or "")) > 60:
        problems.append(f"#{i} text 超过 60 字")
if items and not str(items[0].get("text") or "").startswith("待办："):
    problems.append("待办没有优先输出")
if len(items) >= 3:
    if " 条事件里 fs 占 3 条，最新 " not in str(items[1].get("text") or ""):
        problems.append(f"事件分组第 1 条不是 fs：{items[1].get('text')}")
    if " 条事件里 clipboard 占 2 条，最新 " not in str(items[2].get("text") or ""):
        problems.append(f"事件分组未按条数倒序：{items[2].get('text')}")
else:
    problems.append(f"分组条目数不足：{len(items)}")
print("ok" if not problems else " | ".join(problems))
PY
)"
[ "${EX1:-1}" = "0" ] && [ "$T1" = "ok" ] && pass "T1 后端契约（证据全真且都在提示词里、条数≤max_items、text≤60字、待办优先、分组倒序）" \
  || fail "T1 后端契约" "exit=$EX1 checks=$T1 stderr=$(head -c 200 "$TMP/e1.txt")"

HUB_ROOT="$R1" "${LOCAL[@]}" --window-days 7 --config "$CFG1" <"$TMP/p1.txt" >"$TMP/o1b.json" 2>/dev/null
cmp -s "$TMP/o1.json" "$TMP/o1b.json" && pass "T1b 相同输入两次跑输出逐字节相同（确定性、顺序固定）" \
  || fail "T1b 确定性" "$(diff "$TMP/o1.json" "$TMP/o1b.json" | head -4)"

# 提示词里没有任何事件 id（上游空窗口/提示词被截断）⇒ 一条都不许引用，绝不编依据
printf '这是上游提示词，里面没有任何事件流水。\n' | \
  HUB_ROOT="$R1" "${LOCAL[@]}" --window-days 7 --config "$CFG1" >"$TMP/o1c.json" 2>/dev/null
T1C="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))=={'items':[]})" "$TMP/o1c.json" 2>/dev/null)"
[ "$T1C" = "True" ] && pass "T1c 提示词里没有事件 id ⇒ {\"items\": []}（无依据不发）" \
  || fail "T1c 无 id 提示词" "eq=$T1C out=$(head -c 200 "$TMP/o1c.json")"

# ---------------------------------------------------------------- T2 无依据不出条目：空库 ⇒ {"items": []}
CFG2="$TMP/c2.yaml"; R2="$TMP/r2"; mkcfg "$CFG2"
printf 'prompt\n' | HUB_ROOT="$R2" "${LOCAL[@]}" --window-days 7 --config "$CFG2" >"$TMP/o2.json" 2>"$TMP/e2.txt"
EX2=$?
T2="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))=={'items':[]})" "$TMP/o2.json" 2>/dev/null)"
[ "${EX2:-1}" = "0" ] && [ "$T2" = "True" ] && pass "T2 空窗口 ⇒ {\"items\": []}（不编一句话凑数）" \
  || fail "T2 空窗口" "exit=$EX2 eq=$T2 out=$(head -c 200 "$TMP/o2.json")"

# ---------------------------------------------------------------- T3 接进归纳层：落库 1 行 → 同输入复用（不再调后端）
CFG3="$TMP/c3.yaml"; R3="$TMP/r3"
mkcfg "$CFG3" "digest.enabled=true" \
  "digest.command=[\"$ROOT/bin/hub-digest-local\", \"--window-days\", \"7\", \"--config\", \"$CFG3\"]"
seed "$R3" "$CFG3" >/dev/null
HUB_ROOT="$R3" HUB_CONFIG="$CFG3" "${DIGEST[@]}" --quiet --json run --window-days 7 >"$TMP/o3.json" 2>"$TMP/e3.txt"
EX3=$?
read -r RSN3 N3 <<< "$(python3 - "$TMP/o3.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(d.get("reason"), len(d.get("items") or []))
PY
)"
read -r CNT3 ROW3 <<< "$(dcount "$CFG3" "$R3")"
[ "${EX3:-1}" = "0" ] && [ "$RSN3" = "ok" ] && [ "${N3:-0}" -gt 0 ] \
  && [ "$CNT3" = "1" ] && [ "${ROW3:-0}" -gt 0 ] \
  && pass "T3 归纳层接线：退出 0、digests +1 行、items 非空（$N3 条）" \
  || fail "T3 归纳层接线" "exit=$EX3 reason=$RSN3 items=$N3 digests=$CNT3/${ROW3:-?} err=$(head -c 200 "$TMP/e3.txt")"

HUB_ROOT="$R3" HUB_CONFIG="$CFG3" "${DIGEST[@]}" --quiet --json run --window-days 7 >"$TMP/o3b.json" 2>/dev/null
EX3B=$?
RSN3B="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8')).get('reason'))" "$TMP/o3b.json" 2>/dev/null)"
read -r CNT3B _ <<< "$(dcount "$CFG3" "$R3")"
[ "${EX3B:-1}" = "0" ] && [ "$RSN3B" = "reused" ] && [ "$CNT3B" = "1" ] \
  && pass "T3b 同输入再跑 ⇒ reused、digests 不增行" \
  || fail "T3b 复用" "exit=$EX3B reason=$RSN3B digests=$CNT3B"

# 把加工层换成不存在的命令：仍 reused ⇒ 机械证明第二次没调后端
CFG3C="$TMP/c3c.yaml"; mkcfg "$CFG3C" "digest.enabled=true" 'digest.command=["/nonexistent/m4d-backend"]'
HUB_ROOT="$R3" HUB_CONFIG="$CFG3C" "${DIGEST[@]}" --quiet --json run --window-days 7 >"$TMP/o3c.json" 2>"$TMP/e3c.txt"
EX3C=$?
RSN3C="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8')).get('reason'))" "$TMP/o3c.json" 2>/dev/null)"
[ "${EX3C:-1}" = "0" ] && [ "$RSN3C" = "reused" ] \
  && pass "T3c 复用判在调后端之前（前端换不存在命令仍 reused）" \
  || fail "T3c 复用不调后端" "exit=$EX3C reason=$RSN3C err=$(head -c 200 "$TMP/e3c.txt")"

# ---------------------------------------------------------------- T5 待办证据老于 max_events：仍要进提示词，且被审计接受
CFG5="$TMP/c5.yaml"; R5="$TMP/r5"
mkcfg "$CFG5" "digest.enabled=true" "digest.max_events=5" \
  "digest.command=[\"$ROOT/bin/hub-digest-local\", \"--window-days\", \"7\", \"--config\", \"$CFG5\"]"
HUB_ROOT="$R5" HUB_CONFIG="$CFG5" python3 - <<'PY' >/dev/null
from datetime import datetime, timedelta
from infohub.config import load
from infohub.events import make_event
from infohub.store import Store
cfg = load(); cfg.ensure_dirs()
store = Store(cfg.db_path); store.migrate()
now = datetime.now().astimezone()
iso = lambda d: d.isoformat(timespec="seconds")
store.insert_events([make_event("fs", "fs.change", {"path": f"/tmp/m4d/old{i}.py"},
                                ts=iso(now - timedelta(hours=20 - i))) for i in range(2)])
store.insert_events([make_event("dsh-session", "step/end", {"n": i},
                                ts=iso(now - timedelta(minutes=30 - i))) for i in range(8)])
ids = sorted(int(r["id"]) for r in store.events(limit=100))
old = [r["id"] for r in store.events(limit=100) if r["source"] == "fs"]
store.upsert_todo({"dedupe_key": "m4d:old-evidence", "title": "证据很老的待办",
                   "why": "合成", "confidence": 0.5, "source_event_ids": sorted(old)})
store.close(); print("seeded", len(ids))
PY
HUB_ROOT="$R5" HUB_CONFIG="$CFG5" "${DIGEST[@]}" --quiet --json run --dry-run --window-days 7 \
  >"$TMP/p5.json" 2>/dev/null
OLD_IDS="$(HUB_ROOT="$R5" HUB_CONFIG="$CFG5" python3 -c "
from infohub.config import load
from infohub.store import Store
print(sorted(r['id'] for r in Store(load().db_path).events(limit=100) if r['source']=='fs'))")"
T5="$(python3 - "$TMP/p5.json" "$OLD_IDS" <<'PY'
import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
prompt_ids = {int(m) for m in re.findall(r"^- (\d+) \|", d.get("prompt") or "", re.M)}
want = [int(x) for x in sys.argv[2].strip("[]").split(",") if x.strip()]
missing = [i for i in want if i not in prompt_ids]
print("ok" if not missing else f"证据 {missing} 没进提示词")
PY
)"
[ "$T5" = "ok" ] && pass "T5 待办证据老于 max_events ⇒ 也进提示词（否则本地条目会被审计丢掉）" \
  || fail "T5 老证据进提示词" "$T5"
HUB_ROOT="$R5" HUB_CONFIG="$CFG5" "${DIGEST[@]}" --quiet --json run --window-days 7 >"$TMP/o5.json" 2>/dev/null
T5B="$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
items=d.get('items') or []
print('ok' if d.get('reason')=='ok' and any(str(i.get('text','')).startswith('待办：') for i in items) else f\"reason={d.get('reason')} items={items}\")" "$TMP/o5.json")"
[ "$T5B" = "ok" ] && pass "T5b 端到端：老证据的待办条目没被 M4b 审计丢掉" || fail "T5b 端到端保留" "$T5B"

# ---------------------------------------------------------------- T4 单元与安装面
T4T="$(python3 - "$ROOT/packaging/info-hub-digest.timer" "$ROOT/packaging/info-hub-week.timer" <<'PY'
import re, sys
def slot(path):
    m = re.search(r"^OnCalendar=(\w+) \*-\*-\* (\d+):(\d+):(\d+)",
                  open(path, encoding="utf-8").read(), re.M)
    return m
a, b = slot(sys.argv[1]), slot(sys.argv[2])
if not a or not b:
    print("OnCalendar 不匹配模板"); raise SystemExit
if a.group(1) != "Sun":
    print(f"归纳定时器不在周日：{a.group(1)}"); raise SystemExit
print("ok" if (int(a.group(2)), int(a.group(3))) < (int(b.group(2)), int(b.group(3)))
      else f"归纳 {a.group(2)}:{a.group(3)} 不早于周报 {b.group(2)}:{b.group(3)}")
PY
)"
[ "$T4T" = "ok" ] && pass "T4 digest.timer 每周日跑、且在周报定时器之前" || fail "T4 timer 日历" "$T4T"

SVC="$ROOT/packaging/info-hub-digest.service"
if grep -Eq '^ExecStart=.*/bin/hub-digest ' "$SVC"; then
  pass "T4b service 的 ExecStart 指向 bin/hub-digest"
else
  fail "T4b service ExecStart" "$(grep -m1 '^ExecStart=' "$SVC" || echo '缺 ExecStart')"
fi

DRY="$(XDG_CONFIG_HOME="$TMP/xdg" bash "$ROOT/bin/hub-install" --dry-run 2>&1)"
grep -q 'info-hub-digest.timer' <<< "$DRY" && pass "T4c hub-install --dry-run 已把 digest 定时器纳入安装面" \
  || fail "T4c 安装面" "$(printf '%s' "$DRY" | tail -3)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
