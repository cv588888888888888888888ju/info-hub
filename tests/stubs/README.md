# info-hub/tests/stubs — 可机械跑的替身（W3）

> 给 S5–S12 用的测试替身：不弹真通知、不连真手机、不起 DSH、不读写真库与真配置。
> 只依赖 bash / python3 标准库 / curl；替身本身不 import infohub。
> 写作用域：本目录与 `plugins/channels/phone-mcp/layout.schema.json`（W3 工位）。

| 文件 | 作用 |
|---|---|
| `gdbus_stub.sh` | 假 gdbus：Notify 回 `(uint32 <id>,)`、GetCapabilities、Mutter GetIdletime、monitor 吐动作信号行 |
| `bridge_stub.py` | 假手机 MCP 服务器（streamable-http）：initialize / tools/list / tools/call；tools/call 的 params 落 JSONL，可按回放文件回预设回执 |
| `selftest.sh` | 两个替身的自测；全部断言过才退出 0 |
| `README.md` | 本文件 |

## 1. gdbus_stub.sh

模拟 info-hub 实际用到的 gdbus 调用（方法名按**后缀**匹配，其余方法一律回 `()` 且退出 0）：

| 调用 | 替身输出 |
|---|---|
| `...Notifications.Notify` | `(uint32 <id>,)` |
| `...Notifications.GetCapabilities` | `(['actions', 'body', 'icon-static'],)` |
| `...Notifications.GetServerInformation` | `('gdbus_stub', 'info-hub', '1.0', '1.2')` |
| `...Mutter.IdleMonitor.GetIdletime` | `(uint64 <ms>,)` |
| `...Notifications.CloseNotification` / 未知方法 | `()` |

### 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `GDBUS_STUB_ID` | 77 | Notify 返回的通知 id |
| `GDBUS_STUB_ID_FILE` | 空 | 给文件路径 ⇒ 每次 Notify 自增（文件存上次 id，首个 id = GDBUS_STUB_ID） |
| `GDBUS_STUB_CALL_FAIL` | 空 | 非空 ⇒ Notify 退出 1（模拟发送失败） |
| `GDBUS_STUB_CALL_LOG` | 空 | 每次调用的 argv 追加一行（断言按钮/正文真的传了） |
| `GDBUS_STUB_CAPABILITIES` | actions,body,icon-static | GetCapabilities 内容（逗号分隔裸名字）；空或 none ⇒ `()` |
| `GDBUS_STUB_IDLE_MS` | 0 | Mutter GetIdletime 的毫秒数 |
| `GDBUS_STUB_ACTIONS` | 空 | monitor 依次吐的动作键（逗号/空格/换行分隔），原样进 ActionInvoked 的第二个参数 |
| `GDBUS_STUB_MONITOR_ID` | GDBUS_STUB_ID | 信号行里的通知 id |
| `GDBUS_STUB_MONITOR` | 空 | 原始信号体文件，每行一条（如 `ActionInvoked(uint32 79, 'acted')`），与旧 `tests/gdbus_stub.py` 兼容 |
| `GDBUS_STUB_CLOSE` | 空 | 非空 ⇒ 额外吐 `NotificationClosed(uint32 <id>, 2)` |
| `GDBUS_STUB_MONITOR_FAIL` | 空 | 非空 ⇒ monitor 退出 1（模拟 gdbus 起不来） |
| `GDBUS_STUB_HOLD` | 0.3 | 吐完信号后常驻秒数（monitor 是常驻进程，不能吐完立刻死） |

### 用法

直接调：

    STUBS=$HUB_ROOT/tests/stubs
    GDBUS_STUB_ID=70 bash $STUBS/gdbus_stub.sh call --session \
      --dest org.freedesktop.Notifications \
      --object-path /org/freedesktop/Notifications \
      --method org.freedesktop.Notifications.Notify \
      info-hub 0 "" 标题 正文 '["打开", "稍后"]' '{}' 8000
    # -> (uint32 70,)

当假 gdbus 塞进 PATH（notify.py 就是这么被喂的）：

    mkdir -p $TMP/fakebin
    ln -sf $STUBS/gdbus_stub.sh $TMP/fakebin/gdbus
    GDBUS_STUB_ACTIONS="acted snooze" GDBUS_STUB_HOLD=0.2 \
      PATH="$TMP/fakebin:$PATH" python3 -m infohub.entry_notify listen --once

### 与 notify.py 的解析口径（别改格式）

- `GDBUS_ID_RE` 要的是 `(uint32 70,)` 这个形状；
- `SIGNAL_RE` 要的是 `The signal org.freedesktop.Notifications.ActionInvoked(uint32 70, 'acted') received`；
- `dispatch` 发送前会先调 GetCapabilities，替身默认带 `actions` ⇒ 按钮真的会进 Notify 参数；想测「无按钮」分支就把 `GDBUS_STUB_CAPABILITIES=none`。

