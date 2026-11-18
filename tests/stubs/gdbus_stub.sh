#!/usr/bin/env bash
# 假 gdbus（W3 替身）：给 info-hub 的 notify.py 一个可机械复现的通知后端。
#
# 只模拟四件事（其余方法一律回 () 且退出 0）：
#   gdbus call --session --dest org.freedesktop.Notifications ... --method ...Notify          -> (uint32 <id>,)
#   gdbus call --session --dest org.freedesktop.Notifications ... --method ...GetCapabilities -> (['actions', ...],)
#   gdbus call --session --dest org.gnome.Mutter.IdleMonitor  ... --method ...GetIdletime     -> (uint64 <ms>,)
#   gdbus monitor --session --dest org.freedesktop.Notifications                              -> 信号行（ActionInvoked / NotificationClosed）
#
# 解析口径以 info-hub/src/infohub/notify.py 为准：
#   DBUS_ID_RE  ^\(uint32\s+(\d+)\s*,?\)$
#   SIGNAL_RE   (ActionInvoked|NotificationClosed)\(uint32\s+(\d+)(?:,\s*'?([^')]*)'?)?\)
# 环境变量与已知限制见同目录 README.md。用法示例：
#   PATH="info-hub/tests/stubs:$PATH" gdbus call --session --dest org.freedesktop.Notifications \
#     --object-path /org/freedesktop/Notifications \
#     --method org.freedesktop.Notifications.Notify info-hub 0 "" 标题 正文 '["打开"]' '{}' 8000
set -u

log_call() {
  [ -n "${GDBUS_STUB_CALL_LOG:-}" ] || return 0
  printf '%s\n' "$*" >>"${GDBUS_STUB_CALL_LOG}"
}

next_id() {
  # 固定 id（GDBUS_STUB_ID），或按 GDBUS_STUB_ID_FILE 自增（文件缺失时先用 base）
  local base="${GDBUS_STUB_ID:-77}" file="${GDBUS_STUB_ID_FILE:-}" n prev
  if [ -z "$file" ]; then
    printf '%s' "$base"
    return 0
  fi
  n="$base"
  if [ -s "$file" ]; then
    prev="$(tr -dc '0-9' <"$file")"
    [ -n "$prev" ] && n=$((prev + 1))
  fi
  printf '%s' "$n" >"$file"
  printf '%s' "$n"
}

print_capabilities() {
  # GDBUS_STUB_CAPABILITIES 是逗号分隔的裸名字；空串 / none => ()
  local raw="${GDBUS_STUB_CAPABILITIES-actions,body,icon-static}" out="" item
  case "$raw" in
    "" | none | None | NONE) printf '()\n'; return 0 ;;
  esac
  local IFS=','
  for item in $raw; do
    item="${item//[[:space:]]/}"
    [ -n "$item" ] && out="$out${out:+, }'$item'"
  done
  printf "([%s],)\n" "$out"
}

emit_signal() {
  printf 'The signal org.freedesktop.Notifications.%s received\n' "$1"
  sleep 0.02
}

cmd_monitor() {
  if [ -n "${GDBUS_STUB_MONITOR_FAIL:-}" ]; then
    printf 'gdbus_stub: monitor 起不来（GDBUS_STUB_MONITOR_FAIL）\n' >&2
    return 1
  fi
  printf 'Monitoring signals on object /org/freedesktop/Notifications owned by org.freedesktop.Notifications\n'
  printf 'The name org.freedesktop.Notifications is owned by org.freedesktop.Notifications\n'

  local id="${GDBUS_STUB_MONITOR_ID:-${GDBUS_STUB_ID:-77}}"
  local actions="${GDBUS_STUB_ACTIONS:-}" one
  local IFS=','
  if [ -n "$actions" ]; then
    # 空格 / 换行也当分隔符，方便 GDBUS_STUB_ACTIONS="acted snooze"
    actions="$(printf '%s' "$actions" | tr -s '[:space:]' ',')"
    for one in $actions; do
      one="${one//[[:space:]]/}"
      [ -n "$one" ] || continue
      emit_signal "ActionInvoked(uint32 $id, '$one')"
    done
  fi

  # 原始信号体文件（每行一个，如 ActionInvoked(uint32 79, 'acted')），与旧 tests/gdbus_stub.py 兼容
  if [ -n "${GDBUS_STUB_MONITOR:-}" ] && [ -f "${GDBUS_STUB_MONITOR}" ]; then
    while IFS= read -r one; do
      one="$(printf '%s' "$one" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      [ -n "$one" ] || continue
      emit_signal "$one"
    done <"${GDBUS_STUB_MONITOR}"
  fi

  if [ -n "${GDBUS_STUB_CLOSE:-}" ]; then
    emit_signal "NotificationClosed(uint32 $id, 2)"
  fi

  sleep "${GDBUS_STUB_HOLD:-0.3}"
  return 0
}

cmd_call() {
  local dest="" method=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dest) dest="${2-}"; shift 2 || shift ;;
      --method) method="${2-}"; shift 2 || shift ;;
      --object-path | --address | --timeout | -t) shift 2 || shift ;;
      *) shift ;;
    esac
  done

  case "$method" in
    *".Notify")
      if [ -n "${GDBUS_STUB_CALL_FAIL:-}" ]; then
        printf 'gdbus_stub: Notify 失败（GDBUS_STUB_CALL_FAIL）\n' >&2
        return 1
      fi
      printf '(uint32 %s,)\n' "$(next_id)"
      return 0
      ;;
    *".GetCapabilities")
      print_capabilities
      return 0
      ;;
    *".GetIdletime")
      printf '(uint64 %s,)\n' "${GDBUS_STUB_IDLE_MS:-0}"
      return 0
      ;;
    *".GetServerInformation")
      printf "('gdbus_stub', 'info-hub', '1.0', '1.2')\n"
      return 0
      ;;
    *)
      # CloseNotification / 未知方法：真 gdbus 回 () 或报错；替身统一给空元组，避免把无关调用变成硬失败
      printf '()\n'
      return 0
      ;;
  esac
}

main() {
  log_call "$@"
  local mode="${1:-}"
  [ "$#" -gt 0 ] && shift

  case "$mode" in
    monitor) cmd_monitor "$@" ;;
    call) cmd_call "$@" ;;
    *)
      printf "gdbus_stub: 只模拟 'call' 与 'monitor'（收到: %s）\n" "${mode:-空}" >&2
      return 2
      ;;
  esac
}

main "$@"
exit $?
