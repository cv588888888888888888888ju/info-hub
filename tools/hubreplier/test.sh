#!/usr/bin/env bash
# hubreplier 一条命令验收：投递 → 断言通知结构 → 测试入口落盘 → 截图存证。
# 契约见 README.md §5。用法：
#   ./test.sh              # 确定性回归（默认）
#   HUB_TAP=1 ./test.sh    # 额外尝试「通知栏真点选项按钮」（需要解锁、无遮挡）
# 环境变量：HUB_DEVICE / HUB_EVIDENCE / HUB_TAP
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADB="${ADB:-adb}"
DEVICE="${HUB_DEVICE:-<serial>}"
PKG="com.chenty.hubreplier"
TOKEN="${HUB_TOKEN:-}"
EVID="${HUB_EVIDENCE:-~/hub-tasks/runs/core-deliver-plugins/evidence/hubreplier}"
TAP="${HUB_TAP:-0}"
D="-s $DEVICE"
ID="t-$(date +%Y%m%d-%H%M%S)"
OPT_DONE="完成任务"
OPT_LATER="稍后"

mkdir -p "$EVID"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS  $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
step() { echo; echo "==> $*"; }
sh_() { "$ADB" $D shell "$@" 2>/dev/null; }
jsonl_lines() { sh_ run-as "$PKG" cat files/replies/all.jsonl | wc -l | tr -d ' '; }

step "0. 环境"
[ "$("$ADB" $D get-state 2>/dev/null)" = "device" ] \
  && ok "设备 $DEVICE 在线" || { bad "设备 $DEVICE 不在线"; exit 1; }
sh_ pm list packages | grep -qx "package:$PKG" \
  && ok "已安装 $PKG" || { bad "$PKG 未安装（先跑 ./build.sh && ./install.sh）"; exit 1; }
LINES_BEFORE="$(jsonl_lines)"
echo "  all.jsonl 现有行数: $LINES_BEFORE"

step "1. 投递（am start，绕开 MIUI Greezer 丢广播）"
PAYLOAD=$(printf '{"id":"%s","mode":"question","title":"自测 %s","body":"点选项或回复","options":[{"key":"done","label":"%s"},{"key":"later","label":"%s"}],"token":"%s"}' \
  "$ID" "$ID" "$OPT_DONE" "$OPT_LATER" "$TOKEN")
B64=$(printf '%s' "$PAYLOAD" | base64 -w 0)
sh_ am start -n "$PKG/.HubActivity" --es op show --es payload_b64 "$B64" >/dev/null
sleep 2
LOG_SHOW="$(sh_ logcat -d -s HubReplier:* | grep "SHOW ok id=$ID")"
[ -n "$LOG_SHOW" ] && ok "SHOW 成功: $LOG_SHOW" || bad "SHOW 未打印成功日志（adb logcat -s HubReplier:*）"

step "2. 断言通知结构（dumpsys notification --noredact）"
DUMP="$EVID/dumpsys-$ID.txt"
"$ADB" $D shell dumpsys notification --noredact >"$DUMP" 2>/dev/null
if python3 - "$DUMP" "$ID" <<'PY'
import re, sys
txt = open(sys.argv[1], encoding='utf-8', errors='replace').read()
ident = sys.argv[2]
recs = re.split(r'\n(?=\s+NotificationRecord\()', txt)
hit = [r for r in recs if ('hub_id=String (' + ident + ')') in r]
if not hit:
    print("  FAIL  没找到 hub_id=%s 的通知记录" % ident); sys.exit(1)
r = hit[0]
fails = []
if 'channel=hub_reply' not in r: fails.append('channel 不是 hub_reply')
if 'importance=4' not in r:      fails.append('importance 不是 4(HIGH)')
if 'category=msg' not in r:      fails.append('category 不是 msg')
acts = re.findall(r'\[\d+\] "([^"]*)" -> PendingIntent', r)
if not acts:                     fails.append('actions 为空')
elif '回复' not in acts:          fails.append('actions 里没有 RemoteInput 的「回复」: %s' % acts)
if fails:
    print("  FAIL  通知结构: " + "; ".join(fails)); sys.exit(1)
print("  PASS  channel=hub_reply importance=HIGH category=msg actions=%d %s" % (len(acts), acts))
PY
then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
grep -q "hub_id=String ($ID)" "$DUMP" && ok "extras 带 hub_id=$ID" || bad "extras 缺 hub_id"

step "3. TEST_REPLY（复用 RemoteInput 取结果的那段代码）→ 落盘"
TXT="test.sh 回执 $ID"
# extra 的值必须自带单引号：adb shell 会把参数拼成一条命令交给远端 sh 再分词
sh_ am start -n "$PKG/.HubActivity" --es op reply --es id "$ID" --es text "'$TXT'" >/dev/null
sleep 2
ROW="$(sh_ run-as "$PKG" cat files/replies/all.jsonl | grep "\"id\":\"$ID\"" | grep '"kind":"reply"' | tail -1)"
echo "  $ROW"
if echo "$ROW" | grep -q '"src":"test"' && echo "$ROW" | grep -q "$TXT"; then
  ok "TEST_REPLY 落盘 kind=reply src=test"
else bad "TEST_REPLY 未按预期落盘"; fi

step "4. op=action（选项按钮的脚本化等价入口）→ 落盘"
sh_ am start -n "$PKG/.HubActivity" --es op action --es id "$ID" --es option_key done >/dev/null
sleep 2
ROW="$(sh_ run-as "$PKG" cat files/replies/all.jsonl | grep "\"id\":\"$ID\"" | grep '"kind":"action"' | tail -1)"
echo "  $ROW"
if echo "$ROW" | grep -q '"action":"done"' && echo "$ROW" | grep -q '"src":"test"'; then
  ok "op=action 落盘 kind=action action=done src=test"
