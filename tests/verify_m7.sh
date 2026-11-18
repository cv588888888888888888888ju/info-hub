#!/usr/bin/env bash
# info-hub M7 验收：静默截屏（活跃才取帧 → 缩放图落盘 → sensitive 事件 → 7 天留存）。
# 全程离线、**不碰真 portal、不弹任何对话框**：取帧走 backend=command 的假命令，活跃度走假 gdbus。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"
COLLECT=(python3 -m infohub.entry_collect)
SHOT=(python3 -m infohub.entry_screencast)

TMP="$(mktemp -d /tmp/infohub-m7-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# ---------------------------------------------------------------- 假 gdbus（活跃度探针）
mkdir -p "$TMP/bin"
cat > "$TMP/bin/gdbus" <<'SHGDBUS'
#!/usr/bin/env bash
# 假 mutter 空闲探针（毫秒）：$IDLE_SEQ_FILE = 空格分隔的序列，按调用次序取值、用尽后一直取最后一个
# （计数器在同名 .cnt）；$IDLE_FILE = 单个值。两个都不存在 ⇒ 报错（模拟探针失败）。
val=""
if [ -n "${IDLE_SEQ_FILE:-}" ] && [ -f "$IDLE_SEQ_FILE" ]; then
  cnt="$IDLE_SEQ_FILE.cnt"
  n=0; [ -f "$cnt" ] && n="$(cat "$cnt" 2>/dev/null || echo 0)"
  vals=($(cat "$IDLE_SEQ_FILE"))
  last=$(( ${#vals[@]} - 1 ))
  idx=$n; [ "$idx" -gt "$last" ] && idx=$last
  val="${vals[$idx]}"
  printf '%s\n' "$(( n + 1 ))" > "$cnt"
elif [ -n "${IDLE_FILE:-}" ] && [ -f "$IDLE_FILE" ]; then
  val="$(cat "$IDLE_FILE")"
fi
[ -n "$val" ] || { echo "fake gdbus: no idle file" >&2; exit 1; }
printf '(uint32 %s,)\n' "$val"
SHGDBUS
chmod 755 "$TMP/bin/gdbus"
export PATH="$TMP/bin:$PATH"
IDLE_FILE="$TMP/idle_ms"
export IDLE_FILE
echo 10000 > "$IDLE_FILE"         # 默认：空闲 10 秒 = 活跃（< idle_max_s 60）且**已停手**（≥ idle_min_s 3）
# 打字保护那段（T3b/T3c）用 IDLE_SEQ_FILE 逐次喂值；这里先保证它没被上一轮残留污染
unset IDLE_SEQ_FILE

# ---------------------------------------------------------------- 假取帧命令（真写一张 8x8 JPEG）
cat > "$TMP/shot_stub.py" <<'PYSTUB'
import base64, os, sys
from pathlib import Path
PX = ("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8SEhEP"
      "ERETFhwXExQaFRERGCEYGh0dHx8fExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEUHh4eHh4e"
      "Hh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAARCAAIAAgDAREA"
      "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
      "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
      "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
      "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
      "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
      "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
      "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
      "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD6roA/"
      "/9k=")
out = os.environ.get("HUB_SHOT_OUT") or (sys.argv[1] if len(sys.argv) > 1 else "")
Path(out).parent.mkdir(parents=True, exist_ok=True)
Path(out).write_bytes(base64.b64decode(PX))
log = os.environ.get("HUB_SHOT_LOG")
if log:
    Path(log).write_text(f"{out} max_width={os.environ.get('HUB_SHOT_MAX_WIDTH')}"
                         f" quality={os.environ.get('HUB_SHOT_QUALITY')}\n")
PYSTUB

mkcfg() {  # mkcfg <out> [key=value ...]  —— key 形如 collect.screenshot.max_width
  local out="$1"; shift
  python3 - "$ROOT/hub.config.yaml" "$out" "$TMP/shot_stub.py" "$@" <<'PYCFG'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name in ("dsh-session", "clipboard", "window", "fs", "bilibili", "browser-history"):
    raw["collect"][name]["enabled"] = False
shot = raw["collect"]["screenshot"]
shot.update({"enabled": True, "backend": "command",
             "command": ["python3", sys.argv[3]], "interval_s": 300})
for kv in sys.argv[4:]:
    k, v = kv.split("=", 1)
    node = raw
    parts = k.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = yaml.safe_load(v)
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PYCFG
}

collectq() {  # collectq <cfg> <root> [args...]
  local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" HUB_SHOT_LOG="$TMP/shot.log" "${COLLECT[@]}" --json --quiet "$@"
}
shotq() {
  local cfg="$1" root="$2"; shift 2
  HUB_ROOT="$root" HUB_CONFIG="$cfg" HUB_SHOT_LOG="$TMP/shot.log" "${SHOT[@]}" "$@"
}
sql() {
  local root="$1" expr="$2"
  HUB_ROOT="$root" python3 - "$expr" <<'PYSQL' | tail -1
import sys
sys.path.insert(0, "src")
from infohub.config import load
from infohub.store import Store
c = load(); s = Store(c.db_path); s.migrate()
try:
    print(eval(sys.argv[1]))
finally:
    s.close()
PYSQL
}
shots_of() { find "$1/screens" -name '*.jpg' 2>/dev/null | wc -l; }

CFG="$TMP/c.yaml"; R="$TMP/r"; mkcfg "$CFG"

# ---------------------------------------------------------------- T1 活跃 ⇒ 取一帧 + sensitive 事件
O1="$(collectq "$CFG" "$R" --source screenshot)"
EM1="$(printf '%s' "$O1" | tail -1 | json_get "d['sources']['screenshot']['emitted']")"
[ "$EM1" = "1" ] && pass "T1 活跃时取到 1 帧（emitted=1）" || fail "T1 取帧" "emitted=$EM1 out=$O1"
[ "$(shots_of "$R")" = "1" ] && pass "T1b 图真落盘（1 张 .jpg）" || fail "T1b 落盘" "shots=$(shots_of "$R")"
K1="$(sql "$R" "[(r['source'], r['kind'], r['privacy']) for r in s.events(10)]")"
[ "$K1" = "[('screenshot', 'shot.saved', 'sensitive')]" ] \
  && pass "T1c 事件口径 source/kind/privacy（sensitive）" || fail "T1c 事件口径" "got=$K1"
WH="$(sql "$R" "(lambda e: (e['payload']['width'], e['payload']['height'], e['payload']['idle_s'] is not None, e['payload']['rel'].startswith('screens/')))(s.events(1)[0])")"
[ "$WH" = "(8, 8, True, True)" ] \
  && pass "T1d 元数据：尺寸 8x8 + 空闲秒数 + root 相对路径" || fail "T1d 元数据" "got=$WH"
SUM_OK="$(sql "$R" "(lambda e: e['payload']['sha256'] == __import__('infohub.sources', fromlist=['x']).load_module('screenshot').digest_of(e['payload']['path']))(s.events(1)[0])")"
[ "$SUM_OK" = "True" ] && pass "T1e 事件里的 sha256 == 文件内容哈希（可对账）" || fail "T1e 哈希对账" "got=$SUM_OK"

# ---------------------------------------------------------------- T2 间隔门
O2="$(collectq "$CFG" "$R" --source screenshot)"
EM2="$(printf '%s' "$O2" | tail -1 | json_get "d['sources']['screenshot']['emitted']")"
[ "$EM2" = "0" ] && [ "$(shots_of "$R")" = "1" ] \
  && pass "T2 间隔内再跑一轮不再取（emitted=0）" || fail "T2 间隔门" "emitted=$EM2 shots=$(shots_of "$R")"

# ---------------------------------------------------------------- T3 不活跃门
CFG3="$TMP/c3.yaml"; R3="$TMP/r3"; mkcfg "$CFG3"
echo 900000 > "$IDLE_FILE"        # 空闲 900 秒 = 不活跃
O3="$(collectq "$CFG3" "$R3" --source screenshot)"
EM3="$(printf '%s' "$O3" | tail -1 | json_get "d['sources']['screenshot']['emitted']")"
SKIP="$(sql "$R3" "((s.state_get('screenshot') or {}).get('cursor') or {}).get('last_skip')")"
[ "$EM3" = "0" ] && [ "$(shots_of "$R3")" = "0" ] && [ "$SKIP" = "idle" ] \
  && pass "T3 不活跃（空闲 900s）⇒ 不取帧、不发事件、状态记 idle" \
  || fail "T3 不活跃门" "emitted=$EM3 shots=$(shots_of "$R3") skip=$SKIP"

# ---------------------------------------------------------------- T3b 打字中 ⇒ 这轮不取（④ 打字保护）
CFG3B="$TMP/c3b.yaml"; R3B="$TMP/r3b"
mkcfg "$CFG3B" "collect.screenshot.pause_wait_s=0"
echo 1000 > "$IDLE_FILE"          # 空闲 1 秒 = 正在敲键（< idle_min_s 3）
O3B="$(collectq "$CFG3B" "$R3B" --source screenshot)"
EM3B="$(printf '%s' "$O3B" | tail -1 | json_get "d['sources']['screenshot']['emitted']")"
SKIP3B="$(sql "$R3B" "((s.state_get('screenshot') or {}).get('cursor') or {}).get('last_skip')")"
[ "$EM3B" = "0" ] && [ "$(shots_of "$R3B")" = "0" ] && [ "$SKIP3B" = "typing" ] \
  && pass "T3b 打字中（空闲 1s < idle_min_s=3）⇒ 不取帧、不发事件、状态记 typing" \
  || fail "T3b 打字保护" "emitted=$EM3B shots=$(shots_of "$R3B") skip=$SKIP3B"

# ---------------------------------------------------------------- T3c 轮询到停顿 ⇒ 仍取（等的是停顿，不是放弃）
CFG3C="$TMP/c3c.yaml"; R3C="$TMP/r3c"
mkcfg "$CFG3C" "collect.screenshot.pause_wait_s=5"
IDLE_SEQ_FILE="$TMP/idle_seq"; export IDLE_SEQ_FILE
printf '1000 1000 10000\n' > "$IDLE_SEQ_FILE"; rm -f "$IDLE_SEQ_FILE.cnt"
O3C="$(collectq "$CFG3C" "$R3C" --source screenshot)"
EM3C="$(printf '%s' "$O3C" | tail -1 | json_get "d['sources']['screenshot']['emitted']")"
IDLE3C="$(sql "$R3C" "s.events(1)[0]['payload']['idle_s']")"
rm -f "$IDLE_SEQ_FILE" "$IDLE_SEQ_FILE.cnt"; unset IDLE_SEQ_FILE
[ "$EM3C" = "1" ] && [ "$(shots_of "$R3C")" = "1" ] && [ "$IDLE3C" = "10.0" ] \
  && pass "T3c 打字中轮询（1s→1s→10s）⇒ 等到停顿后仍取到 1 帧" \
  || fail "T3c 打字保护轮询" "emitted=$EM3C shots=$(shots_of "$R3C") idle=$IDLE3C"
echo 10000 > "$IDLE_FILE"         # 回到默认：10 秒空闲 = 活跃且已停手

# ---------------------------------------------------------------- T4 探针失败 ⇒ 不取（fail-soft、rc=0）
rm -f "$IDLE_FILE"                # 假 gdbus 直接 exit 1
O4="$(collectq "$CFG3" "$R3" --source screenshot)"
RC4=$?
ERR4="$(sql "$R3" "((s.state_get('run:screenshot') or {}).get('last_error') or '')")"
[ "$RC4" = "0" ] && [ "$(shots_of "$R3")" = "0" ] && printf '%s' "$ERR4" | grep -q "活跃度探针失败" \
  && pass "T4 活跃度探不到 ⇒ 不取帧、记 run:screenshot、退出码仍 0" \
  || fail "T4 探针失败语义" "rc=$RC4 shots=$(shots_of "$R3") err=$ERR4"
echo 10000 > "$IDLE_FILE"

# ---------------------------------------------------------------- T5 require_active=false ⇒ 探针不参与
CFG_NB="$TMP/nb.yaml"; RNB="$TMP/rnb"; mkcfg "$CFG_NB" "collect.screenshot.require_active=false"
rm -f "$IDLE_FILE"
ONB="$(collectq "$CFG_NB" "$RNB" --source screenshot)"
EMNB="$(printf '%s' "$ONB" | tail -1 | json_get "d['sources']['screenshot']['emitted']")"
[ "$EMNB" = "1" ] && pass "T5 require_active=false ⇒ 探针失败也照取" || fail "T5 require_active=false" "emitted=$EMNB"
echo 10000 > "$IDLE_FILE"

# ---------------------------------------------------------------- T6 enabled=false ⇒ 不采
CFG_OFF="$TMP/off.yaml"; ROFF="$TMP/roff"; mkcfg "$CFG_OFF" "collect.screenshot.enabled=false"
collectq "$CFG_OFF" "$ROFF" >/dev/null
[ "$(shots_of "$ROFF")" = "0" ] && pass "T6 enabled=false ⇒ 一张都不取" || fail "T6 关闭语义" "shots=$(shots_of "$ROFF")"

# ---------------------------------------------------------------- T7 缩放/质量参数真传下去
CFG_S="$TMP/s.yaml"; RS="$TMP/rs"; rm -f "$TMP/shot.log"
mkcfg "$CFG_S" "collect.screenshot.max_width=640" "collect.screenshot.jpeg_quality=55"
collectq "$CFG_S" "$RS" --source screenshot >/dev/null
LOG="$(cat "$TMP/shot.log" 2>/dev/null)"
printf '%s' "$LOG" | grep -q "max_width=640" && printf '%s' "$LOG" | grep -q "quality=55" \
  && pass "T7 max_width / jpeg_quality 真传给了取帧命令" || fail "T7 参数透传" "log=$LOG"

# ---------------------------------------------------------------- T8 留存：删老图、留新图、事件不动
R8="$TMP/r8"; CFG8="$TMP/c8.yaml"; mkcfg "$CFG8"; collectq "$CFG8" "$R8" --source screenshot >/dev/null
NEW8="$(find "$R8/screens" -name '*.jpg' | head -1)"
OLD="$R8/screens/2026-01-01"; mkdir -p "$OLD"; cp "$NEW8" "$OLD/000000.jpg"
touch -d "8 days ago" "$OLD/000000.jpg"
EV_BEFORE="$(sql "$R8" "len(s.events(50))")"
P8="$(shotq "$CFG8" "$R8" --json prune | tail -1)"
REM="$(printf '%s' "$P8" | json_get "len(d['removed'])")"
EV_AFTER="$(sql "$R8" "len(s.events(50))")"
[ "$REM" = "1" ] && [ "$EV_BEFORE" = "$EV_AFTER" ] && [ "$(shots_of "$R8")" = "1" ] \
  && pass "T8 prune 删 1 张老图（留 7 天）、事件元数据不动" \
  || fail "T8 留存" "removed=$REM events=$EV_BEFORE→$EV_AFTER shots=$(shots_of "$R8")"

# ---------------------------------------------------------------- T9 status 面
ST="$(shotq "$CFG8" "$R8" --json status | tail -1)"
STOK="$(printf '%s' "$ST" | json_get "(d['enabled'], d['backend'], d['shots'], d['keep_days'])")"
[ "$STOK" = "(True, 'command', 1, 7)" ] && pass "T9 status 如实报（enabled/backend/shots/keep_days）" || fail "T9 status" "got=$STOK"

# ---------------------------------------------------------------- T10 grab 子命令 + 失败面
OUT="$TMP/grab.jpg"
G="$(shotq "$CFG8" "$R8" --json grab --out "$OUT" | tail -1)"
GOK="$(printf '%s' "$G" | json_get "(d['width'], d['height'], d['bytes'] > 0, d['backend'], __import__('os').path.isfile(d['path']))")"
[ "$GOK" = "(8, 8, True, 'command', True)" ] && pass "T10 grab 立刻取一帧并如实报尺寸/字节" || fail "T10 grab" "got=$GOK"
CFG_E="$TMP/e.yaml"; mkcfg "$CFG_E" "collect.screenshot.command=[]"
shotq "$CFG_E" "$R8" --json grab --out "$TMP/none.jpg" >/dev/null 2>&1
RC_G=$?
[ "$RC_G" = "1" ] && [ ! -f "$TMP/none.jpg" ] && pass "T10b 空命令 ⇒ 退出 1 且不写文件" || fail "T10b 失败面" "rc=$RC_G"

# ---------------------------------------------------------------- T11 包装面：prune 定时器也清图
grep -q "hub-screencast prune" "$ROOT/packaging/info-hub-prune.service" \
  && pass "T11 过期清理单元同时跑 hub-screencast prune（7 天留存不是口号）" \
  || fail "T11 prune 单元" "packaging/info-hub-prune.service 里没有截屏清理"

# ---------------------------------------------------------------- T12 单元 + 帮助面
UNIT="$(python3 - "$TMP" <<'PYUNIT'
import os, sys
sys.path.insert(0, "src")
os.environ["HUB_ROOT"] = sys.argv[1]
os.environ["HUB_CONFIG"] = sys.argv[1] + "/c.yaml"
from infohub.config import load
from derived_mod import load as dload
_SC = dload("screencast")
BACKENDS, jpeg_size, shots_dir, token_path = _SC.BACKENDS, _SC.jpeg_size, _SC.shots_dir, _SC.token_path
c = load()
ok = (BACKENDS == ("screencast", "command")
      and str(shots_dir(c)).endswith("/screens")
      and str(token_path(c)).endswith("/state/screencast.json")
      and jpeg_size(next(iter(sorted(__import__("glob").glob(os.path.join(sys.argv[1], "r", "screens", "*", "*.jpg")))), "/nonexistent")) == (8, 8))
print("UNIT_OK" if ok else "UNIT_BAD")
PYUNIT
)"
[ "$UNIT" = "UNIT_OK" ] && pass "T12 单元：BACKENDS 闭集 + 默认落盘/令牌路径 + JPEG 尺寸可读" || fail "T12 单元" "got=$UNIT"
"${SHOT[@]}" --help 2>&1 | grep -q "consent" && "${SHOT[@]}" --help 2>&1 | grep -q "prune" \
  && pass "T12b hub-screencast --help 列出 consent/grab/prune" || fail "T12b 帮助面" ""
# ---------------------------------------------------------------- T13 portal 契约（持久令牌）
# 机械钉住两件事：SelectSources 里必须带 persist_mode=2；restore_token 必须从 **Start** 的结果读。
# 依据（规范）：https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html
# —— restore_token 在 Start 的 Response 里返回，且只有设了 persist_mode 才会给；且它是**单次使用**的。
P13="$(python3 - "$ROOT/src" <<'PY13'
import sys
sys.path.insert(0, sys.argv[1])
from derived_mod import load as dload
SC = dload("screencast")

class V:
    def __init__(self, sig, val):
        self.sig, self.val = sig, val

class GLib:
    Variant = V

calls = []

def fake(Gio, GLib, conn, method, signature, params, timeout_s, handle_token):
    calls.append((method, signature, params))
    if method == "CreateSession":
        return {"session_handle": "/org/freedesktop/portal/desktop/session/1_1/x"}
    if method == "SelectSources":
        return {"restore_token": "OLD-FROM-SELECT"}      # 故意放个假的：实现不许从这里读
    if method == "Start":
        return {"streams": [[7, {"size": (1920, 1200)}]], "restore_token": "NEW-FROM-START"}
    raise AssertionError(method)

SC._call_request = fake
info = SC._open_session(None, GLib, object(), 5)
sel = [p for (m, _, p) in calls if m == "SelectSources"][0]
opts = sel[1]
print("token=%s persist=%s" % (info["restore_token"],
                               opts["persist_mode"].val if "persist_mode" in opts else "none"))
PY13
)"
[ "$P13" = "token=NEW-FROM-START persist=2" ] \
  && pass "T13 portal 契约：SelectSources 带 persist_mode=2、restore_token 从 Start 结果读" \
  || fail "T13 portal 契约" "got=$P13"

# ---------------------------------------------------------------- T14 无人值守：没令牌也不弹同意框
CFG14="$TMP/c14.yaml"; ROOT14="$TMP/r14"
mkcfg "$CFG14" "collect.screenshot.backend=screencast" "collect.screenshot.token_file=$TMP/no-such-token.json"
collectq "$CFG14" "$ROOT14" --source screenshot >/dev/null 2>&1
ERR14="$(sql "$ROOT14" "(s.state_get('run:screenshot') or {}).get('last_error') or ''")"
N14="$(sql "$ROOT14" "len(s.events(limit=100, source='screenshot'))")"
case "$ERR14" in
  *共享令牌*) [ "$N14" = "0" ] && pass "T14 没令牌时采集器直接失败（不弹同意框）：0 帧 + run:screenshot 记原因" \
                              || fail "T14 无人值守不弹框" "err=$ERR14 n=$N14" ;;
  *) fail "T14 无人值守不弹框" "err=$ERR14 n=$N14" ;;
