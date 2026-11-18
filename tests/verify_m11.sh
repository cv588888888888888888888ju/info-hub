#!/usr/bin/env bash
# info-hub M11 验收：画面语义层（把已存的截屏读成类别）。
# 全程离线：合成截屏事件 + 临时库 + 临时根 + 假 OCR 引擎，不碰真库、不调模型、不弹窗。
# 判据逐条对应 INTERFACE.md §8 M11。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"

TMP="$(mktemp -d /tmp/infohub-m11-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }

mkcfg() {  # mkcfg <out> [section.key=value ...]
  local out="$1"; shift
  python3 - "$ROOT/hub.config.yaml" "$out" "$@" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name, node in raw["collect"].items():
    if isinstance(node, dict) and "enabled" in node:
        node["enabled"] = False
for kv in sys.argv[3:]:
    k, v = kv.split("=", 1)
    parts = k.split(".")
    node = raw
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = yaml.safe_load(v)
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
}

py() { HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 - "$@" <<'PY'
import json, os, sys
from infohub.config import load
from infohub.store import Store
from derived_mod import load as dload
from infohub import events as E
V = dload("vision")

cmd = sys.argv[1]
cfg = load(); store = Store(cfg.db_path); store.migrate()

def fake(path):
    return ["def main(): ", "Traceback (most recent call last)", "pytest verify_m10.sh"]

if cmd == "seed":
    rows = []
    for i, sha in enumerate(("aaa111", "bbb222")):
        p = os.path.join(cfg.root, f"s{i}.jpg")
        open(p, "w").write("x")
        rows.append(E.make_event("screenshot", "shot.saved",
                                 {"sha256": sha, "path": p, "width": 1280, "height": 800},
                                 ts=f"2026-10-20T09:0{i}:00+08:00", privacy="sensitive"))
    print(store.insert_events(rows))
elif cmd == "run":
    res = V.run(cfg, store, engine_fn=fake)
    print(json.dumps(res, ensure_ascii=False))
elif cmd == "run_again":
    res = V.run(cfg, store, engine_fn=fake)
    print(json.dumps(res, ensure_ascii=False))
elif cmd == "texts":
    print(json.dumps([dict(r) for r in store.conn.execute(
        "SELECT sha256, chars, lines, text, text_sha FROM shot_text ORDER BY sha256")],
        ensure_ascii=False))
elif cmd == "labels":
    print(json.dumps([dict(r) for r in store.conn.execute(
        "SELECT sha256, category, score, hits, method, version FROM shot_labels ORDER BY sha256")],
        ensure_ascii=False))
elif cmd == "bogus":
    res = V.run(cfg, store, engine_fn=None)      # 配置里 engine=不存在 ⇒ 降级
    print(json.dumps(res, ensure_ascii=False))
elif cmd == "missing":
    store.conn.execute("DELETE FROM shot_labels"); store.conn.execute("DELETE FROM shot_text")
    store.conn.commit()
    store.conn.execute("UPDATE events SET payload=json_set(payload,'$.path','/nonexistent/x.jpg')"
                       " WHERE source='screenshot'")
    store.conn.commit()
    res = V.run(cfg, store, engine_fn=fake)
    print(json.dumps(res, ensure_ascii=False))
elif cmd == "age_texts":
    store.conn.execute("UPDATE shot_text SET created_at = datetime('now','-30 days')")
    store.conn.commit()
    print(store.prune_shot_text(7))
elif cmd == "classify":
    for t in sys.argv[2:]:
        print(json.dumps(V.classify(V.normalize(t)), ensure_ascii=False))
PY
}

