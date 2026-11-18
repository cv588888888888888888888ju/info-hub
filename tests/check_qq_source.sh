#!/usr/bin/env bash
# 验收：QQ 消息采集器（OneBot v11 spool 版；契约 INTERFACE.md §4.24 / §10；共享任务 task-7）。
# 断言面：
#   T0 默认关（清单 false ⇒ status=disabled、--source qq 不加载、sources={}）
#   T1 首轮三条（群/私聊）入库；非 message 事件被忽略；self 消息被跳过
#   T2 offset 前进 ⇒ 二轮 emitted=0
#   T3 半行（末行无 \n）不入库、offset 不动；补换行后入 1 条且不重复
#   T4 重复 message_id ⇒ 只入一条（先到先得）
#   T5 非文本段占位 [图片]/[表情]；title 取值（群名 / 私聊 card 优先 nickname）
#   T6 payload.ts 毫秒、事件 ts 带时区 ISO8601
#   T7 轮转（size < offset）⇒ offset 归 0、seen_keys 去重生效、新消息照收
#   T8 spool 不存在 ⇒ ok=false / fail_streak=1 / rc=0；建好后恢复 ⇒ fail_streak=0
#   T9 spool 掺坏 JSON ⇒ 同上失败语义（宿主 stderr 带真异常）
#   T10 ssh/读超时 ⇒ 失败语义
#   T11 include_self=true ⇒ self 消息入库
#   T12 seen_keys 只留最近 seen_keys_max 个
# 隔离：临时 HUB_ROOT + 临时 spool；collect.qq.ssh_host 置空 ⇒ 同一条远端命令在本机跑。
#      不碰 ~/.dsh/info-hub/hub.db、不要求真 ssh、可复跑。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d /tmp/infohub-qq-spool-XXXXXX)
export PYTHONPATH="$HUB/src"
PASS=0
FAIL=0

cleanup() { /usr/bin/rm -rf "$TMP"; }
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s -- %s\n' "$1" "${2:-}"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want=$2 got=$3"; fi; }
check_has() { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "「$3」里没有「$2」" ;; esac; }
check_no() { case "$3" in *"$2"*) bad "$1" "不该出现「$2」：$3" ;; *) ok "$1" ;; esac; }

# ---- 配置：照真 hub.config.yaml 拷贝后只覆盖 qq 段 -----------------------------
make_cfg() {  # make_cfg <out> <spool> [ssh_host] [remote_cmd] [timeout_s] [include_self] [seen_keys_max] [limit]
  python3 - "$HUB/hub.config.yaml" "$1" "$2" "${3-}" "${4-}" "${5-10}" "${6-false}" "${7-500}" "${8-200}" <<'PY'
import sys, yaml
src, out, spool, host, remote_cmd, timeout_s, include_self, seen_max, limit = sys.argv[1:10]
raw = yaml.safe_load(open(src, encoding="utf-8"))
raw.setdefault("plugins", {})["qq"] = {"enabled": True}   # 清单 default_enabled=false ⇒ 必须显式装
qq = raw.setdefault("collect", {}).setdefault("qq", {})
qq.update({"enabled": True, "ssh_host": host, "spool_path": spool, "limit": int(limit),
           "max_bytes": 4194304, "timeout_s": float(timeout_s),
           "include_self": include_self == "true", "seen_keys_max": int(seen_max)})
if remote_cmd:
    qq["remote_cmd"] = remote_cmd
yaml.safe_dump(raw, open(out, "w", encoding="utf-8"), allow_unicode=True)
PY
}

# ---- 跑一轮 + 汇总取数 -------------------------------------------------------
runqq() {  # runqq <root> <cfg> ⇒ $RC / $OUT（stdout 最后一行是 --json 汇总）
  OUT=$(HUB_ROOT="$1" HUB_CONFIG="$2" "$HUB/bin/hub-collect" --source qq --json 2>"$TMP/last.err")
  RC=$?
}

inj() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["ingested"])'; }
dups() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("skipped_duplicate", 0))'; }
emitted() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print((json.load(sys.stdin)["sources"].get("qq") or {}).get("emitted", "-"))'; }
srcok() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print((json.load(sys.stdin)["sources"].get("qq") or {}).get("ok", "-"))'; }
srcerr() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print((json.load(sys.stdin)["sources"].get("qq") or {}).get("error") or "")'; }
srcnames() { printf '%s' "$OUT" | tail -1 | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["sources"], ensure_ascii=False, sort_keys=True))'; }

