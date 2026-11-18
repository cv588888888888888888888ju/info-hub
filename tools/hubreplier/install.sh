#!/usr/bin/env bash
# 安装到手机并确认生效。契约见 README.md §5。
# 小米/HyperOS 的坑：shell 发起的安装会被 com.miui.securitycenter 的
# 「USB安装提示」对话框拦一次（默认 6 秒倒计时后自动「拒绝」），
# 所以这里在 install 期间轮询 UI 并点「继续安装」。可用 HUB_AUTO_CONFIRM=0 关掉。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

ADB="${ADB:-adb}"
DEVICE="${HUB_DEVICE:-<serial>}"
APK="${APK:-$HERE/hubreplier-0.1.0-debug.apk}"
PKG="com.chenty.hubreplier"
AUTO_CONFIRM="${HUB_AUTO_CONFIRM:-1}"
TMP="${TMPDIR:-/tmp}/hubreplier-install"
LOG="${HUB_EVIDENCE:-~/hub-tasks/runs/core-deliver-plugins/evidence/hubreplier}/install-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "$TMP" "$(dirname "$LOG")"
D="-s $DEVICE"

if [ ! -f "$APK" ]; then
  echo "==> 没有 APK，先构建"
  ./build.sh || exit 1
fi

echo "==> 设备 $DEVICE"
"$ADB" $D get-state || exit 1

echo "==> adb reverse（回执 HTTP 通道；5555 是 jxev 自带的，不动）"
"$ADB" $D reverse tcp:8799 tcp:8799 || echo "WARN: reverse 8799 失败（回执会退化为仅本地落盘）"

# 解析 UI 里「继续安装」按钮的中心坐标（找不到就退回本机实测的固定坐标）
find_continue_button() {
  "$ADB" $D shell uiautomator dump /sdcard/hub_ui.xml >/dev/null 2>&1
  "$ADB" $D pull /sdcard/hub_ui.xml "$TMP/ui.xml" >/dev/null 2>&1
  python3 - "$TMP/ui.xml" <<'PY' 2>/dev/null
import re, sys
try:
    xml = open(sys.argv[1], encoding='utf-8').read()
except Exception:
    sys.exit(1)
want = ('继续安装', '安装', '确定', '允许')
for m in re.finditer(r'<node[^>]*>', xml):
    s = m.group(0)
    t = re.search(r' text="([^"]*)"', s)
    b = re.search(r'bounds="\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"', s)
    if not (t and b):
        continue
    if t.group(1) in want:
        x1, y1, x2, y2 = (int(b.group(i)) for i in (1, 2, 3, 4))
        print((x1 + x2) // 2, (y1 + y2) // 2)
        break
PY
}

echo "==> adb install -r -g --no-incremental"
( "$ADB" $D install -r -g --no-incremental "$APK" > "$LOG" 2>&1; echo $? > "$TMP/rc" ) &
IPID=$!
TAPPED=0
for _ in $(seq 1 60); do
  sleep 0.3
  kill -0 $IPID 2>/dev/null || break
  if [ "$AUTO_CONFIRM" = "1" ] && [ "$TAPPED" = "0" ]; then
    if "$ADB" $D shell dumpsys window 2>/dev/null | grep -q "AdbInstallActivity"; then
      COORDS="$(find_continue_button)"
      if [ -z "$COORDS" ]; then
        COORDS="311 2232"   # 本机 wm size 1080x2400 override 下实测的「继续安装」
        echo "    未解析到按钮，用固定坐标: $COORDS"
      fi
      echo "    点击「继续安装」: $COORDS"
      # shellcheck disable=SC2086
      "$ADB" $D shell input tap $COORDS
      TAPPED=1
    fi
  fi
done
wait $IPID
RC="$(cat "$TMP/rc" 2>/dev/null || echo 1)"
tail -5 "$LOG"
if [ "$RC" != "0" ]; then
  echo "FATAL: adb install 失败（rc=$RC）。若是 INSTALL_FAILED_USER_RESTRICTED，"
  echo "       请手动在手机上点「继续安装」，或检查开发者选项 → USB安装。"
  exit 1
fi

echo "==> 校验"
"$ADB" $D shell pm list packages | grep -q "$PKG" || { echo "FATAL: 包未安装"; exit 1; }

# 关键：Android 3.1+ 新装应用 stopped=true/notLaunched=true，广播一律被丢。
# 起一次 MainActivity（它立刻 finish）把包变成「用户可见地启动过」，stopped 才会清掉。
echo "==> 解除 stopped（am start MainActivity 一次）"
"$ADB" $D shell am start -W -n "$PKG/.MainActivity" 2>&1 | grep -E "Status|Activity|Error" | head -3
sleep 1
# 注意：这里绝不能 am force-stop —— force-stop 会把 stopped 置回 true，广播又被丢

# 关键（实测）：小米 HyperOS 的 Greezer 会拒绝把广播投给 cached 进程
#   （logcat: "Greezer Denial ... need cached broadcast" / "process is not permitted to auto start"），
# 而系统绑定的 NotificationListenerService 让进程持有服务级 adj，广播就能收到。
# 本 App 的 listener 不读、不存任何通知内容，只作为常驻锚点。
echo "==> 授予通知使用权（常驻锚点，避免 MIUI Greezer 丢广播）"
"$ADB" $D shell cmd notification allow_listener "$PKG/$PKG.KeepaliveListener" 2>&1 | head -2
sleep 1
"$ADB" $D shell dumpsys notification 2>/dev/null | grep -A2 "com.chenty.hubreplier" | grep -i "listener\|connected" | head -3 || true

echo "==> 权限"
"$ADB" $D shell pm grant "$PKG" android.permission.POST_NOTIFICATIONS 2>/dev/null || true
"$ADB" $D shell dumpsys package "$PKG" | grep -E "stopped=|POST_NOTIFICATIONS: granted|INTERNET: granted" | head -4
echo "==> adb reverse 现状（应含 tcp:8799，及 jxev 原有的 tcp:5555）"
"$ADB" $D reverse --list
echo "OK: $PKG 已安装"
