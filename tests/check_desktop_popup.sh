#!/usr/bin/env bash
# info-hub S7 验收：电脑弹窗核心插件（任务书 A9）。判据：dispatch 成功 ⇒ deliveries 多一行
# channel=computer 且 remote_ref=通知 id；act <dbus_id> acted ⇒ 该行 status=acked；
# 停用 / 删目录后 hub-plugin list 无可用该通道、dispatch 不报致命错误、其余插件照跑。
#
# 只在隔离环境跑：临时根 + 临时配置 + tests/stubs/gdbus_stub.sh 替身（不弹真通知、不碰真库、不重启服务）。
# 用法: bash tests/check_desktop_popup.sh   # 退出 0 = 全过
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BIN="$ROOT/bin"
STUBS="$ROOT/tests/stubs"
export PYTHONPATH="$ROOT/src"

TMP="$(mktemp -d /tmp/infohub-s7-XXXXXX)"
cleanup() { /usr/bin/rm -rf "$TMP"; }
trap cleanup EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "$2"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

mkcfg() {  # mkcfg <out> <root> [dotted.k=v ...]
  local out="$1" root="$2"; shift 2
  python3 - "$ROOT/hub.config.yaml" "$out" "$root" "$@" <<'PY'
import sys, yaml
src, dst, root = sys.argv[1], sys.argv[2], sys.argv[3]
raw = yaml.safe_load(open(src, encoding="utf-8"))
raw["root"] = root
def put(dotted, value):
    node = raw
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
for kv in sys.argv[4:]:
    key, value = kv.split("=", 1)
    put(key, yaml.safe_load(value))
yaml.safe_dump(raw, open(dst, "w", encoding="utf-8"), allow_unicode=True)
PY
}

seed() {  # seed <root> <cfg> <todos> <rule>
  HUB_ROOT="$1" HUB_CONFIG="$2" python3 "$ROOT/tests/m2_seed.py" "$3" 0 "$4" >/dev/null
}

sqlq() {  # sqlq <root> <cfg> <python-expr>   —— 只读库，不加载插件
  HUB_ROOT="$1" HUB_CONFIG="$2" python3 - "$3" <<'PY'
import sys
from infohub.config import load
from infohub.store import Store
cfg = load(); st = Store(cfg.db_path)
print(eval(sys.argv[1]))
PY
}

# 假 gdbus：转发给替身；额外在 Notify 那一刻把台账现状写进 order log（判「先写台账、再显示」）。
mkdir -p "$TMP/fakebin"
cat > "$TMP/fakebin/gdbus" <<'WRAP'
#!/usr/bin/env bash
case " $* " in
  *".Notify "*)
    if [ -n "$S7_ORDER_LOG" ]; then
      python3 - "$S7_ORDER_LOG" "$HUB_ROOT" <<'PY'
import os, sqlite3, sys
log, hub_root = sys.argv[1], sys.argv[2]
con = sqlite3.connect(os.path.join(hub_root, "hub.db"))
cur = con.execute("SELECT id, channel, status, options FROM deliveries ORDER BY created_at DESC")
rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
with open(log, "a", encoding="utf-8") as fh:
    fh.write("notify-sees rows=%d first=%s\n" % (len(rows), rows[0] if rows else None))
PY
    fi
    ;;
esac
exec __GDBUS_STUB__ "$@"
WRAP
sed -i "s|__GDBUS_STUB__|$STUBS/gdbus_stub.sh|" "$TMP/fakebin/gdbus"
chmod +x "$TMP/fakebin/gdbus"

# 基线：所有「门」都放行，只留被测的那条路径。
OPEN="notify.enabled=true notify.backend=gdbus notify.dnd_source=off notify.idle_source=off"
OPEN="$OPEN notify.require_ui_event=false notify.min_interval_s=0 notify.max_per_hour=0"
OPEN="$OPEN notify.max_per_day_per_kind=0 notify.max_per_dispatch=1"

# ================================================================ T1 notify.enabled=false ⇒ 通道不注册
R0="$TMP/r0"; C0="$TMP/c0.yaml"
mkcfg "$C0" "$R0"
seed "$R0" "$C0" 1 fs-repeat
OUT="$(HUB_ROOT="$R0" HUB_CONFIG="$C0" PATH="$TMP/fakebin:$PATH" "$BIN/hub-notify" --json dispatch)"
RC=$?
N0="$(sqlq "$R0" "$C0" "len(st.deliveries(limit=0))")"
[ "$RC" = "0" ] && [ "$(printf '%s' "$OUT" | json_get "d['reason']")" = "disabled" ] && [ "$N0" = "0" ] \
  && pass "T1 notify.enabled=false ⇒ reason=disabled、不建台账（通道未注册）" \
  || fail "T1 默认关" "rc=$RC n=$N0 out=$OUT"

