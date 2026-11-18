#!/usr/bin/env bash
# info-hub M5b 验收：浏览器历史采集（Edge/Chrome History → 仓库页/视频页 → KB 对账 → 待办）。
# 全程离线：History 是临时造的假库，KB 是临时目录；不碰真库、不碰真浏览器的库。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
COLLECT=(python3 -m infohub.entry_collect)
RULES=(python3 -m infohub.entry_rules)

TMP="$(mktemp -d /tmp/infohub-m5b-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

KB="$TMP/kb"
mkdir -p "$KB"
cat > "$KB/工具清单.md" <<'MD'
# 已经整合进知识库的工具
- ownerB/present-repo（窗口采集 / 已写过笔记）
MD

# 假 History：2 条窗口内仓库页（1 条 KB 有、1 条没有）+ 站点路径噪声 + 窗口外仓库页
#           + 2 个视频（1 个重复访问取最近标题）+ 窗口外视频 + 1 条无关页面
HIST="$TMP/History"
python3 - "$HIST" <<'PY'
import sqlite3, sys
from datetime import datetime, timedelta, timezone
EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
def us(days=0.0):
    t = datetime.now(timezone.utc) - timedelta(days=days)
    return int((t - EPOCH).total_seconds() * 1_000_000)

rows = [
    ("https://github.com/ownerA/absent-repo", "ownerA/absent-repo: 一个没整合的工具", 3, us(2)),
    ("https://github.com/ownerB/present-repo", "ownerB/present-repo: 已经在 KB 里", 1, us(3)),
    ("https://github.com/settings/profile", "Settings", 2, us(3)),
    ("https://github.com/ownerC/old-repo", "ownerC/old-repo", 1, us(40)),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "某某教程_哔哩哔哩_bilibili", 5, us(2)),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "某某教程（后来改过标题）_哔哩哔哩_bilibili", 5, us(1)),
    ("https://www.bilibili.com/video/BV1yy411c7mE", "另一个视频_bilibili", 1, us(2)),
    ("https://www.bilibili.com/video/BV1zz411c7mF", "老视频_哔哩哔哩_bilibili", 1, us(40)),
    ("https://example.com/private-page", "与本次无关的页面", 9, us(1)),
    ("https://github.com/settings/tokens", "Personal access tokens", 1, us(1)),
]
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR,"
            " visit_count INTEGER DEFAULT 0, last_visit_time INTEGER DEFAULT 0)")
con.executemany("INSERT INTO urls (url, title, visit_count, last_visit_time) VALUES (?,?,?,?)", rows)
con.commit(); con.close()
PY
HIST_SHA_BEFORE="$(sha256sum "$HIST" | cut -d' ' -f1)"

