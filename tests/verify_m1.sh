#!/usr/bin/env bash
# info-hub M1 验收。判据见 PROJECT.md §9（造合成事件 → 恰好 N 条待办；重复跑不重复建卡）。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
RULES=(python3 -m infohub.entry_rules)
STORE=(python3 -m infohub.entry_store)

TMP="$(mktemp -d /tmp/infohub-m1-XXXXXX)"
export HUB_ROOT="$TMP/data"
export HUB_CONFIG="$ROOT/hub.config.yaml"
mkdir -p "$HUB_ROOT"

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# ---------------------------------------------------------------- 造合成事件
python3 - "$TMP" <<'PY'
import sys
from datetime import datetime, timedelta
sys.path.insert(0, sys.argv[0] if False else "src")
from infohub.config import load
from infohub.events import make_event
from infohub.store import Store

cfg = load(); cfg.ensure_dirs()
store = Store(cfg.db_path); store.migrate()
now = datetime.now().astimezone()
iso = lambda d: d.isoformat(timespec="seconds")
events = []
for i in range(3):                      # 规则 1：同一文件 3 次改动
    events.append(make_event("fs", "fs.change",
                             {"root": "/tmp", "path": "code/a.py", "mtime": now.timestamp() - i * 60,
                              "size": 10}, ts=iso(now - timedelta(hours=i + 1))))
for i in range(3):                      # 规则 3：同域剪贴板链接 3 条
    events.append(make_event("clipboard", "clip.copy",
                             {"head": f"https://example.com/page{i}", "chars": 24, "truncated": False},
                             ts=iso(now - timedelta(minutes=i + 1)), privacy="sensitive"))
events.append(make_event("dsh-session", "assistant/message",        # 规则 2：3 天没收尾
                         {"session_id": "sess-stale-1", "seq": 5, "cwd": "/tmp",
                          "event_type": "assistant/message", "head": "x"},
                         ts=iso(now - timedelta(days=3))))
events.append(make_event("dsh-session", "turn/end",                 # 反例：刚收尾，不该成待办
                         {"session_id": "sess-fresh-1", "seq": 2, "cwd": "/tmp",
                          "event_type": "turn/end", "head": ""}, ts=iso(now - timedelta(minutes=1))))
inserted, dup = store.insert_events(events)
print(f"seeded={inserted} dup={dup}")
PY

# ---------------------------------------------------------------- T1 规则 → 恰好 3 条
OUT1="$("${RULES[@]}" --json derive)"
C1="$(printf '%s' "$OUT1" | json_get "len(d['created'])")"
RULES1="$(printf '%s' "$OUT1" | json_get "','.join(sorted(i['rule'] for i in d['items']))")"
[ "${C1:-0}" = "3" ] && pass "T1 derive-3-todos" || fail "T1 derive-3-todos" "created=$C1 out=$OUT1"
[ "$RULES1" = "clipboard-domain-repeat,fs-repeat,session-stale" ] && pass "T1b rule-set" \
  || fail "T1b rule-set" "rules=$RULES1"

# ---------------------------------------------------------------- T2 幂等：再跑不新建
OUT2="$("${RULES[@]}" --json derive)"
C2="$(printf '%s' "$OUT2" | json_get "len(d['created'])")"
F2="$(printf '%s' "$OUT2" | json_get "len(d['refreshed'])")"
[ "${C2:-x}" = "0" ] && [ "${F2:-0}" = "3" ] && pass "T2 idempotent" \
  || fail "T2 idempotent" "created=$C2 refreshed=$F2"

# ---------------------------------------------------------------- T3 待办可查
ROWS3="$("${STORE[@]}" todos --json | json_get "len(d)")"
[ "${ROWS3:-0}" = "3" ] && pass "T3 todos-list" || fail "T3 todos-list" "rows=$ROWS3"

# ---------------------------------------------------------------- T4 人改状态不被规则覆盖
TID="$(python3 -c "
import json,subprocess
out=subprocess.run(['${STORE[0]}','-m','infohub.entry_store','todos','--json'],capture_output=True,text=True)
print(json.loads(out.stdout)[0]['id'])")"
"${STORE[@]}" todo-set "$TID" done --json >/dev/null
"${RULES[@]}" --json derive >/dev/null
read -r ST4 HO4 <<<"$(python3 - "$TID" <<'PY'
import json, subprocess, sys
out = subprocess.run(['python3','-m','infohub.entry_store','todos','--json'],capture_output=True,text=True)
rows = {r['id']: r for r in json.loads(out.stdout)}
r = rows[sys.argv[1]]
print(r['status'], r['human_override'])
PY
)"
[ "$ST4" = "done" ] && [ "$HO4" = "1" ] && pass "T4 human-override-kept" \
  || fail "T4 human-override-kept" "status=$ST4 human_override=$HO4"