# ================================================================ T2 dispatch 成功 ⇒ 台账一行 + remote_ref=通知 id
R1="$TMP/r1"; C1="$TMP/c1.yaml"
mkcfg "$C1" "$R1" $OPEN
seed "$R1" "$C1" 1 fs-repeat
ORDER="$TMP/order1.log"; CALLS="$TMP/gdbus1.log"
OUT="$(HUB_ROOT="$R1" HUB_CONFIG="$C1" PATH="$TMP/fakebin:$PATH" \
       GDBUS_STUB_ID=91 GDBUS_STUB_CALL_LOG="$CALLS" S7_ORDER_LOG="$ORDER" \
       "$BIN/hub-notify" --json dispatch)"
RC=$?
SENT="$(printf '%s' "$OUT" | json_get "d['sent']")"
DBID="$(printf '%s' "$OUT" | json_get "d['dbus_id']")"
[ "$RC" = "0" ] && [ "$SENT" = "1" ] && [ "$DBID" = "91" ] \
  && pass "T2 dispatch 经替身发送成功（sent=1，dbus_id=91）" \
  || fail "T2 发送" "rc=$RC out=$OUT"

# 「先写台账、再让通知显示」：Notify 被调用的那一刻，台账里已有 channel=computer 的 pending 行
OL="$(cat "$ORDER" 2>/dev/null)"
[ "$(printf '%s' "$OL" | grep -c 'notify-sees')" = "1" ] \
  && printf '%s' "$OL" | grep -q "rows=1" \
  && printf '%s' "$OL" | grep -q "'channel': 'computer'" \
  && printf '%s' "$OL" | grep -q "'status': 'pending'" \
  && pass "T2b Notify 时台账已存在（先写台账、后显示；该行当时 status=pending）" \
  || fail "T2b 写台账顺序" "order=$OL"

ROW="$(sqlq "$R1" "$C1" "[(r['channel'], str(r['remote_ref']), r['status'], bool(r['title']), bool(r['body']), r['mode']) for r in st.deliveries(limit=0)]")"
[ "$ROW" = "[('computer', '91', 'delivered', True, True, 'single')]" ] \
  && pass "T2c 台账一行：channel=computer、remote_ref=91、status=delivered、title/body 非空" \
  || fail "T2c 台账形状" "row=$ROW"

KEYS="$(sqlq "$R1" "$C1" "sorted(o['key'] for o in st.deliveries(limit=0)[0]['options'])")"
[ "$KEYS" = "['ignore-kind', 'open', 'snooze']" ] \
  && pass "T2d options 的 key 全是 deliver.ACTIONS 的稳定键（open/snooze/ignore-kind）" \
  || fail "T2d options 键" "keys=$KEYS"

grep -q "打开" "$CALLS" && grep -q "稍后" "$CALLS" && grep -q "忽略此类" "$CALLS" && grep -q "8000" "$CALLS" \
  && pass "T2e gdbus Notify 带上了按钮与 timeout=8000" \
  || fail "T2e gdbus 参数" "$(tail -1 "$CALLS" 2>/dev/null)"

# ================================================================ T3 act <dbus_id> acted ⇒ 该行 acked + 待办 done
OUT="$(HUB_ROOT="$R1" HUB_CONFIG="$C1" PATH="$TMP/fakebin:$PATH" "$BIN/hub-notify" --json act 91 acted)"
RC=$?
ROW="$(sqlq "$R1" "$C1" "[(r['status'], r['action'], str(r['remote_ref'])) for r in st.deliveries(limit=0)]")"
TODO="$(sqlq "$R1" "$C1" "[[t['status'] for t in st.todos(limit=50) if t['id'] == [r['todo_id'] for r in st.deliveries(limit=0) if str(r['remote_ref']) == '91'][0]][0]]")"
[ "$RC" = "0" ] && [ "$ROW" = "[('acked', 'acted', '91')]" ] && [ "$TODO" = "['done']" ] \
  && pass "T3 act 91 acted ⇒ 台账 status=acked/action=acted，待办 done" \
  || fail "T3 act" "rc=$RC row=$ROW todo=$TODO out=$OUT"