else bad "op=action 未按预期落盘"; fi

step "5. 通知更新为已回复 + 截图"
sh_ cmd statusbar expand-notifications >/dev/null; sleep 1.5
SHOT="$EVID/test-$ID-shade.png"
"$ADB" $D shell screencap -p /sdcard/hub_test.png >/dev/null 2>&1
"$ADB" $D pull /sdcard/hub_test.png "$SHOT" >/dev/null 2>&1
[ -s "$SHOT" ] && ok "截图 $SHOT" || bad "截图失败"
sh_ dumpsys notification --noredact | grep -q "hub:answered" \
  && ok "通知已被刷成已回复（hub:answered）" || bad "通知没更新成已回复"

if [ "$TAP" = "1" ]; then
  step "6. 真点选项按钮（HUB_TAP=1）"
  # 步骤 3/4 已经把它刷成「已回复」（按钮被移除），先按同 id 重投一次把按钮恢复
  sh_ am start -n "$PKG/.HubActivity" --es op show --es payload_b64 "$B64" >/dev/null
  sleep 2
  # MIUI 上 uiautomator dump 有时只吐一个根节点（动画没 idle），重试到拿到完整层级
  dump_ui() {
    for _ in 1 2 3 4; do
      sh_ rm -f /sdcard/u.xml
      sh_ uiautomator dump /sdcard/u.xml >/dev/null
      "$ADB" $D pull /sdcard/u.xml /tmp/hub_ui.xml >/dev/null 2>&1
      [ "$(grep -o '<node' /tmp/hub_ui.xml 2>/dev/null | wc -l | tr -d ' ')" -gt 5 ] && return 0
      sleep 0.8
    done
    return 1
  }
  tap_option() {
    dump_ui
    COORD=$(python3 - /tmp/hub_ui.xml "$OPT_DONE" <<'PY'
import re, sys
xml = open(sys.argv[1], encoding='utf-8').read()
ns = []
for m in re.finditer(r'<node[^>]*>', xml):
    s = m.group(0)
    def g(k):
        mm = re.search(k + r'="([^"]*)"', s); return mm.group(1) if mm else ''
    b = re.search(r'bounds="\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"', s)
    if not b: continue
    ns.append(dict(text=g('text'), desc=g('content-desc'),
                   x1=int(b.group(1)), y1=int(b.group(2)), x2=int(b.group(3)), y2=int(b.group(4))))
opt = [n for n in ns if n['text'] == sys.argv[2]]
if opt:
    print("OPT %d %d" % ((opt[0]['x1'] + opt[0]['x2']) // 2, (opt[0]['y1'] + opt[0]['y2']) // 2)); raise SystemExit
title = [n for n in ns if n['text'].startswith('自测 ')]
if not title: print(""); raise SystemExit
tc = (title[0]['y1'] + title[0]['y2']) // 2
ch = [n for n in ns if n['desc'] in ('展开', '收起')]
ch = [n for n in ch if abs(((n['y1'] + n['y2']) // 2) - tc) < 200]
if not ch: print(""); raise SystemExit
ch.sort(key=lambda n: abs(((n['y1'] + n['y2']) // 2) - tc))
c = ch[0]
print("CHEV %d %d" % ((c['x1'] + c['x2']) // 2, (c['y1'] + c['y2']) // 2))
PY
)
    echo "  ui: ${COORD:-未定位}"
    case "$COORD" in
      OPT*)  X=$(echo "$COORD" | cut -d' ' -f2); Y=$(echo "$COORD" | cut -d' ' -f3); sh_ input tap "$X" "$Y"; sleep 2 ;;
      CHEV*) X=$(echo "$COORD" | cut -d' ' -f2); Y=$(echo "$COORD" | cut -d' ' -f3); sh_ input tap "$X" "$Y"; sleep 1.2 ;;
      *)     echo "  SKIP  通知不在通知栏可见（可能被同一应用其它通知顶出屏幕）" ;;
    esac
  }
  # 展开↔收起会来回跳，用有界循环（最多 3 次）而不是递归
  i=0
  while [ $i -lt 5 ]; do
    i=$((i+1))
    tap_option
    ROW="$(sh_ run-as "$PKG" cat files/replies/all.jsonl | grep "\"id\":\"$ID\"" | grep '"src":"button"' | tail -1)"
    [ -n "$ROW" ] && break
    sleep 1
  done
  sleep 2
  ROW="$(sh_ run-as "$PKG" cat files/replies/all.jsonl | grep "\"id\":\"$ID\"" | grep '"src":"button"' | tail -1)"
  echo "  $ROW"
  if echo "$ROW" | grep -q '"action":"done"'; then
    ok "真点按钮落盘 kind=action action=done src=button"
  else bad "真点按钮未落盘（截图见 $EVID/test-$ID-tap.png）"; fi
  "$ADB" $D shell screencap -p /sdcard/hub_tap.png >/dev/null 2>&1
  "$ADB" $D pull /sdcard/hub_tap.png "$EVID/test-$ID-tap.png" >/dev/null 2>&1
fi

step "7. 汇总"
LINES_AFTER="$(jsonl_lines)"
echo "  all.jsonl: $LINES_BEFORE → $LINES_AFTER 行"
echo "  证据目录: $EVID"
echo "  id: $ID"
echo
echo "结果: $PASS 通过 / $FAIL 失败"
[ "$FAIL" -gt 0 ] && { echo "FAILED"; exit 1; }
echo "ALL PASS"
