#!/usr/bin/env bash
# A19：重要/紧急口径外置到 info-hub/agent.md，life-task 读同一份。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HUB="$(cd "$HERE/.." && pwd)"
LT_CLIENT=~/文档/deepseek-harness/default-workspace/wxstack/client/bin/life-task
LT_SERVER=~/文档/deepseek-harness/default-workspace/wxstack/server/wxhub/life-task
TMP="$(mktemp -d)"
trap '/usr/bin/rm -rf "$TMP"' EXIT
n=0; bad=0
ok(){ n=$((n+1)); echo "ok  $*"; }
no(){ bad=$((bad+1)); echo "FAIL $*"; }

test -f "$HUB/agent.md" && ok "agent.md 存在" || no "agent.md 缺失"

c1=$(grep -c "进板的唯一门槛" "$LT_CLIENT" 2>/dev/null || true)
c2=$(grep -c "进板的唯一门槛" "$LT_SERVER" 2>/dev/null || true)
[ "$c1" = 0 ] && ok "client 无硬编码长口径" || no "client 仍硬编码（$c1 处）"
[ "$c2" = 0 ] && ok "server 副本无硬编码长口径" || no "server 仍硬编码（$c2 处）"

mkdir -p "$TMP/home/.local/share/life-board"
python3 - "$TMP/hub.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("""CREATE TABLE todos (id TEXT PRIMARY KEY, title TEXT, why TEXT DEFAULT '',
 confidence REAL DEFAULT 0.5, status TEXT DEFAULT 'open', human_override INTEGER DEFAULT 0,
 board_card_id TEXT, dedupe_key TEXT, created_at TEXT)""")
db.execute("INSERT INTO todos (id,title,why,created_at) VALUES ('t1','交材料','明天截止','2026-11-14T00:00:00')")
db.commit()
PY
printf '{"info_hub_db": "%s"}\n' "$TMP/hub.db" > "$TMP/home/.local/share/life-board/config.json"

cp "$HUB/agent.md" "$TMP/agent.md"
printf '\n哨兵-SENTINEL-A19\n' >> "$TMP/agent.md"

out=$(HOME="$TMP/home" LIFE_JUDGE_AGENT_MD="$TMP/agent.md" "$LT_CLIENT" import --from-info-hub --print-prompt 2>"$TMP/err1")
rc=$?
[ "$rc" = 0 ] && ok "print-prompt 退出 0" || no "print-prompt 退出 $rc：$(cat "$TMP/err1")"
case "$out" in *哨兵-SENTINEL-A19*) ok "提示词含 agent.md 哨兵" ;; *) no "提示词不含哨兵" ;; esac
case "$out" in *"候选："*) ok "提示词含候选占位替换" ;; *) no "提示词缺少候选段" ;; esac
case "$out" in *t1*) ok "提示词含 fixture 候选 t1" ;; *) no "提示词缺 t1" ;; esac

HOME="$TMP/home" LIFE_JUDGE_AGENT_MD="$TMP/不存在.md" "$LT_CLIENT" import --from-info-hub --print-prompt >"$TMP/o2" 2>"$TMP/err2"
rc=$?
[ "$rc" = 2 ] && ok "缺文件退出 2" || no "缺文件退出 $rc"
grep -q "$TMP/不存在.md" "$TMP/err2" && ok "stderr 点名绝对路径" || no "stderr 未点名路径：$(cat "$TMP/err2")"

echo "---- $n ok, $bad fail"
[ "$bad" = 0 ]