# ================================================================ T4 ignore-kind ⇒ 台账 suppressed + 类别抑制
seed "$R1" "$C1" 1 clipboard-domain-repeat
OUT="$(HUB_ROOT="$R1" HUB_CONFIG="$C1" PATH="$TMP/fakebin:$PATH" GDBUS_STUB_ID=92 \
       "$BIN/hub-notify" --json dispatch)"
OUT="$(HUB_ROOT="$R1" HUB_CONFIG="$C1" PATH="$TMP/fakebin:$PATH" "$BIN/hub-notify" --json act 92 ignore-kind)"
RC=$?
ROW="$(sqlq "$R1" "$C1" "[(r['status'], r['action']) for r in st.deliveries(limit=0) if str(r['remote_ref']) == '92']")"
SUP="$(sqlq "$R1" "$C1" "bool(st.kind_suppressed('clipboard-domain-repeat'))")"
[ "$RC" = "0" ] && [ "$ROW" = "[('suppressed', 'ignore-kind')]" ] && [ "$SUP" = "True" ] \
  && pass "T4 act 92 ignore-kind ⇒ 台账 suppressed，类别被压住" \
  || fail "T4 ignore-kind" "rc=$RC row=$ROW sup=$SUP out=$OUT"

# ================================================================ T5 reply ⇒ 台账 replied + 文本原样 + 待办不动
seed "$R1" "$C1" 1 study-repeat
OUT="$(HUB_ROOT="$R1" HUB_CONFIG="$C1" PATH="$TMP/fakebin:$PATH" GDBUS_STUB_ID=93 \
       "$BIN/hub-notify" --json dispatch)"
OUT="$(HUB_ROOT="$R1" HUB_CONFIG="$C1" python3 - <<'PY'
from infohub.config import load
from infohub.store import Store
from infohub import notify
cfg = load(); st = Store(cfg.db_path)
info = notify.act(cfg, st, 93, "reply", text="收到，明天做")
print(info["status"], (info.get("delivery") or {}).get("status"))
PY
)"
ROW="$(sqlq "$R1" "$C1" "[(r['status'], r['reply']) for r in st.deliveries(limit=0) if str(r['remote_ref']) == '93']")"
TODO="$(sqlq "$R1" "$C1" "[[t['status'] for t in st.todos(limit=50) if t['id'] == [r['todo_id'] for r in st.deliveries(limit=0) if str(r['remote_ref']) == '93'][0]][0]]")"
[ "$OUT" = "None replied" ] && [ "$ROW" = "[('replied', '收到，明天做')]" ] && [ "$TODO" = "['open']" ] \
  && pass "T5 reply ⇒ 台账 status=replied、文本逐字留档；待办仍 open（reply 不改待办）" \
  || fail "T5 reply" "out=$OUT row=$ROW todo=$TODO"

# ================================================================ T6 停用插件（配置）⇒ 无可用通道、其余照跑
R2="$TMP/r2"; C2="$TMP/c2.yaml"
mkcfg "$C2" "$R2" $OPEN plugins.desktop-popup.enabled=false
seed "$R2" "$C2" 1 fs-repeat
LIST="$(HUB_ROOT="$R2" HUB_CONFIG="$C2" "$BIN/hub-plugin" list --json)"
CHAN="$(printf '%s' "$LIST" | json_get "[p['status'] for p in d['plugins'] if p['name']=='desktop-popup']")"
OTHER="$(printf '%s' "$LIST" | json_get "len([p for p in d['plugins'] if p['name']!='desktop-popup' and p['status']=='ok'])")"
[ "$CHAN" = "['disabled']" ] && [ "$OTHER" -gt 0 ] \
  && pass "T6 hub-plugin list：desktop-popup 已停用（status=disabled）；其余插件照旧" \
  || fail "T6 清单" "chan=$CHAN other=$OTHER"

OUT="$(HUB_ROOT="$R2" HUB_CONFIG="$C2" PATH="$TMP/fakebin:$PATH" "$BIN/hub-notify" --json dispatch)"
RC=$?
N2="$(sqlq "$R2" "$C2" "len(st.deliveries(limit=0))")"
[ "$RC" = "0" ] && [ "$(printf '%s' "$OUT" | json_get "d['reason']")" = "no-channel" ] && [ "$N2" = "0" ] \
  && pass "T6b 停用后 dispatch 退出 0、reason=no-channel、不建台账（不报致命错误）" \
  || fail "T6b 停用后 dispatch" "rc=$RC n=$N2 out=$OUT"

