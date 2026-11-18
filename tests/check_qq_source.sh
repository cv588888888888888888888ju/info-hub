#!/usr/bin/env bash
# 验收：QQ 消息采集器（plugins/sources/qq；契约 INTERFACE.md §4.24 / §10；共享任务 task-5）。
# 断言面：
#   ➀ 默认关：清单 default_enabled=false ⇒ status=disabled，--source qq 也不加载（plugin.py 的装/跑双闸）；
#   ➁ 首轮只入 QQ 行，并按 key 去重（3 条）—— fixture 故意不建 UNIQUE(key)，重复 key 那行正文还不同；
#   ➂ 二轮靠 id 游标 ⇒ emitted=0 / ingested=0；
#   ➃ 游标回退 + 同 key 改正文 ⇒ 仍按 key 去重（emitted=0）；
#   ➄ 游标整行丢失后重放 ⇒ 指纹去重兜底（emitted=3 / ingested=0 / skipped_duplicate=3）；
#   ➅ 失败语义：表缺列 / 远端不可达 / 超时 ⇒ 退出码 0、sources.qq.ok=false、fail_streak=1；恢复后归 0。
# 隔离：临时 HUB_ROOT + 临时 fixture 库；collect.qq.ssh_host 置空 ⇒ 同一条远端命令在本机跑。
#      不碰 ~/.dsh/info-hub/hub.db、不要求真 ssh、可复跑。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-qq-XXXXXX)
export PYTHONPATH="$HUB/src"
PASS=0
FAIL=0

cleanup() { /usr/bin/rm -rf "$TMP"; }
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s -- %s\n' "$1" "${2:-}"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want=$2 got=$3"; fi; }
check_has() { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "「$3」里没有「$2」" ;; esac; }

# ---- fixture：与真表同列同类型；subtext 可缺（造「表缺列」） -----------------
make_fixture() {  # make_fixture <db> <yes|no: 是否有 subtext 列>
  python3 - "$1" "${2:-yes}" <<'PY'
import sqlite3, sys
db, with_subtext = sys.argv[1], sys.argv[2] == "yes"
con = sqlite3.connect(db)
cols = ["id INTEGER PRIMARY KEY AUTOINCREMENT", "ts INTEGER NOT NULL", "pkg TEXT NOT NULL",
        "app TEXT", "title TEXT", "text TEXT"]
if with_subtext:
    cols.append("subtext TEXT")
cols += ["key TEXT NOT NULL", "received_at INTEGER NOT NULL"]
con.execute("CREATE TABLE n (" + ", ".join(cols) + ")")
# 故意不建 UNIQUE(key)：模拟中继侧约束缺失 —— 本地仍必须按 key 去重
rows = [
    (3, 1791641102911, "com.tencent.mobileqq", "QQ", "示例群(7条新消息)",
     "某人: 第一条示例消息", "0|com.tencent.mobileqq|514|null|10323", 1791641199786),
    (4, 1791641000000, "com.tencent.mobileqq", "QQ", "测试群(2条新消息)", "甲: 第二条消息",
     "0|com.tencent.mobileqq|515|null|10324", 1791641099786),
    (5, 1791640900000, "com.tencent.mm", "微信", "示例私聊", "非 QQ 的行必须被过滤",
     "0|com.tencent.mm|700|null|20001", 1791640999786),
    (6, 1791640800000, "com.tencent.mobileqq", "QQ", "重放同一 key", "同一 key 换了正文，仍不许二次入库",
     "0|com.tencent.mobileqq|514|null|10323", 1791640899786),
    (7, 1791640700000, "com.tencent.mobileqq", "QQ", "第三条", "乙: 第三条消息",
     "0|com.tencent.mobileqq|516|null|10325", 1791640799786),
]
if with_subtext:
    con.executemany("INSERT INTO n (id,ts,pkg,app,title,text,subtext,key,received_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    [r[:6] + (None,) + r[6:] for r in rows])
else:
    con.executemany("INSERT INTO n (id,ts,pkg,app,title,text,key,received_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", rows)
con.commit()
PY
}

# ---- 配置：照真 hub.config.yaml 拷贝后只覆盖 qq 段 -----------------------------
make_cfg() {  # make_cfg <out> <db> [ssh_host] [remote_cmd] [timeout_s]
  python3 - "$HUB/hub.config.yaml" "$1" "$2" "${3-}" "${4-}" "${5-10}" <<'PY'
import sys, yaml
src, out, db, host, remote_cmd, timeout_s = sys.argv[1:7]
raw = yaml.safe_load(open(src, encoding="utf-8"))
raw.setdefault("plugins", {})["qq"] = {"enabled": True}   # 清单 default_enabled=false ⇒ 必须显式装
qq = raw.setdefault("collect", {}).setdefault("qq", {})
qq.update({"enabled": True, "ssh_host": host, "remote_db": db, "limit": 200,
           "timeout_s": float(timeout_s), "seen_keys_max": 500})
if remote_cmd:
    qq["remote_cmd"] = remote_cmd
yaml.safe_dump(raw, open(out, "w", encoding="utf-8"), allow_unicode=True)
PY
}

# ---- 跑一轮 + 取数 -----------------------------------------------------------
runqq() {  # runqq <root> <cfg> ⇒ $RC / $OUT（stdout 最后一行是 --json 汇总）
  OUT=$(HUB_ROOT="$1" HUB_CONFIG="$2" "$HUB/bin/hub-collect" --source qq --json --quiet 2>"$TMP/last.err")
  RC=$?
}

inj() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["ingested"])'; }
dups() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("skipped_duplicate", 0))'; }
emitted() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print((json.load(sys.stdin)["sources"].get("qq") or {}).get("emitted", "-"))'; }
srcok() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print((json.load(sys.stdin)["sources"].get("qq") or {}).get("ok", "-"))'; }
srcerr() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print((json.load(sys.stdin)["sources"].get("qq") or {}).get("error") or "")'; }
srcnames() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["sources"], ensure_ascii=False, sort_keys=True))'; }