esac


# ---------------------------------------------------------------- T15/T16 取帧尺寸：等比、两个维度都钉住
P15="$(python3 - "$ROOT/src" <<'PY15'
import sys
sys.path.insert(0, sys.argv[1])
from derived_mod import load as dload
target_size = dload("screencast").target_size
cases = [
    (1280, [1920, 1200], (1280, 800)),   # 16:10 等比，不是 1280x1600（真机踩过的变形）
    (4000, [1920, 1200], (1920, 1200)),  # max_width 大于源宽 ⇒ 不放大
    (640,  [1920, 1200], (640, 400)),
    (1280, None, None),
    (0,    [1920, 1200], None),
    (1280, [0, 0], None),
]
bad = []
for mw, size, want in cases:
    got = target_size(mw, size)
    if got != want:
        bad.append((mw, size, got, want))
print("BAD=%s" % bad if bad else "OK")
PY15
)"
[ "$P15" = "OK" ] && pass "T15 尺寸口径：按源宽高比等比、不放大、拿不到源尺寸就不猜" \
                  || fail "T15 尺寸口径" "got=$P15"

P16="$(python3 - "$ROOT/src" <<'PY16'
import sys
sys.path.insert(0, sys.argv[1])
from derived_mod import load as dload
_gst_desc = dload("screencast")._gst_desc
d = _gst_desc(7, 82, 1280, 80, [1920, 1200])
ok = ("width=1280,height=800" in d and "videoscale add-borders=true" in d
      and "jpegenc quality=80" in d and "width=[1," not in d)
print("OK" if ok else d)
PY16
)"
[ "$P16" = "OK" ] && pass "T16 管道串：宽高都钉死 + add-borders（宁可留黑边也不拉伸）" \
                  || fail "T16 管道串" "got=$P16"


printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