HUB_ROOT="$R2" HUB_CONFIG="$C2" "$BIN/hub-collect" --once --source fs >/dev/null 2>&1
RC=$?
[ "$RC" = "0" ] && pass "T6c 停用后其它来源照跑（hub-collect --source fs 退出 0）" \
  || fail "T6c 其它来源" "rc=$RC"

# ================================================================ T7 删目录 ⇒ 清单里整行消失、dispatch 跳过
CP="$TMP/copy/info-hub"
mkdir -p "$TMP/copy"
cp -a "$ROOT" "$CP"
/usr/bin/rm -rf "$CP/plugins/channels/desktop-popup"
R3="$TMP/r3"; C3="$TMP/c3.yaml"
mkcfg "$C3" "$R3" $OPEN
seed "$R3" "$C3" 1 fs-repeat
LIST="$(HUB_ROOT="$R3" HUB_CONFIG="$C3" PYTHONPATH="$CP/src" python3 -m infohub.entry_plugin list --json)"
HAS="$(printf '%s' "$LIST" | json_get "any(p['name']=='desktop-popup' for p in d['plugins'])")"
FSOK="$(printf '%s' "$LIST" | json_get "[p['status'] for p in d['plugins'] if p['name']=='fs']")"
[ "$HAS" = "False" ] && [ "$FSOK" = "['ok']" ] \
  && pass "T7 删插件目录后 hub-plugin list 里没有 desktop-popup，其它插件照旧" \
  || fail "T7 删目录清单" "has=$HAS fs=$FSOK"

OUT="$(HUB_ROOT="$R3" HUB_CONFIG="$C3" PYTHONPATH="$CP/src" PATH="$TMP/fakebin:$PATH" \
       python3 -m infohub.entry_notify --json dispatch)"
RC=$?
[ "$RC" = "0" ] && [ "$(printf '%s' "$OUT" | json_get "d['reason']")" = "no-channel" ] \
  && pass "T7b 删目录后 dispatch 退出 0、reason=no-channel" \
  || fail "T7b 删目录 dispatch" "rc=$RC out=$OUT"

# ================================================================ T8 无待办 / 待办已收尾 ⇒ 不弹
R4="$TMP/r4"; C4="$TMP/c4.yaml"
mkcfg "$C4" "$R4" $OPEN
seed "$R4" "$C4" 1 fs-repeat
T8="$(HUB_ROOT="$R4" HUB_CONFIG="$C4" PATH="$TMP/fakebin:$PATH" GDBUS_STUB_ID=96 python3 - <<'PY'
import json, sys
from infohub.config import load
from infohub.store import Store
from infohub import plugin as P
cfg = load(); st = Store(cfg.db_path)
row = [r for r in P.list_plugins(cfg) if r["name"] == "desktop-popup"][0]
send = P.load_entry(row)
no_todo = send({"config": cfg, "store": st, "title": "合成待办", "body": "没有待办"})
tid = st.conn.execute("SELECT id FROM todos LIMIT 1").fetchone()[0]
st.conn.execute("UPDATE todos SET status='done' WHERE id=?", (tid,)); st.conn.commit()
closed = send({"config": cfg, "store": st, "title": "x", "body": "y", "todo_id": tid})
st.conn.execute("UPDATE todos SET status='open' WHERE id=?", (tid,)); st.conn.commit()
real = send({"config": cfg, "store": st, "title": "x", "body": "y", "todo_id": tid})
print(json.dumps({"no_todo": no_todo, "closed": closed, "real": real,
                  "rows": len(st.deliveries(limit=0))}))
PY
)"
[ "$(printf '%s' "$T8" | json_get "d['no_todo']['skipped']")" = "no-todo" ] \
  && [ "$(printf '%s' "$T8" | json_get "d['closed']['skipped']")" = "no-todo" ] \
  && [ "$(printf '%s' "$T8" | json_get "d['real']['ok']")" = "True" ] \
  && [ "$(printf '%s' "$T8" | json_get "d['rows']")" = "1" ] \
  && pass "T8 无 todo_id / 待办已 done ⇒ skipped=no-todo 且不建行；真待办照发" \
  || fail "T8 无待办不弹" "out=$T8"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