dbof() { printf '%s/hub.db' "$1"; }
covar() {  # covar <root> <last_id|fail_streak|seen_keys>
  python3 -c 'import json,sqlite3,sys
db, field = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
row = con.execute("SELECT cursor FROM collector_state WHERE key=?", ("qq",)).fetchone()
cur = json.loads(row[0]) if row and row[0] else {}
if field == "seen_keys":
    print(len(cur.get("seen_keys") or []))
else:
    print(int(cur.get(field) or 0))' "$(dbof "$1")" "$2"
}
setlastid() {  # 模拟游标回退（保留 seen_keys）
  python3 -c 'import json,sqlite3,sys
db, lid = sys.argv[1], int(sys.argv[2])
con = sqlite3.connect(db)
row = con.execute("SELECT cursor FROM collector_state WHERE key=?", ("qq",)).fetchone()
cur = json.loads(row[0]) if row and row[0] else {}
cur["last_id"] = lid
con.execute("UPDATE collector_state SET cursor=? WHERE key=?", (json.dumps(cur, ensure_ascii=False), "qq"))
con.commit()' "$(dbof "$1")" "$2"
}
wipecursor() { python3 -c 'import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); con.execute("DELETE FROM collector_state WHERE key=?", ("qq",)); con.commit()' "$(dbof "$1")"; }
retext() { python3 -c 'import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); con.execute("UPDATE n SET text=? WHERE id=?", (sys.argv[2], int(sys.argv[3]))); con.commit()' "$1" "$2" "$3"; }