# ---- 库与文件取数 -----------------------------------------------------------
dbof() { printf '%s/hub.db' "$1"; }
spoolsz() { stat -c %s "$1"; }

covar() {  # covar <root> <offset|fail_streak|seen_keys>
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

evcount() { python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM events WHERE source=?", ("qq",)).fetchone()[0])' "$(dbof "$1")"; }
evkeys() {  # 入库事件的 payload.key（升序，逗号连接）
  python3 -c 'import json,sqlite3,sys
con = sqlite3.connect(sys.argv[1])
ks = sorted(json.loads(r[0]).get("key", "") for r in con.execute("SELECT payload FROM events WHERE source=?", ("qq",)))
print(",".join(ks))' "$(dbof "$1")"
}
evkeyn() {  # evkeyn <root> <key> ⇒ 该 key 的入库条数
  python3 -c 'import json,sqlite3,sys
con = sqlite3.connect(sys.argv[1]); key = sys.argv[2]
print(sum(1 for r in con.execute("SELECT payload FROM events WHERE source=?", ("qq",)) if json.loads(r[0]).get("key") == key))' "$(dbof "$1")" "$2"
}
evfield() {  # evfield <root> <key> <ts|pkg|app|title|text>
  python3 -c 'import json,sqlite3,sys
con = sqlite3.connect(sys.argv[1]); key, field = sys.argv[2], sys.argv[3]
val = ""
for r in con.execute("SELECT payload FROM events WHERE source=?", ("qq",)):
    p = json.loads(r[0])
    if p.get("key") == key:
        val = p.get(field); break
print("" if val is None else val)' "$(dbof "$1")" "$2" "$3"
}
evts() {  # evts <root> <key> ⇒ 事件 ts（列 ts，不是 payload.ts）
  python3 -c 'import json,sqlite3,sys
con = sqlite3.connect(sys.argv[1]); key = sys.argv[2]; out = ""
for ts, payload in con.execute("SELECT ts, payload FROM events WHERE source=?", ("qq",)):
    if json.loads(payload).get("key") == key:
        out = ts; break
print(out)' "$(dbof "$1")" "$2"
}
evshape() {  # 事件 ts 全是带时区 ISO8601、payload.ts 是 >10^12 的整数
  python3 -c 'import json,re,sqlite3,sys
pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
rows = list(sqlite3.connect(sys.argv[1]).execute("SELECT ts, payload FROM events WHERE source=?", ("qq",)))
good = bool(rows) and all(pat.match(r[0]) for r in rows) and all(
    isinstance(json.loads(r[1]).get("ts"), int) and json.loads(r[1])["ts"] > 10 ** 12 for r in rows)
print("ok" if good else "bad")' "$(dbof "$1")"
}

plugin_status() {  # plugin_status <cfg>
  HUB_CONFIG="$1" "$HUB/bin/hub-plugin" list --json 2>/dev/null | python3 -c 'import json,sys
rows = [p for p in json.load(sys.stdin)["plugins"] if p["name"] == "qq" and p["kind"] == "source"]
print(rows[0]["status"] if rows else "missing")'
}

# ---- fixture：OneBot v11 事件 spool（6 行） ----------------------------------
# 1001 群、1002 私聊、1003 self、meta_event（非 message）、1004 群（带图片/表情段）、1001 重放
cat > "$TMP/spool.jsonl" <<'JSONL'
{"time":1791641102,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":1001,"group_id":555,"group_name":"测试群甲","user_id":123,"message":[{"type":"text","data":{"text":"甲: 群消息一"}}],"sender":{"user_id":123,"nickname":"甲","card":""}}
{"time":1791641120,"self_id":999,"post_type":"message","message_type":"private","sub_type":"friend","message_id":1002,"user_id":777,"message":[{"type":"text","data":{"text":"私聊一"}}],"sender":{"user_id":777,"nickname":"昵称乙","card":"昵称甲"}}
{"time":1791641130,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":1003,"group_id":555,"group_name":"测试群甲","user_id":999,"message":[{"type":"text","data":{"text":"自己发的不该入库"}}],"sender":{"user_id":999,"nickname":"我自己","card":""}}
{"time":1791641140,"self_id":999,"post_type":"meta_event","meta_event_type":"heartbeat","interval":30000,"status":{"online":true}}
{"time":1791641150,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":1004,"group_id":556,"group_name":"测试群乙","user_id":456,"message":[{"type":"text","data":{"text":"看图"}},{"type":"image","data":{"file":"a.jpg"}},{"type":"face","data":{"id":"1"}},{"type":"text","data":{"text":"完"}}],"sender":{"user_id":456,"nickname":"丁","card":""}}
{"time":1791641160,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":1001,"group_id":555,"group_name":"测试群甲","user_id":123,"message":[{"type":"text","data":{"text":"重复 message_id 的重放"}}],"sender":{"user_id":123,"nickname":"甲","card":""}}
JSONL

# ---- T0 默认关：清单 false ⇒ status=disabled，--source 也不加载 --------------
if grep -q '^default_enabled: false$' "$HUB/plugins/sources/qq/plugin.yaml"; then
  ok "T0 plugin.yaml default_enabled=false"
else
  bad "T0 plugin.yaml default_enabled=false" "$(cat "$HUB/plugins/sources/qq/plugin.yaml")"
fi
python3 - "$HUB/hub.config.yaml" "$TMP/plain.yaml" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
(raw.get("plugins") or {}).pop("qq", None)     # 模拟「没装」：清单 default_enabled=false 生效
(raw.get("collect") or {}).pop("qq", None)
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
check_eq "T0 未装时清单 status=disabled（行在、不是行消失）" disabled "$(plugin_status "$TMP/plain.yaml")"
runqq "$TMP/r0" "$TMP/plain.yaml"
check_eq "T0 未装时 --source qq rc=0" 0 "$RC"
check_eq "T0 未装时没有任何来源被跑（sources={}）" "{}" "$(srcnames)"
check_eq "T0 未装时 ingested=0" 0 "$(inj)"

# ---- T1 首轮：三条群/私聊入库；非 message 忽略；self 跳过 -------------------
make_cfg "$TMP/qq.yaml" "$TMP/spool.jsonl"
runqq "$TMP/r1" "$TMP/qq.yaml"
SIZE1=$(spoolsz "$TMP/spool.jsonl")
check_eq "T1 首轮 rc=0" 0 "$RC"
check_eq "T1 首轮 emitted=3（self / meta_event / 重放都不发）" 3 "$(emitted)"
check_eq "T1 首轮 ingested=3" 3 "$(inj)"
check_eq "T1 宿主记 sources.qq.ok=true" True "$(srcok)"
check_eq "T1 入库 key 精确为 qq:1001,qq:1002,qq:1004" "qq:1001,qq:1002,qq:1004" "$(evkeys "$TMP/r1")"
check_eq "T1 库里 qq 事件 3 条" 3 "$(evcount "$TMP/r1")"
check_no "T1 self 消息（qq:1003）没入库" "qq:1003" "$(evkeys "$TMP/r1")"
check_eq "T1 字节游标 offset=文件大小（吃满完整行）" "$SIZE1" "$(covar "$TMP/r1" offset)"
check_eq "T1 游标记了 3 个 key" 3 "$(covar "$TMP/r1" seen_keys)"
check_eq "T1 游标 fail_streak=0" 0 "$(covar "$TMP/r1" fail_streak)"

# ---- T2 二轮：offset 已到末尾 ⇒ 一条都不发 ---------------------------------
OFF1=$(covar "$TMP/r1" offset)
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T2 二轮 rc=0" 0 "$RC"
check_eq "T2 二轮 emitted=0" 0 "$(emitted)"
check_eq "T2 二轮 ingested=0" 0 "$(inj)"
check_eq "T2 二轮 offset 不动" "$OFF1" "$(covar "$TMP/r1" offset)"

# ---- T3 半行：末行没换行 ⇒ 不解析、offset 不前进 ---------------------------
printf '%s' '{"time":1791641200,"self_id":999,"post_type":"message","message_type":"group","message_id":1500,"group_id":557,"group_name":"半行群","user_id":321,"message":[{"type":"text","data":{"text":"半行消息"}}],"sender":{"user_id":321,"nickname":"戊","card":""}}' >> "$TMP/spool.jsonl"
SIZEHALF=$(spoolsz "$TMP/spool.jsonl")
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T3 半行时 emitted=0" 0 "$(emitted)"
check_eq "T3 半行时 decoded 空（库里仍 3 条）" 3 "$(evcount "$TMP/r1")"
check_eq "T3 半行时 offset 停在半行之前" "$OFF1" "$(covar "$TMP/r1" offset)"
check_eq "T3 半行确实比 offset 长（前提成立）" "yes" "$([ "$SIZEHALF" -gt "$OFF1" ] && echo yes || echo no)"
printf '\n' >> "$TMP/spool.jsonl"
SIZE3=$(spoolsz "$TMP/spool.jsonl")
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T3 补换行后 emitted=1" 1 "$(emitted)"
check_eq "T3 补换行后 ingested=1" 1 "$(inj)"
check_eq "T3 半行消息 qq:1500 入库且只有 1 条" 1 "$(evkeyn "$TMP/r1" qq:1500)"
check_eq "T3 补换行后 offset=文件大小" "$SIZE3" "$(covar "$TMP/r1" offset)"
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T3 再跑一轮 emitted=0（半行不会重复入）" 0 "$(emitted)"

# ---- T4 重复 message_id：只入一条且先到先得 ---------------------------------
check_eq "T4 重复 message_id 的 qq:1001 只有 1 条" 1 "$(evkeyn "$TMP/r1" qq:1001)"
check_eq "T4 保留首条正文（先到先得）" "甲: 群消息一" "$(evfield "$TMP/r1" qq:1001 text)"

# ---- T5 非文本段占位 + title 取值 ------------------------------------------
check_eq "T5 群消息 title=群名" "测试群甲" "$(evfield "$TMP/r1" qq:1001 title)"
check_eq "T5 私聊 title=发送者 card（优先于 nickname）" "昵称甲" "$(evfield "$TMP/r1" qq:1002 title)"
check_eq "T5 非文本段占位 [图片][表情] 拼接形状" "看图[图片][表情]完" "$(evfield "$TMP/r1" qq:1004 text)"
check_eq "T5 payload.pkg=qq.onebot" "qq.onebot" "$(evfield "$TMP/r1" qq:1001 pkg)"
check_eq "T5 payload.app=qq" "qq" "$(evfield "$TMP/r1" qq:1001 app)"

# ---- T6 ts 形状：payload.ts 毫秒、事件 ts 带时区 ISO8601 --------------------
check_eq "T6 全部事件：ts 带时区 ISO、payload.ts 是 ms" ok "$(evshape "$TMP/r1")"
check_eq "T6 事件 time(秒) → payload.ts=毫秒" 1791641102000 "$(evfield "$TMP/r1" qq:1001 ts)"
case "$(evts "$TMP/r1" qq:1001)" in
  2026-??-??T??:??:??+08:00) ok "T6 事件 ts 是本地时区 ISO 文本" ;;
  *) bad "T6 事件 ts 是本地时区 ISO 文本" "$(evts "$TMP/r1" qq:1001)" ;;
esac

# ---- T7 轮转：新 spool 更短 ⇒ offset 归 0，seen_keys 挡住老消息、新消息照收 ---
OLDOFF=$(covar "$TMP/r1" offset)
cat > "$TMP/spool.jsonl" <<'JSONL'
{"time":1791641300,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":1001,"group_id":555,"group_name":"测试群甲","user_id":123,"message":[{"type":"text","data":{"text":"轮转后重放的老消息"}}],"sender":{"user_id":123,"nickname":"甲","card":""}}
{"time":1791641310,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":2001,"group_id":558,"group_name":"轮转群","user_id":654,"message":[{"type":"text","data":{"text":"轮转后的新消息"}}],"sender":{"user_id":654,"nickname":"己","card":""}}
JSONL
SIZE7=$(spoolsz "$TMP/spool.jsonl")
check_eq "T7 前提：新 spool 比旧 offset 短（真轮转）" "yes" "$([ "$SIZE7" -lt "$OLDOFF" ] && echo yes || echo no)"
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T7 轮转后 emitted=1（只发新消息）" 1 "$(emitted)"
check_eq "T7 轮转后 ingested=1" 1 "$(inj)"
check_eq "T7 轮转后 last_id 语义不变：qq:1001 仍只有 1 条" 1 "$(evkeyn "$TMP/r1" qq:1001)"
check_eq "T7 轮转后的新消息 qq:2001 入库" 1 "$(evkeyn "$TMP/r1" qq:2001)"
check_eq "T7 轮转后 offset=新文件大小" "$SIZE7" "$(covar "$TMP/r1" offset)"
check_eq "T7 轮转后 seen_keys 含 5 个" 5 "$(covar "$TMP/r1" seen_keys)"
runqq "$TMP/r1" "$TMP/qq.yaml"
check_eq "T7 轮转后再跑 emitted=0" 0 "$(emitted)"

# ---- T8 spool 不存在 ⇒ CollectorError；建好后恢复 --------------------------
make_cfg "$TMP/missing.yaml" "$TMP/missing/events.jsonl"
runqq "$TMP/r2" "$TMP/missing.yaml"
check_eq "T8 spool 不存在时 rc=0（不退出非 0）" 0 "$RC"
check_eq "T8 spool 不存在时 sources.qq.ok=false" False "$(srcok)"
check_eq "T8 spool 不存在时 fail_streak=1" 1 "$(covar "$TMP/r2" fail_streak)"
check_has "T8 错误点名 spool 不可读" "spool 不可读" "$(srcerr)"
check_eq "T8 spool 不存在时一条也没入" 0 "$(evcount "$TMP/r2")"
mkdir -p "$TMP/missing"
cat > "$TMP/missing/events.jsonl" <<'JSONL'
{"time":1791642000,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":3001,"group_id":601,"group_name":"恢复群","user_id":111,"message":[{"type":"text","data":{"text":"恢复后的第一条"}}],"sender":{"user_id":111,"nickname":"庚","card":""}}
JSONL
SIZE8=$(spoolsz "$TMP/missing/events.jsonl")
runqq "$TMP/r2" "$TMP/missing.yaml"
check_eq "T8 恢复后 source ok=true" True "$(srcok)"
check_eq "T8 恢复后 fail_streak=0" 0 "$(covar "$TMP/r2" fail_streak)"
check_eq "T8 恢复后 ingested=1" 1 "$(inj)"
check_eq "T8 恢复后 offset=文件大小" "$SIZE8" "$(covar "$TMP/r2" offset)"

# ---- T9 spool 里掺坏 JSON ⇒ CollectorError（宿主 stderr 带真异常） ----------
cat > "$TMP/bad.jsonl" <<'JSONL'
{"time":1791642100,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":4001,"group_id":602,"group_name":"坏行群","user_id":222,"message":[{"type":"text","data":{"text":"坏行前的好消息"}}],"sender":{"user_id":222,"nickname":"辛","card":""}}
这不是 JSON 的一行
JSONL
make_cfg "$TMP/bad.yaml" "$TMP/bad.jsonl"
runqq "$TMP/r3" "$TMP/bad.yaml"
check_eq "T9 坏 JSON 时 rc=0" 0 "$RC"
check_eq "T9 坏 JSON 时 sources.qq.ok=false" False "$(srcok)"
check_eq "T9 坏 JSON 时 fail_streak=1" 1 "$(covar "$TMP/r3" fail_streak)"
check_has "T9 错误点名「spool 行不是 JSON」" "spool 行不是 JSON" "$(srcerr)"
check_eq "T9 坏 JSON 时整轮不入库（0 条）" 0 "$(evcount "$TMP/r3")"
check_eq "T9 坏 JSON 时 offset 不前进" 0 "$(covar "$TMP/r3" offset)"
if grep -q "FAILED" "$TMP/last.err" && grep -q "JSON" "$TMP/last.err"; then
  ok "T9 宿主 stderr 带真异常文本（[qq] FAILED … JSON）"
else
  bad "T9 宿主 stderr 带真异常文本" "$(tail -2 "$TMP/last.err")"
fi

# ---- T10 读超时 ⇒ CollectorError -------------------------------------------
make_cfg "$TMP/slow.yaml" "$TMP/spool.jsonl" "" "bash -c 'sleep 30'" 0.3
runqq "$TMP/r4" "$TMP/slow.yaml"
check_eq "T10 超时时 rc=0" 0 "$RC"
check_eq "T10 超时时 sources.qq.ok=false" False "$(srcok)"
check_eq "T10 超时时 fail_streak=1" 1 "$(covar "$TMP/r4" fail_streak)"
check_has "T10 超时错误点名超时" "超时" "$(srcerr)"

# ---- T11 include_self=true ⇒ self 消息入库 ---------------------------------
cat > "$TMP/self.jsonl" <<'JSONL'
{"time":1791642200,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":1003,"group_id":555,"group_name":"测试群甲","user_id":999,"message":[{"type":"text","data":{"text":"我自己发的"}}],"sender":{"user_id":999,"nickname":"我自己","card":""}}
{"time":1791642210,"self_id":999,"post_type":"message","message_type":"private","sub_type":"friend","message_id":1005,"user_id":888,"message":[{"type":"text","data":{"text":"别人发的"}}],"sender":{"user_id":888,"nickname":"别人","card":"别人卡"}}
JSONL
make_cfg "$TMP/self.yaml" "$TMP/self.jsonl" "" "" 10 true
runqq "$TMP/r5" "$TMP/self.yaml"
check_eq "T11 include_self=true 时 emitted=2" 2 "$(emitted)"
check_eq "T11 include_self=true 时 self 消息（qq:1003）入库" 1 "$(evkeyn "$TMP/r5" qq:1003)"
check_eq "T11 include_self=true 时私聊也入库" 1 "$(evkeyn "$TMP/r5" qq:1005)"

# ---- T12 seen_keys 只留最近 seen_keys_max 个 -------------------------------
cat > "$TMP/cap.jsonl" <<'JSONL'
{"time":1791642300,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":5001,"group_id":701,"group_name":"容量群","user_id":1,"message":[{"type":"text","data":{"text":"一"}}],"sender":{"user_id":1,"nickname":"一","card":""}}
{"time":1791642310,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":5002,"group_id":701,"group_name":"容量群","user_id":2,"message":[{"type":"text","data":{"text":"二"}}],"sender":{"user_id":2,"nickname":"二","card":""}}
{"time":1791642320,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":5003,"group_id":701,"group_name":"容量群","user_id":3,"message":[{"type":"text","data":{"text":"三"}}],"sender":{"user_id":3,"nickname":"三","card":""}}
JSONL
make_cfg "$TMP/cap.yaml" "$TMP/cap.jsonl" "" "" 10 false 2
runqq "$TMP/r6" "$TMP/cap.yaml"
check_eq "T12 seen_keys_max=2 时三条都入库" 3 "$(inj)"
check_eq "T12 游标只留最近 2 个 key" 2 "$(covar "$TMP/r6" seen_keys)"

# ---- T13 limit 小于积压量 ⇒ 不许跳行（游标停在最后消费的行尾） --------------
cat > "$TMP/limit.jsonl" <<'JSONL'
{"time":1791642400,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":6001,"group_id":801,"group_name":"限流群","user_id":1,"message":[{"type":"text","data":{"text":"一"}}],"sender":{"user_id":1,"nickname":"一","card":""}}
{"time":1791642410,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":6002,"group_id":801,"group_name":"限流群","user_id":2,"message":[{"type":"text","data":{"text":"二"}}],"sender":{"user_id":2,"nickname":"二","card":""}}
{"time":1791642420,"self_id":999,"post_type":"message","message_type":"group","sub_type":"normal","message_id":6003,"group_id":801,"group_name":"限流群","user_id":3,"message":[{"type":"text","data":{"text":"三"}}],"sender":{"user_id":3,"nickname":"三","card":""}}
JSONL
make_cfg "$TMP/limit.yaml" "$TMP/limit.jsonl" "" "" 10 false 500 1
runqq "$TMP/r7" "$TMP/limit.yaml"
check_eq "T13 limit=1 首轮只入 1 条" 1 "$(emitted)"
runqq "$TMP/r7" "$TMP/limit.yaml"
check_eq "T13 二轮再入 1 条（不跳行）" 1 "$(emitted)"
runqq "$TMP/r7" "$TMP/limit.yaml"
check_eq "T13 三轮把剩下的也入完" 1 "$(emitted)"
check_eq "T13 三条都在库里（qq:6001/6002/6003）" 3 "$(evcount "$TMP/r7")"
runqq "$TMP/r7" "$TMP/limit.yaml"
check_eq "T13 四轮没有可入的了" 0 "$(emitted)"

printf '\nSUMMARY {"checks":%d,"failed":%d}\n' "$((PASS + FAIL))" "$FAIL"
[ "$FAIL" -eq 0 ]
