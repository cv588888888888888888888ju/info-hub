#!/usr/bin/env bash
# info-hub M8 验收：全量浏览历史（每条 = 时间 + 网址 + 站点 + 用途 + 标题前 120 字）+
# 「一条总结」（用途/站点分布进报表，不推送）。
# 全程离线：History 是临时造的假库、KB 是临时目录、报表写临时根；不碰真库、不碰真浏览器。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
COLLECT=(python3 -m infohub.entry_collect)
RULES=(python3 -m infohub.entry_rules)
REPORT=(python3 -m infohub.entry_report)

TMP="$(mktemp -d /tmp/infohub-m8-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

KB="$TMP/kb"; mkdir -p "$KB"
cat > "$KB/工具清单.md" <<'MD'
# 已经整合进知识库的工具
- ownerB/present-repo（已写过笔记）
MD

# 假 History：仓库页 / 视频页 / 小说页 / 搜索页 / 本机服务 / 空网址 / 窗口外旧记录
HIST="$TMP/History"
python3 - "$HIST" <<'PY'
import sqlite3, sys
from datetime import datetime, timedelta, timezone
EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
def us(days=0.0):
    t = datetime.now(timezone.utc) - timedelta(days=days)
    return int((t - EPOCH).total_seconds() * 1_000_000)
rows = [
    ("https://github.com/ownerA/absent-repo", "ownerA/absent-repo: 没整合的工具", 3, us(1)),
    ("https://github.com/ownerB/present-repo", "ownerB/present-repo: 已在 KB", 1, us(1)),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "某某教程_哔哩哔哩_bilibili", 5, us(1)),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "某某教程（改过标题）_哔哩哔哩_bilibili", 5, us(0.5)),
    ("https://novalpie.cc/book/123", "某本小说 - 最新章", 4, us(1)),
    ("https://www.google.com/search?q=%E6%B5%8B%E8%AF%95", "测试 - Google 搜索", 2, us(1)),
    ("http://127.0.0.1:8799/", "info-hub 控制台", 6, us(1)),
    ("", "没有网址的记录", 1, us(1)),
    ("https://github.com/ownerC/old-repo", "ownerC/old-repo", 1, us(400)),
    ("https://example.com/secret-page", "随便看看", 9, us(1)),
]
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR,"
            " visit_count INTEGER DEFAULT 0, last_visit_time INTEGER DEFAULT 0)")
con.executemany("INSERT INTO urls (url, title, visit_count, last_visit_time) VALUES (?,?,?,?)", rows)
con.commit(); con.close()
PY
HIST_SHA_BEFORE="$(sha256sum "$HIST" | cut -d' ' -f1)"

