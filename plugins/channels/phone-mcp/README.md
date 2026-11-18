# phone-mcp 通道插件（S9）

手机通道：把一条投递推到手机 MCP（`hub_deliver`），手机回执走投递 HTTP（§11.5），台账共一本。
契约本体在 `INTERFACE.md §11.7` 与 §13（layout v1）；本文件只写这个插件的**入口、键表与失败语义**。

## 1. 清单

```yaml
name: phone-mcp
kind: channel
api: 1
entry: channel.py:send
default_enabled: false      # 装了手机 MCP 才开：plugins.phone-mcp.enabled=true
config_key: phone_remind
```

依赖的手机 MCP 工具（S8 在 `root@<bridge-host>:/opt/mcp-bridge/ai_mcp_server/bridge_mcp.py` 加）：

- `hub_deliver(delivery_id, mode, title, body, options, layout, timeout_s, text)` —— `text` 是 `body` 的别名（旧调用方兼容）；
- `hub_dismiss(delivery_id)`。

## 2. 入口 `send(request) -> dict`

| 键 | 必填 | 说明 |
|---|---|---|
| `op` | 否 | `dispatch`（缺省）/ `retry` / `dismiss` |
| `config`、`store` | 是 | 本体注入（`infohub.config` 的 dict、`infohub.store.Store`） |
| `title` | dispatch 时是 | 卡片标题，空 ⇒ `ChannelError` |
| `body` | 否 | 正文；无 layout 时作为 `text` 下发 |
| `mode` | 否 | `single`（缺省）/ `status` / `question`，非法 ⇒ `ChannelError` |
| `options` / `layout` | 否 | JSON 数组 / 对象；layout 非 v=1 视同没给（回落 title+text） |
| `todo_id` / `kind` | 否 | 落台账行 |
| `delivery_id` | retry/dismiss 必填 | 目标行 / 卡片 |

## 3. 语义

- **dispatch**：生成 `d-<时间>-<6hex>` → `hub_deliver` → 成功才 `deliveries` 建行（`channel=phone`、`status=delivered`、`remote_ref=返回 ref`）。失败抛 `PushError`（CLI 退出 1），**不建行**。
- **retry**：只对已有 pending 行；失败写 `attempts+1`/`last_error` 后仍抛错（行留 pending）。退避 `60/120/240/600s`，`bin/hub-phone-push --pending` 按 `updated_at + backoff(attempts)` 挑到点的行。
- **dismiss**：调 `hub_dismiss`；找不到卡片手机侧返回 `dismissed:0`，通道视为成功。
- **ack/reply**：不在这里做——手机回执由手机侧调 §11.5 的 `/api/deliver/ack|reply`（或 `hub-deliver ack|reply`），本通道不解释回执内容。

## 4. 命令

```bash
bin/hub-phone-push --title "交材料" --body "明天 10 点" --mode question \
  --options '[{"key":"ack","label":"知道了"}]' --layout '{"v":1}' --todo-id t-...
bin/hub-phone-push --pending --json      # 重推到点的 pending 行
bin/hub-phone-push --dismiss d-2026...   # 收回卡片
bin/hub-phone-push --strong --title "看这里" --body "锁屏全屏"   # 强提醒（bridge reminder_show）
```

`--strong` 语义（2026-11-18 加）：只发**手机腿**的纯强提醒 —— 建一行 `channel=phone` 的投递（`transport=reminder`，手机侧可见），
再调 bridge 的 `device_action/reminder_show`（`fullscreen=true`）立刻全屏；执行者是手机侧 MCP App（com.jxev.ai），**需要它的无障碍服务保活**。
它不支持在手机上打字回复 —— 要两端强推送且可回复，用 `bin/hub-strong-push`（见 INTERFACE.md §11.7c）。

退出码：`0` 成功；`1` 手机不可达/拒绝、通道未启用；`2` 参数或配置/库错误。

## 5. 配置

