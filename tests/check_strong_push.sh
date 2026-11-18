#!/usr/bin/env bash
# info-hub S13 验收：强推送两端（手机腿只建 pending 行 + 电脑腿 zenity 弹窗）
# 判据：phone 行 kind=strong 且 status=pending（等回复）；desktop 行把 zenity 返回值写成 reply 并 status=replied；
#       --no-reply 时 desktop 只确认、不写 reply；--phone/--desktop 只跑一条腿；--dry-run 不落库。
# 只在隔离环境跑：临时根 + 临时配置 + PATH 注入假 zenity（不弹真窗口、不碰真库）。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src"

TMP="$(mktemp -d /tmp/infohub-s13-XXXXXX)"
trap '/usr/bin/rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
ok() { if [ "$1" = "$2" ]; then pass "$3"; else FAIL=$((FAIL+1)); printf 'FAIL %s  (%s != %s)\n' "$3" "$1" "$2"; fi; }

# 假 zenity：把 --forms 的选项与回复拼成 "选项|回复" 打到 stdout，退出 0
mkdir -p "$TMP/bin"
cat > "$TMP/bin/zenity" <<'SH'
#!/usr/bin/env bash
echo "$FAKE_ZENITY_OUT"
exit 0
SH
chmod +x "$TMP/bin/zenity"
export PATH="$TMP/bin:$PATH"

R0="$TMP/r0"; C0="$TMP/c0.yaml"
python3 - "$ROOT/hub.config.yaml" "$C0" "$R0" <<'PY'
import sys, yaml
src, dst, root = sys.argv[1], sys.argv[2], sys.argv[3]
raw = yaml.safe_load(open(src, encoding="utf-8"))
raw["root"] = root
yaml.safe_dump(raw, open(dst, "w", encoding="utf-8"), allow_unicode=True)
PY
run() { HUB_ROOT="$R0" HUB_CONFIG="$C0" "$@"; }
sql() { HUB_ROOT="$R0" HUB_CONFIG="$C0" python3 - "$1" <<'PY'
import sys
from infohub.config import load
from infohub.store import Store
st = Store(load().db_path)
st.migrate()
print(eval(sys.argv[1]))
PY
}

# T1 两条腿都发：phone=pending，computer=replied + reply 来自 zenity
out=$(FAKE_ZENITY_OUT='收到|我在电脑上打的字' run "$ROOT/bin/hub-strong-push" --title 两端 --body 正文 --option 收到 --option 稍后 2>&1)
rc=$?
ok "$rc" 0 "T1 退出码 0"
ok "$(sql "st.deliveries(limit=1)[0]['channel']")" "computer" "T1 最近一行是 computer"
ok "$(sql "st.deliveries(limit=1)[0]['status']")" "replied" "T1 computer 行 status=replied"
ok "$(sql "st.deliveries(limit=1)[0]['reply']")" "我在电脑上打的字" "T1 computer 行记打字回复"
ok "$(sql "[r for r in st.deliveries(limit=5) if r['channel']=='phone'][0]['kind']")" "strong" "T1 phone 行 kind=strong"
ok "$(sql "[r for r in st.deliveries(limit=5) if r['channel']=='phone'][0]['status']")" "pending" "T1 phone 行保持 pending"

# T2 去重语义：phone 行不回就一直是 pending（info-hub 看得见），不会被当成 delivered
ok "$(sql "sum(1 for r in st.deliveries(limit=50) if r['channel']=='phone' and r['status']=='pending')")" 1 "T2 pending 累积计数"

# T3 --no-reply：只确认，不写 reply
FAKE_ZENITY_OUT='' run "$ROOT/bin/hub-strong-push" --desktop --no-reply --title 只确认 --body 不给回复 >/dev/null 2>&1
ok "$(sql "st.deliveries(limit=1)[0]['status']")" "delivered" "T3 --no-reply ⇒ delivered"
ok "$(sql "st.deliveries(limit=1)[0]['reply']")" "None" "T3 --no-reply ⇒ reply 为空"

# T4 --phone 只跑一条腿
before=$(sql "len(st.deliveries(limit=200))")
run "$ROOT/bin/hub-strong-push" --phone --title 只有手机 --body x >/dev/null 2>&1
after=$(sql "len(st.deliveries(limit=200))")
ok "$((after - before))" 1 "T4 --phone 只建一行"

# T5 --dry-run 不落库
before=$(sql "len(st.deliveries(limit=200))")
run "$ROOT/bin/hub-strong-push" --title 演练 --body y --dry-run >/dev/null 2>&1
ok "$(sql "len(st.deliveries(limit=200))")" "$before" "T5 --dry-run 不落库"

echo "---- $PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ] && echo "ALL PASS" || echo "FAILED"
exit "$FAIL"