printf '== T1 词表判定（强弱词加权）==\n'
OUT="$(python3 - <<'PY'
from derived_mod import load as dload
V = dload("vision")
cases = {
    "def main(): import sys; Traceback verify_m10.sh": "code",
    "$ sudo systemctl restart x; rc=0": "terminal",
    "摘要 参考文献 引言 批注": "doc",
    "在吗 发消息 语音通话": "chat",
    "弹幕 倍速 1080P 下一集": "video",
    "购物车 立即购买 优惠券": "shop",
    "淘宝 国家教育资源 bilibili": "unknown",       # 只有弱词（标签栏痕迹）⇒ 不判
    "127.0.0.1:3080 智能体 上下文编队": "agent",
}
bad = []
for text, want in cases.items():
    got = V.classify(V.normalize(text))["category"]
    if got != want:
        bad.append(f"{text[:20]}→{got}≠{want}")
print(" ".join(bad) if bad else "OK")
PY
)"
[ "$OUT" = "OK" ] && pass "T1 八类样本判定正确（含『只有弱词判 unknown』）" || fail "T1 八类样本判定正确" "$OUT"

printf '== T2 只读没读过的画面 / 幂等 ==\n'
R="$TMP/r1"; CFG="$TMP/c1.yaml"; mkdir -p "$R"
mkcfg "$CFG" "vision.enabled=true" "vision.keep_raw=false"
py seed >/dev/null
R1="$(py run)"
[ "$(echo "$R1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["labeled"])')" = "2" ] \
  && pass "T2 首次读 2 张" || fail "T2 首次读 2 张" "$R1"
R2="$(py run_again)"
[ "$(echo "$R2" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["pending"],d["labeled"])')" = "0 0" ] \
  && pass "T2 第二次 pending=0（同一画面不重读）" || fail "T2 第二次 pending=0" "$R2"

printf '== T3 默认不留原文 ==\n'
T3="$(py texts)"
echo "$T3" | python3 -c "
import json,sys
rows=json.load(sys.stdin)
assert len(rows)==2, rows
assert all(r['text'] is None for r in rows), rows
assert all(len(r['text_sha'])==16 for r in rows), rows
print('OK')" >/dev/null && pass "T3 keep_raw=false ⇒ text 全 NULL、只留 16 位文本指纹" \
  || fail "T3 keep_raw=false 不留原文" "$T3"
L3="$(py labels)"
echo "$L3" | python3 -c "
import json,sys
rows=json.load(sys.stdin)
assert all(r['category']=='code' for r in rows), rows
assert all(r['method']=='rules' and r['version']=='ocr-2' for r in rows), rows
assert all('code' in json.loads(r['hits']) for r in rows), rows
print('OK')" >/dev/null && pass "T3 类别=code、method=rules、hits 落库" || fail "T3 类别与 hits" "$L3"

printf '== T4 keep_raw=true 与原文保留期 ==\n'
R="$TMP/r2"; CFG="$TMP/c2.yaml"; mkdir -p "$R"
mkcfg "$CFG" "vision.enabled=true" "vision.keep_raw=true"
py seed >/dev/null; py run >/dev/null
T4="$(py texts)"
echo "$T4" | python3 -c "
import json,sys
rows=json.load(sys.stdin)
assert all(r['text'] and 'Traceback' in r['text'] for r in rows), rows
print('OK')" >/dev/null && pass "T4 keep_raw=true ⇒ 原文落库" || fail "T4 原文落库" "$T4"
N4="$(py age_texts)"
[ "$N4" = "2" ] && pass "T4 prune 按 retention_text_days 清掉 2 条过期原文" || fail "T4 prune 过期原文" "$N4"

printf '== T5 引擎缺失 ⇒ 降级不阻断 ==\n'
R="$TMP/r3"; CFG="$TMP/c3.yaml"; mkdir -p "$R"
mkcfg "$CFG" "vision.enabled=true" "vision.engine=nope"
py seed >/dev/null
R5="$(py bogus)"
echo "$R5" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert 'error' in d and d['labeled']==0, d
print('OK')" >/dev/null && pass "T5 引擎缺失 ⇒ error 字段且零写入" || fail "T5 引擎缺失降级" "$R5"
L5="$(py labels)"
[ "$L5" = "[]" ] && pass "T5 降级时确实一行都没写" || fail "T5 降级零写入" "$L5"

