# desktop-popup —— 电脑弹窗通道插件（S7）

> 本文件是该通道的**语义契约**（Lead 合并进 INTERFACE.md §10 的通道小节；本工位不改 INTERFACE.md）。
> 台账列语义见 INTERFACE.md §11，插件清单键见 §10，现行通知五道门与动作语义见 §4.6 / §5.5。

## 1. 清单

```yaml
name: desktop-popup
kind: channel
api: 1
entry: channel.py:send
default_enabled: true
config_key: notify
```

- **注册条件**（两者同时满足才可被调到）：`plugins.desktop-popup.enabled` 不为 `false`（缺省 = `default_enabled: true`）；
  本体 `notify.enabled=true`。`notify.enabled=false` 时 `notify.dispatch` 在门 1 就返回 `reason=disabled`，
  通道不会被调用 —— 即「不注册」。`plugins.desktop-popup.enabled=false` 或目录不存在 ⇒ 宿主清单里该通道
  `status=disabled` / 整行消失，`dispatch` 返回 `reason=no-channel`，退出码仍 `0`。
- 停用后**其它来源与通道照跑**：`hub-plugin list` 仍列出其余插件，`hub-collect` 照常。

## 2. 入口（单函数）

`channel.py:send(request: dict) -> dict`；`request["op"]` 取 `dispatch` / `act` / `listen`（缺省 `dispatch`）。

| 字段 | op | 说明 |
|---|---|---|
| `config` | 全部 | `infohub.config.Config` |
| `store` | 全部 | `infohub.store.Store` |
| `env` | dispatch | `infohub.notify.Env`（测试注入；缺省新建） |
| `title` | dispatch | 落台账的标题（本体传待办标题） |
| `summary` | dispatch | 系统通知标题（`notify.summary`，缺省 `info-hub`） |
| `body` / `kind` / `todo_id` | dispatch | 正文 / 类别 / 待办指针，原样落台账 |
| `dbus_id` / `action` / `text` / `reason` | act | 系统通知 id / 动作（标签或稳定键）/ 回复文本（仅 `reply`）/ 留痕原因 |
| `once` / `timeout_s` / `lines` / `max_lines` | listen | 与 `notify.listen` 同义；`lines` 供测试注入信号行 |

### 2.1 `op=dispatch`

0. **无待办不弹**（用户 2026-11-14 定）：`todo_id` 缺失、或该待办不存在、或状态不是 `open`/`doing` ⇒
   直接返回 `{ok:true, skipped:"no-todo", delivery_id:null}`，**不建台账行、不发通知**。
   由处理端决定这条消息要不要变成定时任务或即时消息；电脑弹窗不是「合成待办」的出口。
1. 建一行 `deliveries`：`channel=computer`、`mode=single`、`title/body/options`、`status=pending`；
   `options` 的每个 `key` **只取自** `deliver.ACTIONS`（按钮标签 → 稳定键的映射复用 `notify.CANON_ACTION`：
   打开→`open`、稍后→`snooze`、忽略此类→`ignore-kind`、完成→`acted`、忽略→`ignore`）。
2. 再调 `notify.send`（复用现行 gdbus 调用：`app_name` / `notify.summary` / `notify.actions` / `notify.timeout_ms`）。
3. 成功后回写 `remote_ref=<系统通知 id>`、`status=delivered`；失败回写 `status=failed` / `last_error` / `attempts+1`。

返回 `{ok, delivery_id, dbus_id, remote_ref, status, backend_reply}`；失败时 `{ok:false, error, delivery_id}`，
由本体照旧记一条 `notifications(shown=0)` 并让待办下一轮重试。

### 2.2 `op=act`（按钮 / 动作回调）

1. 按 `remote_ref == dbus_id` 找 `channel=computer` 的台账行；
2. **先写台账**：`reply` ⇒ `reply_delivery`（`status=replied`）；`ignore-kind` ⇒ `ack_delivery` 后置 `suppressed`；
   其余（`acted/open/snooze/ignore/ack`）⇒ `ack_delivery(action=<稳定键>)`（幂等）；
3. **再结算待办**（调用本体 `notify.settle_action`，即现行动作语义）：
   `acted`→`done`、`ignore`→`dropped`、`snooze`→`snooze_until`、`ignore-kind`→`kind_suppressions`、`open`→只留痕。
   `ack`/`reply` 不改待办状态。
4. 老数据没有对应台账行时只做第 3 步（向后兼容：通知行还在、投递行已清）。

返回值 = `notify.settle_action` 的形状 + `delivery`（台账行摘要）。

### 2.3 `op=listen`

起 `gdbus monitor`（`notify.MONITOR_ARGS`）、按 `notify.SIGNAL_RE` 解析；每个 `ActionInvoked` 走一次 `op=act`，
`NotificationClosed` 只计数；认不出的信号行忽略；找不到对应行记 `errors` 但不算失败。返回形状同 `notify.listen`。

## 3. 失败语义

| 情况 | 行为 |
|---|---|
| 通道没装 / 没启用 / 加载失败 | `dispatch` 返回 `reason=no-channel`，退出码 0；`listen` 返回 `stopped=no-channel` |
| `notify.enabled=false` | `dispatch` 返回 `reason=disabled`（通道不被调到） |
| gdbus 发送失败 | 台账行 `status=failed` + `last_error`；本体记 `notifications(shown=0)`；待办不标记已通知 |
| monitor 起不来 / 退出非 0 | `listen` 抛 `notify.ActionError`（`hub-notify listen` 退出码 1） |
| 动作找不到台账行 | 记 `errors`（listen）或按老路径结算（act）；不猜 |
| 空回复文本 | `reply_delivery` 前置拒绝，库不变 |

## 4. 已知限制（写给合并契约的人）

- **就地 UPDATE**：`store.py` 只有「建行 / ack / reply」三个写入口，没有「补 `remote_ref`」的通用更新方法，
  而 S7 的写作用域不含 `store.py`；因此「先建 pending 行、发送成功后补 `remote_ref/status`」这一步由
  `channel.py:_patch()` 直接 UPDATE（列白名单 `status/remote_ref/last_error/attempts/updated_at`）。
  下一步若给 `store.py` 加 `set_delivery_ref()`，本文件改为调它即可（语义不变）。
- **停用仍出现在清单里**：宿主 §10 规定 `enabled=false` 的行显示 `status=disabled`（不是消失）。
  「`hub-plugin list` 里没有该通道」只在**删目录**时字面成立；配置停用时是 `disabled` 行。
- 系统通知的按钮只能回传**标签**（D-Bus `ActionInvoked` 的第二个参数），不是稳定键；映射表在
  `notify.CANON_ACTION`，改按钮文案要同时改那里。
- 自由文本回复（`reply`）无法从 GNOME 通知输入，只能由调用方（以后的 layout/控制台）带 `text` 调 `op=act`。