### 已知限制

- 不是 D-Bus：没有 session bus、没有真信号订阅；monitor 是脚本按剧本吐行，吐完按 `GDBUS_STUB_HOLD` 常驻后退出。
- 只按方法名后缀分派，不校验参数个数/类型；要断言入参只能读 `GDBUS_STUB_CALL_LOG`。
- 不模拟通知超时/自动关闭；NotificationClosed 只在 `GDBUS_STUB_CLOSE` 时吐。
- 并发发送时 id 想自增必须给 `GDBUS_STUB_ID_FILE` 一个独立文件；否则每次都是同一个 id。
- 不支持 `gdbus call --timeout` / `--address` 的语义（只吃掉参数，不改变行为）。

## 2. bridge_stub.py

最小 MCP streamable-http 服务器（纯标准库 `http.server`）。协议形状对齐 `phone_remind.bridge_call`：
initialize（从响应头取 Mcp-Session-Id）→ notifications/initialized（202 空体）→ tools/call（回执在 `result.content[0].text` 里，是一段 JSON 字符串）。

### 启动

    BRIDGE_STUB_LOG=/tmp/calls.jsonl python3 tests/stubs/bridge_stub.py
    # BRIDGE_STUB_LISTENING http://127.0.0.1:41234/mcp
    # BRIDGE_STUB_LOG /tmp/calls.jsonl

端口默认 0（内核分配）避免撞端口；要固定端口设 `BRIDGE_STUB_PORT`。启动行照抄 URL 即可。

### 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `BRIDGE_STUB_PORT` | 0 | 监听端口；0 = 内核分配 |
| `BRIDGE_STUB_HOST` | 127.0.0.1 | 绑定地址；非 loopback 需同时设 `BRIDGE_STUB_ALLOW_REMOTE=1`，否则退出 2 |
| `BRIDGE_STUB_LOG` | 空 | tools/call 的 JSONL 路径；空 = 不记录 |
| `BRIDGE_STUB_REPLAY` | 空 | 回放文件（形状见下） |
| `BRIDGE_STUB_TOOLS` | hub_deliver,hub_dismiss,device_action | tools/list 里出现的工具名 |
| `BRIDGE_STUB_SSE` | 空 | 非空 ⇒ 回 SSE（`event: message` + `data: {...}`）；默认裸 JSON |
| `BRIDGE_STUB_FAIL` | 空 | `http500` / `http400` / `jsonrpc` ⇒ 强制失败（测退避与「不写库」） |
| `BRIDGE_STUB_SESSION_ID` | stub-session-1 | 响应头 Mcp-Session-Id；设成空串 ⇒ 不发这个头 |
| `BRIDGE_STUB_PORT_FILE` | 空 | 启动后把实际端口写进该文件 |
| `BRIDGE_STUB_VERBOSE` | 空 | 非空 ⇒ 每个请求打一行到 stderr |

### 端点

- `POST /mcp`：`initialize`、`notifications/initialized`（202 空体）、`tools/list`、`tools/call`、`ping`。
- `GET /healthz`：200 `ok`（等启动用）；其他 GET 回 405。

### JSONL 一行样例（只有 tools/call 落盘）

    {"ts": "2026-11-14T10:51:23+08:00", "seq": 1, "method": "tools/call", "session": "stub-session-1", "name": "hub_deliver", "params": {"name": "hub_deliver", "arguments": {"delivery_id": "d-1", "mode": "single", "title": "标题", "body": "正文", "options": [{"key": "ack", "label": "知道了"}], "layout": {"v": 1}}}, "arguments": {"delivery_id": "d-1", "mode": "single", "title": "标题", "body": "正文", "options": [{"key": "ack", "label": "知道了"}], "layout": {"v": 1}}}

- 每行一条，字段：`ts / seq / method / session / name / params / arguments`；`arguments` 是 `params.arguments` 的副本，方便直接 grep。
- initialize / tools/list **不**落盘（判据「JSONL 恰好一行」就是靠这个）。

### 回执

- 默认按工具名编「成功」回执：`hub_deliver` → `{"ok": true, "delivery_id": …, "mode": …, "shown": true}`；`hub_dismiss` → `{"ok": true, "dismissed": …}`；其余 → `{"ok": true, "data": {"success": true, "shown": true}}`（兼容 `phone_remind` 的 `data.shown` 判定）。
- 回放文件三种形状（可组合）：
  - `{"hub_deliver": {…回执…}}`：按工具名固定回执；
  - `{"hub_deliver": [{…}, {…}]}`：同工具按序消费（每消费一条就少一条，只在进程内存里）；
  - `[{…}, {…}]`：任意工具按序消费；
  - 特殊键 `"__default__"`：没匹配到工具名时的兜底。