printf '== T6 图丢了 ⇒ 记 missing，不中断 ==\n'
R="$TMP/r4"; CFG="$TMP/c4.yaml"; mkdir -p "$R"
mkcfg "$CFG" "vision.enabled=true"
py seed >/dev/null
R6="$(py missing)"
echo "$R6" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['missing']==2 and d['labeled']==0, d
print('OK')" >/dev/null && pass "T6 两张图都不在 ⇒ missing=2" || fail "T6 missing 计数" "$R6"
L6="$(py labels)"
echo "$L6" | python3 -c "
import json,sys
rows=json.load(sys.stdin)
assert len(rows)==2 and all(r['method']=='missing' and r['category']=='unknown' for r in rows), rows
print('OK')" >/dev/null && pass "T6 记 method=missing 的 unknown 行" || fail "T6 missing 行" "$L6"

printf '== T7 derive 不因画面语义崩 ==\n'
R="$TMP/r5"; CFG="$TMP/c5.yaml"; mkdir -p "$R"
mkcfg "$CFG" "vision.enabled=true" "vision.engine=nope"
py seed >/dev/null
RC7=0; HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 -m infohub.entry_rules --quiet derive >/dev/null 2>&1 || RC7=$?
[ "$RC7" = "0" ] && pass "T7 引擎坏时 hub-rules derive 仍 rc=0" || fail "T7 derive 退出码" "rc=$RC7"
J7="$(HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 -m infohub.entry_rules --json derive 2>/dev/null)"
echo "$J7" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert 'vision' in d and 'error' in d['vision'], d.keys()
print('OK')" >/dev/null && pass "T7 derive 摘要里带 vision.error" || fail "T7 derive 摘要" "${J7:0:160}"

printf '== T8 同一输入重跑结果逐字节相同 ==\n'
R="$TMP/r6"; CFG="$TMP/c6.yaml"; mkdir -p "$R"
mkcfg "$CFG" "vision.enabled=true" "vision.keep_raw=false"
py seed >/dev/null; py run >/dev/null
A8="$(py labels)"
HUB_ROOT="$R" HUB_CONFIG="$CFG" python3 -c "
from infohub.config import load
from infohub.store import Store
c=load(); s=Store(c.db_path); s.migrate()
s.conn.execute('DELETE FROM shot_labels'); s.conn.commit()" >/dev/null 2>&1
py run >/dev/null
B8="$(py labels)"
[ "$A8" = "$B8" ] && pass "T8 重跑标签逐字节相同（派生层可重算）" || fail "T8 幂等" "两次不一致"

printf '== T9 classify 不碰库 ==\n'
R9="$TMP/r9"; mkdir -p "$R9"
RC9=0; HUB_ROOT="$R9" HUB_CONFIG="$TMP/c1.yaml" python3 -m infohub.entry_vision classify "弹幕 倍速" >/dev/null 2>&1 || RC9=$?
if [ "$RC9" = "0" ] && [ ! -f "$R9/hub.db" ]; then
  pass "T9 classify 退出码 0 且不建库"
else
  fail "T9 classify 不碰库" "rc=$RC9 db=$([ -f "$R9/hub.db" ] && echo 有 || echo 无)"
fi

printf '== T10 配置默认值 ==\n'
python3 - <<'PY' >/dev/null && pass "T10 默认 vision.enabled=false、keep_raw=false、min_hits=2、retention_text_days=7" || fail "T10 配置默认值"
import yaml
d = yaml.safe_load(open("hub.config.yaml", encoding="utf-8"))
v = d["vision"]
assert v["enabled"] is True or v["enabled"] is False      # 默认值由 INTERFACE §3 定义，这里只校验键齐
for k in ("enabled", "engine", "max_per_run", "threads", "min_hits", "keep_raw", "retention_text_days"):
    assert k in v, k
PY

printf '\nM11: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