mkcfg() {  # mkcfg <out> [key=value ...]  —— key 形如 collect.browser-history.max_items
  local out="$1"; shift
  python3 - "$ROOT/hub.config.yaml" "$out" "$HIST" "$KB" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
# M8 起仓库默认 kinds=[history]；本套测的是 M5b 的两类口径 ⇒ 显式钉住
raw["collect"]["browser-history"].update({"enabled": True, "db": sys.argv[3],
                                          "kinds": ["repo", "video"], "since_days": 14,
                                          "max_items": 200})
raw["collect"]["bilibili"]["enabled"] = False
raw["rules"]["bili-unintegrated"]["kb_root"] = sys.argv[4]
for kv in sys.argv[5:]:
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
sql() {  # sql <root> <python-expr with s=Store>
  local root="$1" expr="$2"
  HUB_ROOT="$root" python3 - "$expr" <<'PY' | tail -1
import sys
sys.path.insert(0, "src")
from infohub.config import load
from infohub.store import Store
c = load(); s = Store(c.db_path)
print(eval(sys.argv[1]))
PY
}

CFG="$TMP/c.yaml"; R="$TMP/r"; mkcfg "$CFG"

# ---------------------------------------------------------------- T1 采集口径：只落仓库页与视频页
O1="$(collectq "$CFG" "$R" --source browser-history)"
EM1="$(printf '%s' "$O1" | tail -1 | json_get "d['sources']['browser-history']['emitted']")"
IG1="$(printf '%s' "$O1" | tail -1 | json_get "d['ingested']")"
[ "$EM1" = "4" ] && [ "$IG1" = "4" ] && pass "T1 首次采集 4 条（2 仓库页 + 2 视频页）" \
  || fail "T1 首次采集" "emitted=$EM1 ingested=$IG1"

K1="$(sql "$R" "sorted((r['source'], r['kind'], r['privacy']) for r in s.events(50))")"
[ "$K1" = "[('browser-history', 'browser.repo', 'sensitive'), ('browser-history', 'browser.repo', 'sensitive'), ('browser-history', 'browser.video', 'sensitive'), ('browser-history', 'browser.video', 'sensitive')]" ] \
  && pass "T1b source/kind/privacy 口径正确（sensitive）" || fail "T1b 事件口径" "got=$K1"

OUT1="$(sql "$R" "s.events(50)")"
printf '%s' "$OUT1" | grep -q "example.com" && fail "T1c 无关页面不入库" "example.com 落库了" \
  || pass "T1c 无关页面（example.com）不入库"
printf '%s' "$OUT1" | grep -q "settings/profile" && fail "T1d 站点路径不当仓库" "settings/profile 落库了" \
  || pass "T1d 站点路径（github.com/settings/*）不当仓库"
printf '%s' "$OUT1" | grep -q "old-repo" && fail "T1e 窗口外记录不入库" "old-repo 落库了" \
  || pass "T1e 窗口外的旧记录不入库"

TITLE1="$(sql "$R" "[r['payload']['title'] for r in s.events(50) if r['kind']=='browser.video' and 'BV1xx411c7mD' in r['payload']['url']]")"
[ "$TITLE1" = "['某某教程（后来改过标题）']" ] && pass "T1f 同一视频只留最近一次访问，且去掉站点标题尾巴" \
  || fail "T1f 去重/标题" "got=$TITLE1"

# ---------------------------------------------------------------- T2 幂等：游标挡住重复
O2="$(collectq "$CFG" "$R" --source browser-history)"
EM2="$(printf '%s' "$O2" | tail -1 | json_get "d['sources']['browser-history']['emitted']")"
[ "$EM2" = "0" ] && pass "T2 再采一轮：游标挡住重复（emitted=0）" || fail "T2 游标" "emitted=$EM2"

# ---------------------------------------------------------------- T3 规则：只有 KB 里查不到的仓库出待办
O3="$(rulesq "$CFG" "$R" derive)"
CR3="$(printf '%s' "$O3" | json_get "len(d['created'])")"
K3="$(printf '%s' "$O3" | json_get "sorted(x['key'].rsplit(':',1)[0] for x in d['items'])")"
WHY3="$(sql "$R" "s.todos(limit=10)[0]['why']")"
[ "$CR3" = "1" ] && [ "$K3" = "['bili-unintegrated:ownera/absent-repo']" ] \
  && pass "T3 恰好 1 条待办：ownerA/absent-repo（present-repo 静默）" \
  || fail "T3 规则" "created=$CR3 keys=$K3"
case "$WHY3" in
  *"来自浏览器历史"*) pass "T3b 待办正文点明来源是浏览器历史（不是 bilibili）" ;;
  *) fail "T3b 来源措辞" "why=$WHY3" ;;
esac
EV3="$(sql "$R" "s.todos(limit=10)[0]['source_event_ids']")"
[ "$EV3" = "[1]" ] && pass "T3c 待办依据 = 那条真实事件（#1）" || fail "T3c 证据" "got=$EV3"

# ---------------------------------------------------------------- T4 每类上限（不是全局截断）
CFG4="$TMP/c4.yaml"; R4="$TMP/r4"; mkcfg "$CFG4" "collect.browser-history.max_items=1"
O4="$(collectq "$CFG4" "$R4" --source browser-history)"
EM4="$(printf '%s' "$O4" | tail -1 | json_get "d['sources']['browser-history']['emitted']")"
KD4="$(sql "$R4" "sorted(set(r['kind'] for r in s.events(50)))")"
[ "$EM4" = "2" ] && [ "$KD4" = "['browser.repo', 'browser.video']" ] \
  && pass "T4 max_items 每类各算：上限 1 ⇒ repo 1 + video 1" \
  || fail "T4 每类上限" "emitted=$EM4 kinds=$KD4"