- 回执对象原样 json.dumps 进 `result.content[0].text`；对象里带 `"isError": true` 时同时把 `result.isError` 置 true。

### 配 phone_remind 用

    # 1) 隔离配置：把 phone_remind.bridge 指到替身 URL
    #    （从 info-hub/hub.config.yaml 复制，root 改成临时目录）
    # 2) 直接调 client
    python3 - <<'PY'
    import os, sys
    sys.path.insert(0, "info-hub/src")
    os.environ["HUB_CONFIG"] = "/tmp/c.yaml"
    from infohub import config, phone_remind
    cfg = config.load()
    print(phone_remind.bridge_call(cfg, "hub_deliver", {"delivery_id": "d-1", "mode": "single"}))
    PY

### 已知限制

- **不校验 Mcp-Session-Id**：缺失/任意值都接受。它只保证 client 流程跑通，不能用来测会话归属、越权、会话过期。
- 没有 GET 流：SSE 只在 POST 响应里回一条；不支持服务器主动通知、断线续传、resumability。
- tools/list 的 `inputSchema` 是占位（`{"type": "object", "additionalProperties": true}`），不是真工具 schema；工具行为只能靠回放模拟，不校验参数合法性。
- 回放队列只在进程内存里消费，重启即重置；并发 tools/call 的消费顺序不保证。
- 单进程线程模型，无鉴权/无 TLS；默认只绑 loopback，别拿它暴露到网络。
- 所有 `BRIDGE_STUB_*` 都在**启动时**读取：运行中改环境变量无效；要测失败分支请另起一个实例（`BRIDGE_STUB_FAIL=http500`）。

## 3. selftest.sh

    bash info-hub/tests/stubs/selftest.sh; echo $?

覆盖 29 条断言，任一失败退出 1：

- bridge：启动、initialize 200 + Mcp-Session-Id 头 + serverInfo、notifications/initialized 202、tools/list 含三个工具、tools/call 200 且回执可解析、JSONL 恰好一行且含 delivery_id/mode/options/layout、回放回执、SSE 回包、强制 500；
- gdbus：Notify 退出码与 `(uint32 70,)` 形状、DBUS_ID_RE 可解析、CALL_LOG 记录入参、GetCapabilities 形状（含空能力表）、Notify 失败退出码、monitor 两条动作信号 + SIGNAL_RE 可解析、NotificationClosed、monitor 失败退出码。

临时目录用 `mktemp -d`，退出时 `/usr/bin/rm -rf` 清掉；不碰真库、真配置、真手机。

## 4. layout.schema.json

位置：`info-hub/plugins/channels/phone-mcp/layout.schema.json`（JSON Schema 2020-12）。契约正文：INTERFACE.md §13。

| 键 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `v` | 是 | `const 1` | 版本；不认识 ⇒ 手机侧当整份无效，回落标题+正文 |
| `title` / `body` | 否 | string | 缺省用投递行的 title/body |
| `options` | 否 | array of {key,label,description} | 元素只强制 `key`（稳定键，回执原样回传）；`label` 只显示不落库 |
| `input` | 否 | object {enabled,placeholder} | `enabled=false` ⇒ 不画输入框 |
| `actions` | 否 | array of {key,label,style} | `style` 取 `primary` / `default` / `destructive` |
| `expires_at` | 否 | string (date-time) | ISO 8601 带时区 |

- `additionalProperties: true`（顶层与每个 `$defs` 元素都是）：契约要求「未知键一律忽略」，schema 不能把前向兼容的字段判成错。
- `format: date-time` 在 JSON Schema 里默认只是注解，要校验得显式开 format 检查（本机没装 jsonschema，也没为它加依赖）。
- 本仓库对它的机械检查只有 `python3 -c "import json; json.load(...)"` + 结构断言（键齐、`v.const == 1`、各层 `additionalProperties` 为 true）。真校验在手机端/后续步骤。
- `options[].key` / `actions[].key` **故意不做 enum**（前向兼容）；`style` 做 enum 是因为 §13 把三种取值写死了，新增风格要先改契约。

## 5. 判据对照

| 判据 | 用哪个替身 |
|---|---|
| A9 电脑通道（dispatch 成功、act 后 acked、停用通道） | gdbus_stub.sh（PATH 注入 + GDBUS_STUB_ACTIONS 喂动作） |
| A10 手机通道（推送后有 channel=phone 行、替身收到的 JSON 含 delivery_id、bridge 失败退出 1） | bridge_stub.py（JSONL + BRIDGE_STUB_FAIL=http500/http400） |
| A17 界面 JSON 透传（layout.v=1、options[*].key、layout=NULL 时只有 title+text） | bridge_stub.py 的 JSONL |
| A22 三种模式（single/status/question、同 id 两次 status、非法 mode） | bridge_stub.py 的 JSONL + 回放回执 |