mkcfg() {  # mkcfg <out> [key=value ...]
  local out="$1"; shift
  python3 - "$ROOT/hub.config.yaml" "$out" "$HIST" "$KB" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
raw["collect"]["browser-history"].update({"enabled": True, "db": sys.argv[3],
                                          "kinds": ["history"], "since_days": 30})
raw["collect"]["bilibili"]["enabled"] = False
raw["collect"]["screenshot"]["enabled"] = False
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
collectq() { local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" "${COLLECT[@]}" --json --quiet "$@"; }
rulesq() { local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" "${RULES[@]}" --json "$@"; }
sql() { local root="$1" expr="$2"
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

# ---------------------------------------------------------------- T1 单元：站点/用途分类
O1="$(PYTHONPATH="$ROOT/src" python3 - <<'PY' | tail -1
from infohub import history as H
cases = [
    ("https://www.bilibili.com/video/BV1xx411c7mD", "视频/音频"),
    ("https://github.com/settings/profile", "代码仓库"),
    ("https://github.com/ownerA/absent-repo", "代码仓库"),
    ("https://www.google.com/search?q=x", "搜索"),
    ("https://www.google.com/", "搜索"),
    ("https://drive.google.com/drive/my-drive", "网盘/文件"),
    ("https://chat.deepseek.com/", "AI 服务"),
    ("https://novalpie.cc/book/1", "小说/阅读"),
    ("http://127.0.0.1:8799/", "本机服务"),
    ("https://www.bbc.co.uk/news", "其它"),
    ("https://hrbust.edu.cn/", "学习/课程"),
    ("https://example.com/x", "其它"),
]
print([H.purpose(u) == want for u, want in cases].count(True), H.site("https://a.b.example.co.uk/x"),
      H.site("http://127.0.0.1:8799/"), H.visit_id("https://x/") == H.visit_id("https://x/"))
PY
)"
[ "$O1" = "12 example.co.uk 127.0.0.1 True" ] \
  && pass "T1 单元：12/12 用途判定正确（含搜索/网盘/AI/本机）/ 多段后缀站点 / id 稳定" \
  || fail "T1 单元分类" "got=$O1"

# ---------------------------------------------------------------- T2 单元：每条只留「一条总结」的字段
O2="$(PYTHONPATH="$ROOT/src" python3 - <<'PY' | tail -1
from infohub import browserhist as BH, history as H
row = {"url": "https://www.bilibili.com/video/BV1xx411c7mD", "title": "某某教程_哔哩哔哩_bilibili",
       "last_visit_time": 13410000000000000, "visit_count": 5}
it = BH.history_item(row)
short = BH.history_item(row, title_limit=4)
empty = BH.history_item({"url": "  ", "title": "x", "last_visit_time": 13410000000000000})
long_title = "长" * 300
trunc = H.clean_title(long_title, limit=120)
print(it["kind"], it["title"], it["purpose"], it["site"], it["id"].startswith("visit:"),
      len(trunc), short["title"], empty, H.one_line(it["ts"], it).split(" · ")[1])
PY
)"
[ "$O2" = "history 某某教程 视频/音频 bilibili.com True 120 某某教程 None bilibili.com" ] \
  && pass "T2 单元：条目 = 时间+网址+站点+用途+标题（去尾巴、截 120）；空网址不出条目" \
  || fail "T2 单元条目" "got=$O2"

# ---------------------------------------------------------------- T3 全量采集：8 条窗口内记录都落库
O3="$(collectq "$CFG" "$R" --source browser-history)"
EM3="$(printf '%s' "$O3" | tail -1 | json_get "d['sources']['browser-history']['emitted']")"
[ "$EM3" = "7" ] && pass "T3 全量口径：窗口内 7 条都落库（空网址那条丢）" \
  || fail "T3 全量采集" "emitted=$EM3"

K3="$(sql "$R" "sorted(set((r['source'], r['kind'], r['privacy']) for r in s.events(50)))")"
[ "$K3" = "[('browser-history', 'browser.history', 'sensitive')]" ] \
  && pass "T3b 事件统一 kind=browser.history、privacy=sensitive" || fail "T3b 事件口径" "got=$K3"

P3="$(sql "$R" "sorted(set((r['payload']['site'], r['payload']['purpose']) for r in s.events(50) if r['payload'].get('purpose')))")"
if printf '%s' "$P3" | grep -q "('novalpie.cc', '小说/阅读')" \
   && printf '%s' "$P3" | grep -q "('google.com', '搜索')" \
   && printf '%s' "$P3" | grep -q "('127.0.0.1', '本机服务')" \
   && printf '%s' "$P3" | grep -q "('bilibili.com', '视频/音频')"; then
  pass "T3c payload 带站点与用途（分类结果随事件一起落库）"
else
  fail "T3c payload 分类" "got=$P3"
fi

T3D="$(sql "$R" "[r['payload']['title'] for r in s.events(50) if 'BV1xx411c7mD' in r['payload']['url']]")"
[ "$T3D" = "['某某教程（改过标题）']" ] \
  && pass "T3d 同一网址只留最近一次访问，标题已去站点尾巴" || fail "T3d 去重/标题" "got=$T3D"

# ---------------------------------------------------------------- T4 幂等：游标挡住重复
O4="$(collectq "$CFG" "$R" --source browser-history)"
EM4="$(printf '%s' "$O4" | tail -1 | json_get "d['sources']['browser-history']['emitted']")"
[ "$EM4" = "0" ] && pass "T4 再采一轮 emitted=0（游标幂等）" || fail "T4 幂等" "emitted=$EM4"

# ---------------------------------------------------------------- T5 一条总结：用途/站点分布
O5="$(sql "$R" "__import__('json').dumps(__import__('infohub.history', fromlist=['x']).summarize(s, None, days=30), ensure_ascii=False)")"
TOT5="$(printf '%s' "$O5" | json_get "d['total']")"
LN5="$(printf '%s' "$O5" | json_get "d['line']")"
[ "$TOT5" = "7" ] && case "$LN5" in
  "最近 30 天浏览 7 个页面（去重后）："*) pass "T5 一条总结：7 个页面 + 一句话（$LN5）" ;;
  *) fail "T5 一句话" "line=$LN5" ;;