# ---------------------------------------------------------------- T5 失败 fail-soft（退出码 0、记 error）
CFG5="$TMP/c5.yaml"; R5="$TMP/r5"; mkcfg "$CFG5" "collect.browser-history.db=$TMP/不存在的库"
O5="$(collectq "$CFG5" "$R5" --source browser-history; echo "exit=$?")"
EX5="$(printf '%s' "$O5" | tail -1)"
N5="$(sql "$R5" "len(s.events(50))")"
ER5="$(sql "$R5" "bool((s.state_get('run:browser-history') or {}).get('last_error'))")"
[ "$EX5" = "exit=0" ] && [ "$N5" = "0" ] \
  && pass "T5 历史库不存在 ⇒ 退出码 0、0 事件（不影响其它采集器）" \
  || fail "T5 fail-soft" "ex=$EX5 events=$N5"
[ "$ER5" = "True" ] && pass "T5b 失败如实记进 collector_state（run:browser-history）" || fail "T5b 记错误" "got=$ER5"

# ---------------------------------------------------------------- T6 默认关：enabled=false 一条都不采
CFG6="$TMP/c6.yaml"; R6="$TMP/r6"; mkcfg "$CFG6" "collect.browser-history.enabled=false"
O6="$(collectq "$CFG6" "$R6" --source browser-history)"
EM6="$(printf '%s' "$O6" | tail -1 | json_get "d['sources']['browser-history']['emitted']")"
[ "$EM6" = "0" ] && pass "T6 collect.browser-history.enabled=false ⇒ 不采" || fail "T6 默认关" "emitted=$EM6"

# ---------------------------------------------------------------- T7 只读：没写浏览器的库
HIST_SHA_AFTER="$(sha256sum "$HIST" | cut -d' ' -f1)"
[ "$HIST_SHA_BEFORE" = "$HIST_SHA_AFTER" ] \
  && pass "T7 取数只读：假 History 的 sha256 未变（没写它的库）" \
  || fail "T7 只读" "before=$HIST_SHA_BEFORE after=$HIST_SHA_AFTER"
[ -z "$(ls "$TMP" | grep -v '^History$' | grep -v '^kb$' | grep -v '\.yaml$' | grep '^History-')" ] \
  && pass "T7b 没在库旁留下副本/wal（副本只在临时目录，用完即删）" \
  || fail "T7b 无残留" "$(ls "$TMP")"

# ---------------------------------------------------------------- T8 单元级：时间换算与噪声过滤
O8="$(PYTHONPATH="$ROOT/src" python3 - <<'PY' | tail -1
from infohub import browserhist as BH, bili
row = {"url": "https://github.com/settings/profile", "title": "Settings",
       "last_visit_time": 13410000000000000, "visit_count": 1}
gl = {"url": "https://gitlab.com/o/r", "title": "O/R", "last_visit_time": 13410000000000000, "visit_count": 1}
item = {"id": "repo:ownera/absent-repo", "url": "https://github.com/ownera/absent-repo",
        "title": "ownera/absent-repo", "desc": "A collection of notebooks/recipes showcasing things"}
print(BH.repo_item(row), BH.repo_item(gl)["url"], BH.ts_iso(13410000000000000)[:4],
      [t["key"] for t in bili.extract_tools([item])])
PY
)"
case "$O8" in
  "None https://gitlab.com/o/r 2025 ['ownera/absent-repo']") pass "T8 单元：站点路径不出条目 / GitLab 认 host / 微秒→ISO 正确 / URL 是仓库页时标题里的斜杠词不产假工具" ;;
  *) fail "T8 单元" "got=$O8" ;;
esac

# ---------------------------------------------------------------- T8b 单元级：失效重置链接不是仓库
O8B="$(PYTHONPATH="$ROOT/src" python3 - <<'PY' | tail -1
from infohub import bili
blob = "A" * 200
bad = {"id": "repo:password_reset/" + blob,
       "url": "https://github.com/password_reset/" + blob,
       "title": "password_reset/" + blob, "desc": ""}
long_owner = {"id": "repo:" + "o" * 50 + "/r", "url": "https://github.com/" + "o" * 50 + "/r",
              "title": "", "desc": ""}
ok = {"id": "repo:ownera/absent-repo", "url": "https://github.com/ownera/absent-repo",
      "title": "ownera/absent-repo", "desc": ""}
print([t["key"] for t in bili.extract_tools([bad, long_owner, ok])])
PY
)"
case "$O8B" in
  "['ownera/absent-repo']") pass "T8b 单元：失效重置链接（200 位 base32）与超长 owner 都不产假仓库" ;;
  *) fail "T8b 单元（失效重置链接）" "got=$O8B" ;;
esac

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
