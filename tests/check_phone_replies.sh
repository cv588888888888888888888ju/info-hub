#!/usr/bin/env bash
# helper 回执同步验收（INTERFACE §11.7b 的回执通道）：假 adb + 假 all.jsonl，不碰真手机、不碰真库。
# 判据：dry-run 不写库；apply 按行折成 ack/reply 并推进游标；重复跑不重复处理；追加只处理新行；
# 坏行/不存在的 id 进 skipped 不挡整批；文件被截短 ⇒ rotated 从头读；文件不存在 ⇒ 空而不是报错。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FAKE="$HERE/stubs/adb_fake.sh"
export PYTHONPATH="$ROOT/src"
TMP="$(mktemp -d /tmp/infohub-replies-XXXXXX)"
trap '/usr/bin/rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "$2"; }

R="$TMP/root"; C="$TMP/c.yaml"; mkdir -p "$R"
python3 - "$ROOT/hub.config.yaml" "$C" "$R" "$FAKE" <<'PY'
import sys, yaml
src, dst, root, fake = sys.argv[1:5]
raw = yaml.safe_load(open(src, encoding="utf-8"))
raw["root"] = root
raw["phone_remind"]["adb"] = fake
raw["phone_remind"]["device"] = "fake-serial"
raw["phone_remind"]["backend"] = "helper"
yaml.safe_dump(raw, open(dst, "w", encoding="utf-8"), allow_unicode=True)
PY
REPLIES="$TMP/all.jsonl"; : > "$REPLIES"
export ADB_FAKE_LOG="$TMP/adb.log" ADB_FAKE_REPLIES="$REPLIES" ADB_FAKE_INSTALLED=1
export HUB_ROOT="$R" HUB_CONFIG="$C"

python3 - <<'PY' >/dev/null
from infohub.config import load
from infohub.store import Store
st = Store(load().db_path); st.migrate()
st.add_delivery({"id": "d-a1", "channel": "phone", "title": "问题一", "body": ""})
st.add_delivery({"id": "d-a2", "channel": "phone", "title": "问题二", "body": ""})
PY
q() { python3 -c "from infohub.config import load;from infohub.store import Store;r=Store(load().db_path).delivery('$1');print(' '.join(str(r[k]) for k in $2))"; }

printf '%s\n' '{"id":"d-a1","kind":"reply","text":"好的","src":"remoteinput","at":"2026-11-14T17:30:00+08:00"}' '{"id":"d-a2","kind":"action","text":null,"action":"ack","src":"button","at":"2026-11-14T17:31:00+08:00"}' > "$REPLIES"

OUT=$("$ROOT/bin/hub-phone-replies" --json)
printf '%s' "$OUT" | grep -q '"new": 2' && pass "T1 dry-run 看到 2 条" || fail "T1 dry-run 看到 2 条" "$OUT"
ST=$(q d-a1 "['status']")
[ "$ST" = "pending" ] && pass "T2 dry-run 不写库" || fail "T2 dry-run 不写库" "$ST"

OUT=$("$ROOT/bin/hub-phone-replies" --apply --json)
printf '%s' "$OUT" | grep -q '"new": 2' && pass "T3 apply 处理 2 条" || fail "T3 apply 处理 2 条" "$OUT"
ST1=$(q d-a1 "['status','reply']")
[ "$ST1" = "replied 好的" ] && pass "T4 reply 落库" || fail "T4 reply 落库" "$ST1"
ST2=$(q d-a2 "['status','action']")
[ "$ST2" = "acked ack" ] && pass "T5 action 折成 ack" || fail "T5 action 折成 ack" "$ST2"
CUR=$(cat "$R/state/phone-replies.cursor")
[ "$CUR" = "2" ] && pass "T6 游标推进到 2" || fail "T6 游标推进到 2" "$CUR"

OUT=$("$ROOT/bin/hub-phone-replies" --apply --json)
printf '%s' "$OUT" | grep -q '"new": 0' && pass "T7 重复跑不重复处理" || fail "T7 重复跑不重复处理" "$OUT"

printf '%s\n' '{"id":"d-a1","kind":"reply","text":"补充一句"}' >> "$REPLIES"
OUT=$("$ROOT/bin/hub-phone-replies" --apply --json)
printf '%s' "$OUT" | grep -q '"new": 1' && pass "T8 追加只处理新行" || fail "T8 追加只处理新行" "$OUT"
ST1=$(q d-a1 "['reply']")
[ "$ST1" = "补充一句" ] && pass "T9 回复被覆盖为最新" || fail "T9 回复被覆盖为最新" "$ST1"

printf '%s\n' '{"id":"d-a1","mode":"reply","source":"remoteinput","text":"旧键也能读","received_at":"2026-11-14T17:32:00Z"}' >> "$REPLIES"
OUT=$("$ROOT/bin/hub-phone-replies" --apply --json)
printf '%s' "$OUT" | grep -q '"new": 1' && pass "T9b 旧键（mode/source）也认" || fail "T9b 旧键（mode/source）也认" "$OUT"
ST1=$(q d-a1 "['reply']")
[ "$ST1" = "旧键也能读" ] && pass "T9c 旧键回复落库" || fail "T9c 旧键回复落库" "$ST1"

printf '%s\n' 'not-json' '{"id":"d-nope","kind":"reply","text":"x"}' >> "$REPLIES"
OUT=$("$ROOT/bin/hub-phone-replies" --apply --json)
printf '%s' "$OUT" | grep -q 'bad-line' && pass "T10 坏行进 skipped" || fail "T10 坏行进 skipped" "$OUT"
printf '%s' "$OUT" | grep -q 'not-found' && pass "T11 不存在的 id 进 skipped" || fail "T11 不存在的 id 进 skipped" "$OUT"

: > "$REPLIES"; printf '%s\n' '{"id":"d-a2","kind":"reply","text":"截短后"}' > "$REPLIES"
OUT=$("$ROOT/bin/hub-phone-replies" --apply --json)
printf '%s' "$OUT" | grep -q '"rotated": true' && pass "T12 文件截短 ⇒ rotated" || fail "T12 文件截短 ⇒ rotated" "$OUT"
ST2=$(q d-a2 "['reply']")
[ "$ST2" = "截短后" ] && pass "T13 截短后仍能落地" || fail "T13 截短后仍能落地" "$ST2"

export ADB_FAKE_REPLIES="$TMP/missing.jsonl"
OUT=$("$ROOT/bin/hub-phone-replies" --json 2>&1); RC=$?
[ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q '"total": 0' && pass "T14 文件不存在 ⇒ 空而不是报错" || fail "T14 文件不存在 ⇒ 空而不是报错" "rc=$RC $OUT"

echo "---- $PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