| 键 | 缺省 | 说明 |
|---|---|---|
| `plugins.phone-mcp.enabled` | manifest 的 `false` | 开了才会被 `hub-plugin list` 标 ok |
| `phone_remind.bridge` | `http://<bridge-host>:9500/mcp` | MCP streamable-http 端点 |
| `phone_remind.timeout_s` | `30` | 单次 MCP 调用超时 |
| `phone_remind.deliver_tool` / `dismiss_tool` | `hub_deliver` / `hub_dismiss` | 替身/改名时覆盖 |
| `phone_remind.backend` | `bridge` | `bridge`（jxev App）/ `helper`（自制可回复通知 APK）/ `auto`（helper 可用则用 helper，否则 bridge） |
| `phone_remind.adb` | `adb` | helper 后端用的 adb 可执行文件（PATH 或绝对路径） |
| `phone_remind.device` | 空 | helper 后端必填：设备序列号（`adb devices` 第二列） |
| `phone_remind.adb_timeout_s` | `15` | 单次 adb 调用超时 |
| `phone_remind.helper_pkg` | `com.chenty.hubreplier` | helper 包名（装机自检用） |
| `phone_remind.helper_transport` | `activity` | `activity`（`am start`；HyperOS/MIUI 的 Greezer 会丢 adb 广播，只有这条实测可靠）或 `broadcast`（换 ROM 兜底） |
| `phone_remind.helper_activity` | `com.chenty.hubreplier/.HubActivity` | helper 入口 Activity（`op=show` / `op=dismiss`） |
| `phone_remind.helper_show` | `com.chenty.hubreplier/.ShowReceiver` | broadcast 模式下的 SHOW 组件 |
| `phone_remind.helper_dismiss` | `com.chenty.hubreplier/.DismissReceiver` | broadcast 模式下的 DISMISS 组件 |
| `phone_remind.helper_token` | 空 | SHOW 里带的防误触 token（helper 侧比对） |

## 6. helper 后端（可回复通知）

场景：jxev App 只能显示只读卡片（APK 里 `KEY_TEXT_REPLY` 出现 0 次），而用户要"在通知栏直接回复/点选项"。
用法：`phone_remind.backend=helper` 时，通道不再走 MCP，而是：

```
adb -s <device> shell am start -n com.chenty.hubreplier/.HubActivity --es op show --es payload_b64 <base64(json)>
adb -s <device> shell am start -n com.chenty.hubreplier/.HubActivity --es op dismiss --es id <delivery_id>
```

- `payload_b64` = **整条 JSON 的 base64**（避开设备端再套一层 shell 引号）：`{id,mode,title,body,options,layout,token}`；
- 为什么不用广播：HyperOS 上 `adb shell am broadcast` 被 Greezer 直接丢弃（logcat `Greezer Denial … process is not permitted to auto start`），
  `am start` 实测可到；`helper_transport=broadcast` 仅作换 ROM 兜底；
- helper 侧发一条带 `RemoteInput` 的通知；同 id 重复 SHOW ⇒ 替换旧通知（不堆叠）；
- 收回：`adb ... am broadcast -n <helper_dismiss> --es id <delivery_id>`，幂等。

失败语义：adb 不在 / 设备离线 / helper 未安装 / 广播返回 `Broadcast completed: result=0` ⇒ `PushError`（CLI 退出 1，不建行）。

回执：helper **自己** POST `http://127.0.0.1:8799/api/deliver/reply`，body `{"id":<delivery_id>,"text":<答案>}`；
手机视角的 127.0.0.1 靠宿主机 `adb reverse tcp:8799 tcp:8799` 打到 info-hub 的 loopback（宿主机安装脚本负责建这条 reverse）。
hub 不可达时 helper 落盘 `files/replies/<id>.json` 兜底。通道不解释回执内容（同 §3）。

## 7. 验收

`tests/check_phone_mcp.sh`（19 项，`tests/stubs/mcp_stub.py` 替身，不碰真手机）：layout 透传与回落、
失败退出 1 且不写库、ack 后 `status=acked` 且待办 status 不变、schema 可解析、pending 重推、dismiss。