esac || fail "T5 总结总数" "total=$TOT5"
TOP5="$(printf '%s' "$O5" | json_get "d['top_sites'][0]")"
case "$TOP5" in
  *"'site': 'github.com'"*"'count': 2"*) pass "T5b 主力站点计数正确（github.com 2）" ;;
  *) fail "T5b 站点分布" "got=$TOP5" ;;
esac

# ---------------------------------------------------------------- T6 窗口：窗口外的历史不进总结
O6="$(sql "$R" "__import__('json').dumps(__import__('infohub.history', fromlist=['x']).summarize(s, None, days=0.7), ensure_ascii=False)")"
TOT6="$(printf '%s' "$O6" | json_get "d['total']")"
[ "$TOT6" = "1" ] && pass "T6 窗口生效：0.7 天窗口只剩 1 条（更早的不进总结）" \
  || fail "T6 窗口" "total=$TOT6"

# ---------------------------------------------------------------- T7 规则：非仓库页不产假工具（M8 噪声门）
O7="$(rulesq "$CFG" "$R" derive)"
K7="$(printf '%s' "$O7" | json_get "sorted(x['key'].rsplit(':',1)[0] for x in d['items'])")"
[ "$K7" = "['bili-unintegrated:ownera/absent-repo']" ] \
  && pass "T7 全量历史里只有仓库页产卡（小说/视频标题里的斜杠词不产假工具）" \
  || fail "T7 规则噪声门" "keys=$K7"

# ---------------------------------------------------------------- T8 报表：历史区块（只进报表，不推送）
export HUB_ROOT="$R" HUB_CONFIG="$CFG"
"${REPORT[@]}" --quiet generate >/dev/null 2>&1
REP="$R/reports/hub-report.md"
if grep -q 'hub:auto:begin history' "$REP" && grep -q '浏览历史' "$REP"; then
  pass "T8 报表含「浏览历史」区块（含一句话总结 + 用途表）"
else
  fail "T8 报表区块" "$(grep -c 'history' "$REP" 2>/dev/null || echo no-report)"
fi
unset HUB_ROOT HUB_CONFIG

# ---------------------------------------------------------------- T9 失败 fail-soft
CFG9="$TMP/c9.yaml"; R9="$TMP/r9"; mkcfg "$CFG9" "collect.browser-history.db=$TMP/不存在的库"
O9="$(collectq "$CFG9" "$R9" --source browser-history; echo "exit=$?")"
EX9="$(printf '%s' "$O9" | tail -1)"
N9="$(sql "$R9" "len(s.events(50))")"
ER9="$(sql "$R9" "bool((s.state_get('run:browser-history') or {}).get('last_error'))")"
[ "$EX9" = "exit=0" ] && [ "$N9" = "0" ] && [ "$ER9" = "True" ] \
  && pass "T9 历史库不存在 ⇒ 退出码 0、0 事件、如实记 run:browser-history" \
  || fail "T9 fail-soft" "ex=$EX9 events=$N9 err=$ER9"

# ---------------------------------------------------------------- T10 默认关
CFG10="$TMP/c10.yaml"; R10="$TMP/r10"; mkcfg "$CFG10" "collect.browser-history.enabled=false"
O10="$(collectq "$CFG10" "$R10" --source browser-history)"
EM10="$(printf '%s' "$O10" | tail -1 | json_get "d['sources']['browser-history']['emitted']")"
[ "$EM10" = "0" ] && pass "T10 enabled=false ⇒ 不采" || fail "T10 默认关" "emitted=$EM10"

# ---------------------------------------------------------------- T11 只读：没写浏览器的库
HIST_SHA_AFTER="$(sha256sum "$HIST" | cut -d' ' -f1)"
[ "$HIST_SHA_BEFORE" = "$HIST_SHA_AFTER" ] \
  && pass "T11 取数只读：假 History 的 sha256 未变" || fail "T11 只读" "变了"

# ---------------------------------------------------------------- T12 CLI 两个面
C12="$(PYTHONPATH="$ROOT/src" python3 -m infohub.entry_history classify \
        "https://www.bilibili.com/video/BV1xx411c7mD" 2>&1 | tail -1)"
J12="$(HUB_ROOT="$R" HUB_CONFIG="$CFG" PYTHONPATH="$ROOT/src" python3 -m infohub.entry_history \
        --config "$CFG" summary --days 30 --json 2>/dev/null | json_get "d['total']")"
[ "$C12" = "bilibili.com · 视频/音频" ] && [ "$J12" = "7" ] \
  && pass "T12 CLI：classify 与 summary 都按契约出数" || fail "T12 CLI" "classify=$C12 total=$J12"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