# ---------------------------------------------------------------- T5/T6 看板同步（假服务器）
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BLOG="$TMP/board.log"
python3 "$HERE/board_stub.py" "$PORT" "$BLOG" &
STUB=$!
sleep 0.7
python3 - "$ROOT/hub.config.yaml" "$TMP/board.config.yaml" "$PORT" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["board"].update({"enabled": True, "base_url": f"http://127.0.0.1:{sys.argv[3]}"})
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
OUT5="$(HUB_CONFIG="$TMP/board.config.yaml" "${RULES[@]}" --json board-sync)"
P5="$(printf '%s' "$OUT5" | json_get "len(d['pushed'])")"
POSTS5="$(wc -l < "$BLOG" 2>/dev/null || echo 0)"
[ "${P5:-0}" = "2" ] && [ "$POSTS5" = "2" ] && pass "T5 board-sync-open-only" \
  || fail "T5 board-sync-open-only" "pushed=$P5 posts=$POSTS5 out=$OUT5"
python3 - <<'PY' >/dev/null 2>&1
import json, subprocess
out = subprocess.run(['python3','-m','infohub.entry_store','todos','--json'],capture_output=True,text=True)
rows = json.loads(out.stdout)
assert sum(1 for r in rows if r['board_card_id']) == 2, rows
PY
[ $? -eq 0 ] && pass "T5b card-id-stored" || fail "T5b card-id-stored"

OUT6="$(HUB_CONFIG="$TMP/board.config.yaml" "${RULES[@]}" --json board-sync)"
P6="$(printf '%s' "$OUT6" | json_get "len(d['pushed'])")"
POSTS6="$(wc -l < "$BLOG" 2>/dev/null || echo 0)"
[ "${P6:-x}" = "0" ] && [ "$POSTS6" = "2" ] && pass "T6 board-sync-idempotent" \
  || fail "T6 board-sync-idempotent" "pushed=$P6 posts=$POSTS6"

# ---------------------------------------------------------------- T7 默认关：一个请求都不发
OUT7="$(HUB_CONFIG="$ROOT/hub.config.yaml" "${RULES[@]}" --json board-sync)"
EN7="$(printf '%s' "$OUT7" | json_get "d['enabled']")"
POSTS7="$(wc -l < "$BLOG" 2>/dev/null || echo 0)"
[ "$EN7" = "False" ] && [ "$POSTS7" = "2" ] && pass "T7 board-default-off" \
  || fail "T7 board-default-off" "enabled=$EN7 posts=$POSTS7"

# ---------------------------------------------------------------- T8 dry-run 不写库
python3 - <<'PY'
import sys
from datetime import datetime, timedelta
from infohub.config import load
from infohub.events import make_event
from infohub.store import Store
cfg = load(); store = Store(cfg.db_path); store.migrate()
now = datetime.now().astimezone()
evs = [make_event("fs", "fs.change", {"root": "/tmp", "path": "code/b.py", "mtime": now.timestamp(),
                                      "size": 3}, ts=(now - timedelta(minutes=i)).isoformat(timespec="seconds"))
       for i in range(3)]
store.insert_events(evs)
PY
OUT8="$(HUB_CONFIG="$ROOT/hub.config.yaml" "${RULES[@]}" --json --dry-run derive)"
C8="$(printf '%s' "$OUT8" | json_get "len(d['created'])")"
# dry-run 的 created 是"将要新建"，库里条数应保持 3
ROWS8="$("${STORE[@]}" todos --json | json_get "len(d)")"
[ "${C8:-0}" -ge 1 ] && [ "${ROWS8:-0}" = "3" ] && pass "T8 dry-run" \
  || fail "T8 dry-run" "would_create=$C8 rows=$ROWS8"

# ---------------------------------------------------------------- T9 规则开关
python3 - "$ROOT/hub.config.yaml" "$TMP/off.config.yaml" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["rules"]["fs-repeat"]["enabled"] = False
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
OUT9="$(HUB_CONFIG="$TMP/off.config.yaml" "${RULES[@]}" --json --dry-run derive)"
HAS9="$(printf '%s' "$OUT9" | json_get "'fs-repeat' in [i['rule'] for i in d['items']]")"
[ "$HAS9" = "False" ] && pass "T9 rule-switch" || fail "T9 rule-switch" "has_fs=$HAS9"

kill "$STUB" 2>/dev/null; wait "$STUB" 2>/dev/null
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
rm -rf "$TMP"
[ "$FAIL" -eq 0 ]
