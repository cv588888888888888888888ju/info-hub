#!/usr/bin/env bash
# 假 adb：把调用记进 $ADB_FAKE_LOG，并模拟几种返回。仅测试用（check_phone_helper / check_phone_replies）。
LOG="${ADB_FAKE_LOG:-/dev/null}"
printf '%s\n' "$*" >> "$LOG"
args="$*"
case "$args" in
  *"pm list packages"*)
    if [ "${ADB_FAKE_INSTALLED:-0}" = "1" ]; then echo "package:com.chenty.hubreplier"; fi
    ;;
  *"am start"*)
    echo "Starting: Intent { cmp=com.chenty.hubreplier/.HubActivity }"
    echo "Status: ok"
    ;;
  *"am broadcast"*)
    echo "Broadcasting: Intent { act=com.chenty.hubreplier.SHOW }"
    echo "Broadcast completed: result=0"
    ;;
  *"exec-out run-as"*)
    f="${ADB_FAKE_REPLIES:-}"
    if [ -z "$f" ] || [ ! -f "$f" ]; then
      echo "cat: files/replies/all.jsonl: No such file or directory" >&2
      exit 1
    fi
    cat "$f"
    ;;
  *) echo "unexpected adb call: $args" >&2; exit 3 ;;
esac
