#!/usr/bin/env bash
# info-hub M4c 验收：周报（归纳条目/待办/通知按周聚合 → 只读文件，不碰可手改的报表）。
# 全程离线；不调加工层、不联网。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
WEEK=(python3 -m infohub.entry_week)
REPORT=(python3 -m infohub.entry_report)

TMP="$(mktemp -d /tmp/infohub-m4c-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
CFG="$ROOT/hub.config.yaml"
ROOTD="$TMP/root"

seed() {  # 造：本周 3 条事件（含左边界 1 条、右边界 1 条不计）+ 1 待办 + 1 已弹通知 + 1 次归纳(2 条带证据)
  HUB_ROOT="$ROOTD" HUB_CONFIG="$CFG" python3 - <<'PY'
from datetime import datetime
from infohub.config import load
from infohub.events import make_event
from infohub.store import Store
from infohub import views
cfg = load(); cfg.ensure_dirs()
W = views.module(cfg, 'week')   # S15：周报实现是 view 插件，按清单取，不再从这里 import 源码
store = Store(cfg.db_path); store.migrate()
now = datetime.now().astimezone()
start, end = W.week_bounds()
iso = lambda d: d.isoformat(timespec="seconds")
store.insert_events([
    make_event("fs", "fs.change", {"root": "/tmp", "path": "a.py", "mtime": now.timestamp(), "size": 1},
               ts=iso(now)),
    make_event("clipboard", "clip.copy", {"text": "hello", "sha256": "cd" * 32}, ts=iso(now)),
    make_event("fs", "fs.change", {"root": "/tmp", "path": "edge-start.py", "mtime": 0, "size": 1},
               ts=start.isoformat()),
    make_event("fs", "fs.change", {"root": "/tmp", "path": "edge-end.py", "mtime": 0, "size": 1},
               ts=end.isoformat()),
])
tid, _ = store.upsert_todo({"dedupe_key": "week-synth", "title": "周报测试待办", "why": "合成",
                            "confidence": 0.5, "source_event_ids": [1]})
store.mark_todo_notified(tid)
store.add_notification("week-synth", "通知正文", basis_event_ids=[1], shown=True, todo_id=tid)
store.add_digest(7, "stub", "p1", "h1", "{}",
                 items=[{"text": "周报条目甲", "evidence": [1, 2]},
                        {"text": "周报条目乙", "evidence": [3]}])
store.close()
print("seeded")
PY
}

seed

# ---------------------------------------------------------------- T1 生成：区间/计数/证据/留痕
HUB_ROOT="$ROOTD" HUB_CONFIG="$CFG" "${REPORT[@]}" generate >/dev/null 2>&1
RPT="$ROOTD/reports/hub-report.md"
RPT_BEFORE="$(md5sum "$RPT" | cut -d' ' -f1)"
OUT1="$(HUB_ROOT="$ROOTD" HUB_CONFIG="$CFG" "${WEEK[@]}" --json run)"
D1="$(printf '%s' "$OUT1" | tail -1)"
PATH1="$(printf '%s' "$D1" | json_get "d['path']")"
TOTAL1="$(printf '%s' "$D1" | json_get "d['event_total']")"
CREATED1="$(printf '%s' "$D1" | json_get "d['todos_created']")"
SHOWN1="$(printf '%s' "$D1" | json_get "d['notifications_shown']")"
ITEMS1="$(printf '%s' "$D1" | json_get "d['items_total']")"
[ -f "$PATH1" ] && [ "$TOTAL1" = "3" ] && [ "$CREATED1" = "1" ] && [ "$SHOWN1" = "1" ] \
  && [ "$ITEMS1" = "2" ] \
  && pass "T1 生成周报：事件 3（右边界不计）/待办 1/通知 1/条目 2" \
  || fail "T1 生成周报" "path=$PATH1 total=$TOTAL1 created=$CREATED1 shown=$SHOWN1 items=$ITEMS1"

# ---------------------------------------------------------------- T2 内容：概览表 + 每条带 evidence
HAS_EV="$(grep -c 'evidence: #1,#2' "$PATH1" || true)"
HAS_TXT="$(grep -c '周报条目甲' "$PATH1" || true)"
HAS_TBL="$(grep -c '| 入库事件 | 3 |' "$PATH1" || true)"
HAS_READONLY="$(grep -c '只读产物' "$PATH1" || true)"
[ "$HAS_EV" = "1" ] && [ "$HAS_TXT" = "1" ] && [ "$HAS_TBL" = "1" ] && [ "$HAS_READONLY" = "1" ] \
  && pass "T2 周报正文：条目带真实证据 + 概览计数 + 标明只读" \
  || fail "T2 周报正文" "ev=$HAS_EV txt=$HAS_TXT tbl=$HAS_TBL ro=$HAS_READONLY"