evcount() { python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM events WHERE source=?", ("qq",)).fetchone()[0])' "$(dbof "$1")"; }
evpkgs() {  # 入库事件里出现过的 pkg 集合（必须只有 QQ）
  python3 -c 'import json,sqlite3,sys
con = sqlite3.connect(sys.argv[1])
pkgs = sorted({json.loads(r[0]).get("pkg") for r in con.execute("SELECT payload FROM events WHERE source=?", ("qq",))})
print(",".join(str(p) for p in pkgs))' "$(dbof "$1")"
}
evwellformed() {  # 事件 ts=ISO8601 带时区；payload.ts 仍是毫秒整数
  python3 -c 'import json,re,sqlite3,sys
pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
rows = [json.loads(r[0]) for r in sqlite3.connect(sys.argv[1]).execute("SELECT payload FROM events WHERE source=?", ("qq",))]
ts = [r[0] for r in sqlite3.connect(sys.argv[1]).execute("SELECT ts FROM events WHERE source=?", ("qq",))]
good = bool(rows) and all(pat.match(t) for t in ts) and all(isinstance(p.get("ts"), int) and p["ts"] > 10**12 for p in rows)
print("ok" if good else "bad")' "$(dbof "$1")"
}

plugin_status() {  # plugin_status <cfg>
  HUB_CONFIG="$1" "$HUB/bin/hub-plugin" list --json 2>/dev/null | python3 -c 'import json,sys
rows = [p for p in json.load(sys.stdin)["plugins"] if p["name"] == "qq" and p["kind"] == "source"]
print(rows[0]["status"] if rows else "missing")'
}

# ---- T0 默认关：清单 false ⇒ 行在 status=disabled，--source 也不加载 ----------
if grep -q '^default_enabled: false$' "$HUB/plugins/sources/qq/plugin.yaml"; then
  ok "T0 plugin.yaml default_enabled=false"
else
  bad "T0 plugin.yaml default_enabled=false" "$(cat "$HUB/plugins/sources/qq/plugin.yaml")"
fi
cp "$HUB/hub.config.yaml" "$TMP/plain.yaml"
check_eq "T0 未装时清单 status=disabled（行在、不是行消失）" disabled "$(plugin_status "$TMP/plain.yaml")"
make_fixture "$TMP/fixture.db"
runqq "$TMP/r0" "$TMP/plain.yaml"
check_eq "T0 未装时 --source qq rc=0" 0 "$RC"
check_eq "T0 未装时没有任何来源被跑（sources={}）" "{}" "$(srcnames)"
check_eq "T0 未装时 ingested=0" 0 "$(inj)"

# ---- T1 首轮：只入 QQ、按 key 去重（3 条），游标 last_id=7 ----------------
make_cfg "$TMP/qq.yaml" "$TMP/fixture.db"
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T1 首轮 rc=0" 0 "$RC"
check_eq "T1 首轮 emitted=3（重复 key 只算一条）" 3 "$(emitted)"
check_eq "T1 首轮 ingested=3（dup key 不多入、非 QQ 不入）" 3 "$(inj)"
check_eq "T1 宿主记 sources.qq.ok=true" True "$(srcok)"
check_eq "T1 库里 qq 事件 3 条" 3 "$(evcount "$TMP/r1")"
check_eq "T1 入库的 payload.pkg 只有 QQ" com.tencent.mobileqq "$(evpkgs "$TMP/r1")"
check_eq "T1 事件 ts 是带时区 ISO8601、payload.ts 仍是 ms" ok "$(evwellformed "$TMP/r1")"
check_eq "T1 游标 last_id=7（按 id，不是按 ts）" 7 "$(covar "$TMP/r1" last_id)"
check_eq "T1 游标记了 3 个 key" 3 "$(covar "$TMP/r1" seen_keys)"
check_eq "T1 游标 fail_streak=0" 0 "$(covar "$TMP/r1" fail_streak)"

# ---- T2 二轮：id 游标挡住 ⇒ 一条都不发 ------------------------------------
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T2 二轮 rc=0" 0 "$RC"
check_eq "T2 二轮 emitted=0" 0 "$(emitted)"
check_eq "T2 二轮 ingested=0" 0 "$(inj)"

# ---- T3 游标回退 + 同 key 改正文 ⇒ 仍按 key 去重 ---------------------------
retext "$TMP/fixture.db" "甲: 第二条消息（上游改写）" 4
setlastid "$TMP/r1" 0
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T3 游标回退后 emitted=0（同 key 即使正文变了也不重发）" 0 "$(emitted)"
check_eq "T3 游标回退后 ingested=0" 0 "$(inj)"
check_eq "T3 游标重新推进到 last_id=7" 7 "$(covar "$TMP/r1" last_id)"
retext "$TMP/fixture.db" "甲: 第二条消息" 4

# ---- T4 游标整行丢失后重放 ⇒ 指纹去重兜底 --------------------------------
wipecursor "$TMP/r1"
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T4 丢游标后 emitted=3（重新发三条）" 3 "$(emitted)"
check_eq "T4 丢游标后 ingested=0（指纹去重不重复入库）" 0 "$(inj)"
check_eq "T4 丢游标后 skipped_duplicate=3" 3 "$(dups)"
check_eq "T4 库里仍只有 3 条" 3 "$(evcount "$TMP/r1")"

# ---- T5 表缺列 ⇒ CollectorError，rc=0，fail_streak=1 ---------------------
make_fixture "$TMP/nosubtext.db" no
make_cfg "$TMP/misscol.yaml" "$TMP/nosubtext.db"
runqq "$TMP/r2" "$TMP/misscol.yaml"
check_eq "T5 表缺列时 rc=0（不退出非 0）" 0 "$RC"
check_eq "T5 表缺列时 sources.qq.ok=false" False "$(srcok)"
check_eq "T5 表缺列时 fail_streak=1" 1 "$(covar "$TMP/r2" fail_streak)"
check_has "T5 错误信息点名读库失败" "读取 QQ 中继库失败" "$(srcerr)"
check_has "T5 错误信息点明缺列原因（traceback 末行）" "no such column" "$(srcerr)"
check_eq "T5 表缺列时一条也没入" 0 "$(evcount "$TMP/r2")"

# ---- T6 远端不可达（TEST-NET-1 黑洞）⇒ CollectorError，fail_streak=1 -----
make_cfg "$TMP/unreach.yaml" "$TMP/fixture.db" root@192.0.2.1 "" 3
runqq "$TMP/r3" "$TMP/unreach.yaml"
check_eq "T6 远端不可达时 rc=0" 0 "$RC"
check_eq "T6 远端不可达时 sources.qq.ok=false" False "$(srcok)"
check_eq "T6 远端不可达时 fail_streak=1" 1 "$(covar "$TMP/r3" fail_streak)"
[ -n "$(srcerr)" ] && ok "T6 错误信息非空：$(srcerr | head -c 80)" || bad "T6 错误信息非空" "空"

# ---- T7 超时 ⇒ CollectorError，fail_streak=1 -----------------------------
make_cfg "$TMP/slow.yaml" "$TMP/fixture.db" "" "bash -c 'sleep 30'" 0.3
runqq "$TMP/r4" "$TMP/slow.yaml"
check_eq "T7 超时时 rc=0" 0 "$RC"
check_eq "T7 超时时 sources.qq.ok=false" False "$(srcok)"
check_eq "T7 超时时 fail_streak=1" 1 "$(covar "$TMP/r4" fail_streak)"
check_has "T7 超时错误点名超时" "超时" "$(srcerr)"

# ---- T8 恢复：同一根换回好 fixture ⇒ fail_streak 归 0 并正常入库 ----------
make_cfg "$TMP/qq4.yaml" "$TMP/fixture.db"
runqq "$TMP/r4" "$TMP/qq4.yaml"
check_eq "T8 恢复后 source ok=true" True "$(srcok)"
check_eq "T8 恢复后 fail_streak=0" 0 "$(covar "$TMP/r4" fail_streak)"
check_eq "T8 恢复后 ingested=3" 3 "$(inj)"

printf '\nSUMMARY {"checks":%d,"failed":%d}\n' "$((PASS + FAIL))" "$FAIL"
[ "$FAIL" -eq 0 ]