# ---------------------------------------------------------------- T3 不碰可手改的报表
RPT_AFTER="$(md5sum "$RPT" | cut -d' ' -f1)"
[ "$RPT_BEFORE" = "$RPT_AFTER" ] && pass "T3 周报不改 hub-report.md（可手改那份才留痕）" \
  || fail "T3 不碰报表" "before=$RPT_BEFORE after=$RPT_AFTER"

# ---------------------------------------------------------------- T4 dry-run：不落盘
ROOT4="$TMP/r4"; HUB_ROOT="$ROOT4" HUB_CONFIG="$CFG" python3 -c "
import sys; sys.path.insert(0, 'src')
from infohub.config import load
c = load(); c.ensure_dirs()" >/dev/null 2>&1
OUT4="$(HUB_ROOT="$ROOT4" HUB_CONFIG="$CFG" "${WEEK[@]}" run --dry-run)"
N4="$(find "$ROOT4/reports" -name 'hub-week-*.md' 2>/dev/null | wc -l)"
HAS_H4="$(printf '%s' "$OUT4" | grep -c '信息中枢周报' || true)"
[ "$N4" = "0" ] && [ "$HAS_H4" -ge 1 ] && pass "T4 --dry-run 只渲染不落盘" \
  || fail "T4 dry-run" "files=$N4 head=$HAS_H4"

# ---------------------------------------------------------------- T5 --week-of 指到过去那周：另落一个文件、计数为 0
PAST="$(date -d '30 days ago' +%F)"
OUT5="$(HUB_ROOT="$ROOTD" HUB_CONFIG="$CFG" "${WEEK[@]}" --json run --week-of "$PAST")"
D5="$(printf '%s' "$OUT5" | tail -1)"
PATH5="$(printf '%s' "$D5" | json_get "d['path']")"
TOTAL5="$(printf '%s' "$D5" | json_get "d['event_total']")"
N5="$(find "$ROOTD/reports" -name 'hub-week-*.md' | wc -l)"
[ -f "$PATH5" ] && [ "$PATH5" != "$PATH1" ] && [ "$TOTAL5" = "0" ] && [ "$N5" = "2" ] \
  && pass "T5 --week-of 过去那周：另一个文件、计数 0" \
  || fail "T5 --week-of" "path=$PATH5 total=$TOTAL5 files=$N5"

# ---------------------------------------------------------------- T6 --weeks 2：区间 14 天，仍落当周文件
OUT6="$(HUB_ROOT="$ROOTD" HUB_CONFIG="$CFG" "${WEEK[@]}" --json run --weeks 2)"
D6="$(printf '%s' "$OUT6" | tail -1)"
PATH6="$(printf '%s' "$D6" | json_get "d['path']")"
SPAN6="$(printf '%s' "$D6" | json_get "(__import__('datetime').datetime.fromisoformat(d['end'])-__import__('datetime').datetime.fromisoformat(d['start'])).days")"
[ "$SPAN6" = "14" ] && [ "$PATH6" = "$PATH1" ] && pass "T6 --weeks 2：区间 14 天且不倒回去覆盖历史周文件" \
  || fail "T6 --weeks 2" "span=$SPAN6 path6=$PATH6 path1=$PATH1"

# ---------------------------------------------------------------- T7 list 能列出来
OUT7="$(HUB_ROOT="$ROOTD" HUB_CONFIG="$CFG" "${WEEK[@]}" --json list)"
N7="$(printf '%s' "$OUT7" | json_get "len(d)")"
[ "${N7:-0}" -ge 2 ] && pass "T7 list 列出已生成的周报（$N7 份）" || fail "T7 list" "n=$N7"

# ---------------------------------------------------------------- T8 坏参数：--week-of 不是日期 ⇒ 退出 1
OUT8="$(HUB_ROOT="$ROOTD" HUB_CONFIG="$CFG" "${WEEK[@]}" run --week-of 不是日期 2>&1; echo "exit=$?")"
EX8="$(printf '%s' "$OUT8" | tail -1)"
[ "$EX8" = "exit=1" ] && pass "T8 坏 --week-of ⇒ 退出 1" || fail "T8 坏参数" "ex=$EX8"

# ---------------------------------------------------------------- T9 空库也能出周报（不崩）
ROOT9="$TMP/r9"
OUT9="$(HUB_ROOT="$ROOT9" HUB_CONFIG="$CFG" "${WEEK[@]}" --json run)"
D9="$(printf '%s' "$OUT9" | tail -1)"
R9="$(printf '%s' "$D9" | json_get "d['reason']")"
P9="$(printf '%s' "$D9" | json_get "d['path']")"
[ "$R9" = "ok" ] && [ -f "$P9" ] && pass "T9 空库也能生成周报（reason=ok）" \
  || fail "T9 空库" "reason=$R9 path=$P9"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
