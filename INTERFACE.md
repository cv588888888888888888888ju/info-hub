# info-hub 接口契约（INTERFACE.md）

> 期号 / 日期见 [PROJECT.md](PROJECT.md) 抬头（`pm-render.py` 自动生成）；本文不另立版本号，只跟期号走。

- 设计依据：[PROJECT.md](PROJECT.md)。**可调数值一律只写在 `hub.config.yaml`**；本文只定义名字与语义，代码注释只写指向本文的指针，不抄数值、不抄语义。
- 覆盖范围：存储、事件总线、采集器、规则、通知、报表与控制台、插件宿主、投递台账、手机界面、派生层都已落在本文件；已淘汰或有边界的能力在对应小节标注。

## 1. 范围

| 提供 | 不提供（后续期） |
|---|---|
| 事件入库（append-only）、去重、查询、保留策略 | 待办生成（M1）、通知（M2）、UI 与报表留痕（M3）、LLM 归纳（M4） |
| 四个**只读**采集器：`dsh-session` / `clipboard` / `window` / `fs` | 任何写用户文件、任何弹窗、任何网络 |
| 同一套 CLI 与库表结构供后续期复用 | 多机、云同步 |

## 2. CLI

### 2.1 `bin/hub-collect`

| 参数 | 语义 |
|---|---|
| `--once` | 跑一轮采集后退出（M0 唯一模式；不加则等价于 `--once`） |
| `--source NAME[,NAME…]` | 只跑指定采集器（名字见 §4）；默认跑配置里 enabled 的全部 |
| `--notify` | **M2f 触发面**：采完后若这一轮真的收进了新的 `window` 事件，就顺手评估一次通知（见 §4.6「触发面」）。开关仍看 `notify.enabled` |
| `--selftest` | 在临时目录里跑合成事件自检（不碰真实数据），全过退出 0 |
| `--json` | 机器可读汇总打到 stdout（**人读日志一律走 stderr**，故 stdout 可直接 `json.load`） |
| `--quiet` | 不打日志（只留退出码） |

退出码：`0` = 本轮无致命错误（**单个采集器失败不算致命**，见 §6）；`1` = 配置/存储层错误；`2` = 用法错误。

stdout（`--json`）：`{"run_at":…,"sources":{"<name>":{"ok":bool,"emitted":N,"skipped":N,"error":…}},"ingested":N,"skipped_duplicate":N,"window_new":N}`；
带 `--notify` 时多一个 `"notify":{…}`（`dispatch` 的返回结构见 §2.4；不做评估时是 `{"skipped":true,"reason":"no-new-ui-event","sent":0}`）。

### 2.2 `bin/hub-store`

> 注意：本命令的 `--json` 挂在**子命令**上（`hub-store stats --json`），不是全局参数 —— 与 `hub-collect --json` 的写法不同。

| 子命令 | 语义 |
|---|---|
| `stats [--json]` | 库体量：各 source 事件数、最早/最新时间、采集器游标、库文件大小 |
| `events [--limit N] [--source S] [--since ISO] [--json]` | 查事件（默认按 id 倒序） |
| `prune [--json]` | 按保留策略删除过期事件（只删 `events`，**永不删报表与修订**） |
| `redact [--source S] [--dry-run] [--limit N] [--json]` | **M15**：把历史事件里的验证码类正文换成占位符（**只 UPDATE `payload`，绝不 DELETE 行**；`--dry-run` 只报将命中几条；再跑命中 0）。`redact.enabled=false` ⇒ 不动库 |
| `doctor [--json]` | 检查配置路径、库可写、各采集器前置条件（`wl-paste`/`pyatspi`/会话目录）是否就绪 |
| `todos [--status S] [--limit N] [--json]` | 列待办（含 `human_override`/`notified_at`/卡片指针） |
| `todo-add --title T [--why W] [--dedupe-key K] [--source-event-id ID]… [--confidence F] [--json]` | **M3a**：「要人跟进的事」的唯一写入通道（agent / 脚本直接建，不经规则引擎）。幂等键缺省 = `manual:<sha1(title)[:12]>`；同键已存在 ⇒ 只刷新证据集合、返回既有 id 且 `new=false`。改动记录 `author=manual`、`reason=todo-add`。标题为空 ⇒ 退出 2；`--json` 给 `{id,new,dedupe_key,title}` |
| `todo-set <id> <open\|doing\|done\|dropped> [--json]` | 人工改待办状态（打 `human_override=1`，规则不再翻回） |
| `todo <id> [--json]` | **M3c**：单条待办的完整视图 = 元信息 + **证据链**（把 `source_event_ids` 摊开成真实 `events` 行）+ 改动记录。id 不存在 ⇒ 退出 1（`--json` 给 `{"error":"not-found"}`） |
| `todo-revisions [<id>] [--limit N] [--json]` | **M3b**：清单改动记录（省略 id = 最近所有待办） |

### 2.3 `bin/hub-rules`（M1）

| 子命令 | 语义 |
|---|---|
| `derive` | 跑规则 → 幂等写 `todos`（`--dry-run` 只算不写） |
| `board-sync` | 把还没有卡片指针的 open 待办推到看板（幂等靠 `todos.board_card_id`） |
| `rules` | 列出规则与开关 |

参数：`--json` / `--quiet` / `--dry-run` / `--limit N`。退出码口径同 §2.1（0 = 无致命错误）。

### 2.4 `bin/hub-notify`（M2）

| 子命令 | 语义 |
|---|---|
| `dispatch`（默认） | 评估一次：过五道门 → 有得发就发一条 → 写台账。`--dry-run` 只评估，不发送/不写库/不消费界面事件 |
| `status` | 把五道门现状摊开（开关/勿扰/空闲/界面事件游标/待提示待办/已弹计数），不改状态 |
| `test [--dry-run]` | **自检**：真弹一条**探针**通知并写台账（`kind=probe`）。**绕开门 1–8**、不碰待办；语义见 §4.6.1 之前的「自检探针」 |

参数：`--json` / `--quiet` / `--dry-run`。**跳过不是错误**：退出码 0，跳过原因走 `reason`/`detail`（见 §4.6）。

#### `bin/hub-notify act <dbus_id> <action>`（M2b）

把**通知上的按钮被点了**这件事落成待办状态（`human=True`，规则不许翻回去）：

- `acted` / `完成` ⇒ 待办 `done`；`ignore` / `忽略` ⇒ 待办 `dropped`；
- `open` / `打开`（M2d）⇒ **不改待办状态**，只留痕；`notify.open_url` 与 `notify.open_command` 都非空时执行该命令（占位符 `{url}`/`{kind}`/`{todo}`）；
  命令起不来只记 `open_error`（fail-soft：退出码仍 0，点击不算失败）；
- `snooze` / `稍后`（M2d）⇒ 写 `todos.snooze_until = now + notify.snooze_minutes` 并**清空 `notified_at`**，状态仍 `open`（人没说做完）；
- `ignore-kind` / `忽略此类`（M2c）⇒ 这一条 `dropped` + 整个类别压住 `notify.suppress_days` 天；
- 找不到该 `dbus_id` 的通知、动作键不认识、通知没有 `todo_id` 指针（老数据）⇒ 退出码 1，**不瞎猜**；
- 台账 `notifications.action` 记的是**稳定键**（见 §4.6.1 `CANON_ACTION`），不是按钮上的中文标签。

#### `bin/hub-notify listen [--once] [--timeout S]`（M2b）

常驻监听系统通知的动作回调（`gdbus monitor --session --dest org.freedesktop.Notifications`），
把 `ActionInvoked(uint32 <id>, '<key>')` → `act()`。`NotificationClosed` 只计数。
返回计数：`signals` / `acted` / `ignored` / `opened` / `snoozed` / `suppressed` / `closed` / `handled` / `errors` / `stopped`。
认不出的行忽略；找不到对应通知 ⇒ 记 `errors` 但退出码仍 0（通知早关了，这是竞态）；
`gdbus monitor` 自己非 0 退出 ⇒ `ActionError` ⇒ 退出码 1。

#### `bin/hub-notify suppress|unsuppress|suppressions`（M2c）

| 子命令 | 语义 |
|---|---|
| `suppress <kind> [--days N] [--note 文本]` | 把某类压住 N 天（缺省取 `notify.suppress_days`）；`--days 0` 打回（要解除请用 `unsuppress`） |
| `unsuppress <kind>` | 立刻解除（幂等：本来没有也返回 `removed=false`） |
| `suppressions [--all]` | 列出抑制（默认只看生效中的）；`--all` 连到期的也列 |

`kind` = 通知类别 = 待办 `dedupe_key` 的**规则段**（如 `fs-repeat`、`session-stale`、
`clipboard-domain-repeat`、`bili-unintegrated`）。`status` 会带上 `suppressions`（生效中的）与
`suppress_days`；本地控制台左栏也列出来，并提示解除命令（**不**在 UI 上写抑制，写入面仍只有
`/api/save` 与 `/api/generate`）。

### 2.5 `bin/hub-report`（M3）

| 子命令 | 语义 |
|---|---|
| `generate`（默认同 `show` 前的显式动作） | 按系统数据重写自动区块（`author=hub`），内容没变则不留痕 |
| `show` | 打印报表全文（默认动作） |
| `write` | 人手改：`--text` 整篇替换 / `--file <路径>` 整篇替换 / `--append <一段>` 追加；`--reason <词>` 记进修订 |
| `revisions` | 列出修改记录（rev/时间/作者/原因/前后 hash） |
| `diff --rev N` | 看某次修订的 diff（不给 `--rev` 取最新一次） |

全局参数（**写在子命令前**）：`--config` / `--json` / `--quiet` / `--report <名>`。契约见 §4.7。

### 2.6 `bin/hub-ui`（M3 / M3b）

起本地控制台：`--host`（默认 `ui.host`）/ `--port`（`0` = 内核分配）/ `--report` / `--once` / `--json`。
启动即向 stdout 打一行 JSON（含实际端口）。契约见 §4.8。路由（全部只绑 loopback + Host/Origin 守卫，见 §4.8）：

| 路由 | 方法 | 干什么 |
|---|---|---|
| `/api/state` | GET | 只读快照：系统数据（来源计数/待办/通知台账/被压类别）+ 报表正文 + 报表改动记录 + **清单改动记录**（M3b） |
| `/api/save` | POST | 存报表正文（author=human，记一次改动） |
| `/api/generate` | POST | 重写报表自动区块（author=hub） |
| `/api/todo` | POST | **M3b**：改一条待办 —— `{id, status?}` 或 `{id, snooze_minutes?, reason?}`；每次都写 `todo_revisions` |
| `/api/todo/revisions` | GET | **M3b**：`?id=<todo_id>` 的改动记录（省略 id = 最近所有待办） |
| `/api/todo/detail` | GET | **M3c**：`?id=<todo_id>` 单条待办的元信息 + **证据链**（摊开成真实事件）+ 改动记录；id 为空或查不到 ⇒ **404** `{"error":"not found"}` |

`/api/todo` 的失败语义：状态非法 / `snooze_minutes` 越界 ⇒ **400**；待办不存在 ⇒ **404**；Host/Origin 不合法 ⇒ **403**。
`/api/todo/detail` 是**纯只读**：不写 `events` / `todos` / `todo_revisions`（契约见 §5.8）。

### 2.7 归纳层入口 `bin/hub-digest` / `bin/hub-digest-local` / `bin/hub-week`（M4b / M4d / M4c）

| 命令 | 用法 | 语义 |
|---|---|---|
| `bin/hub-digest` | `run [--window-days N] [--force] [--dry-run]` / `show` | 归纳层；加工层 = `digest.command`（§4.9） |
| `bin/hub-digest-local` | `[--window-days N] [--config X]` | **确定性本地后端**：stdin 读上游提示词，stdout 出 `{"items":[…]}`（§4.9） |
| `bin/hub-digest-dsh` | `--session <id> [--bridge 路径] [--timeout 300]` | LLM 后端：把提示词丢给一个 DSH 会话（要那个会话活着） |
| `bin/hub-week` | `run [--weeks N] [--week-of YYYY-MM-DD] [--dry-run]` / `list` | 周报（§4.10） |

全局 `--json/--config/--quiet` 一律写在子命令**之前**。

### 2.7b `bin/hub-phone-remind`（2026-11-10，手机强提醒通道）

两个出口分工（用户 m00728）：**入口 = 系统常驻通知**（通知栏 ongoing「INFO-HUB · N 条待处理」，点开进列表）；**红条 / 全屏 = 强提醒**，只给单条紧急项。

| 子命令 | 语义 |
|---|---|
| `sync [--limit N] [--dry-run]` | 把 open/doing 待办全量推到手机（`hub_sync`）：更新通知栏常驻通知 + 列表数据；0 条时通知消失 |
| `push <todo_id> [--fullscreen] [--dry-run]` | 把一条 open/doing 待办作为**强提醒**推到手机：**亮屏时**出顶部红条，**锁屏时**自动出全屏页（`showWhenLocked` + `turnScreenOn`）。成功后写 `todos.notified_at`；非 open/doing ⇒ 退出 1 |
| `clear` | 收起手机上的顶部条 |
| `acks [--apply]` | 取走手机端「知道了」回执（bridge `reminder_ack_drain`）；`--apply` 给对应待办写一条 `field=acked_at` 的 revision（**不改 status** ——「看见了」不等于「做完了」） |

- 传输：手机 bridge（`phone_remind.bridge`，streamable-http MCP）→ `device_action` 的 `reminder_show` / `reminder_clear` / `reminder_ack_drain`。
- 手机侧实现（mcp_for_android 工程）：`server/ReminderOverlay.java` = TYPE_APPLICATION_OVERLAY 顶部条，状态落 SharedPreferences（进程被杀后自动恢复）；`server/ReminderActivity.java` = 锁屏全屏页。
- 边界：悬浮条盖不住状态栏与锁屏；锁屏只能靠 ReminderActivity。bridge 多设备在线时优先 `device_id` 以 `jxev-` 开头的设备（否则请求会打到 dsha-guest）。
- 失败语义：bridge 不可达 / 手机未连接 ⇒ 退出 1，待办不动（不写 `notified_at`）。

### 2.8 `bin/hub-screencast`（M7）

| 子命令 | 用法 | 语义 |
|---|---|---|
| `consent` | `[--json]` | **一次性**：「共享屏幕」同意对话框；成功 ⇒ `restore_token` 落 `<state_dir>/screencast.json`（0600）。用户拒绝/超时 ⇒ 退出 1，不写文件 |
| `status` | `[--json]` | 同意状态 / 落盘目录 / 图张数与字节 / `keep_days`；纯只读 |
| `grab` | `[--out 路径] [--json]` | **立刻**取一帧（真机自证用；不经过活跃度与间隔门 —— 那两道门在采集器里） |
| `prune` | `[--json]` | 按 `collect.screenshot.keep_days` 删过期图（**只删文件，不删事件元数据**） |

全局 `--json/--config/--quiet` 写在子命令之前（同 §2.7）。

### 2.9 `bin/hub-history`（M8）

| 子命令 | 用法 | 语义 |
|---|---|---|
| `summary` | `[--days N] [--top N] [--json]` | 窗口内浏览历史 → 用途/站点分布 + **一条总结**（`line`）。纯只读 |
| `classify` | `<url> [--title 文本] [--json]` | 单条 URL 的分类（`site` / `purpose`），调试与验收用；**不碰库** |

这类信息**只进报表与清单，不推送**（用户 2026-10-16 定：卡片不推送）。报表区块名 `history`（§4.7）。

### 2.10 `bin/hub-activity`（M10）

转译层入口：把 `activity.sample` 事件翻成「什么时候开始干活 / 专注程度 / 多久」。

| 子命令 | 干什么 | 退出码 |
|---|---|---|
| `sample` | 采一条活跃采样（在场/离开 + 窗口集合哈希），打印一行 | 探针失败=1 |
| `episodes [--day YYYY-MM-DD] [--rebuild]` | 从 `events` 重算当天片段写 `episodes`，打印片段表 | 0 |
| `day [--date YYYY-MM-DD]` | 白话汇总：第一次开始干活、在场合计、**扣掉手机游戏后的净时间**、片段列表 | 0 |

`--json` 一律可用；`day` 的白话输出即 §8 M10 判据里那条。

### 2.11 `bin/hub-vision`（M11，画面语义）

**读的是已经存下来的截屏，不新增取帧**。派生层：删掉标签表，从原图重跑一遍结果相同。

| 子命令 | 干什么 | 退出码 |
|---|---|---|
| `run [--limit N] [--dry-run]` | 给「还没读过」的截屏补类别（按时间升序补历史） | 0；引擎缺失也 0（结果里带 `error`） |
| `labels [--day YYYY-MM-DD] [--limit N]` | 列出已判类别 | 0 |
| `stats` | 已标注多少张 / 各类别多少 / 留了几条原文 | 0 |
| `deps` | 检查 OCR 依赖是否可用 | 可用 0，缺 1 |
| `classify <文本...>` | 只跑词表判定（不碰库，用来调词表） | 0 |
| `vlm [--limit N] [--all]` | **M12**：用本地小 VLM 给截屏做粗判 —— 工作 / 玩（`act` 表）；没装模型或没开 ⇒ 0 + `error` | 0 |
| `act [--day YYYY-MM-DD]` | **M12**：汇总粗判结果 —— 今天判了几张 / 工作几张 / 玩几张 / 还剩几张没判 | 0 |

`--json` 一律可用。**`hub-rules derive` 会顺带跑一次**（`vision.enabled=true` 时），失败只写进摘要 `vision`，不影响 derive 退出码。

### 2.12 `bin/hub-input`（M13，输入节奏）

读 `/dev/input/event*` 的**计数**：键盘按下多少次、点击多少次、光标跑了多远。**不读内容、不记 keycode。**

| 子命令 | 干什么 | 退出码 |
|---|---|---|
| `probe` | 列出可读的输入设备与能力（key/rel/abs） | 0（设一个都没读到也 0，只是列表为空） |
| `sample [--window-s N]` | 只采一个窗口并打印，**不进库**（调参用） | 0；无权限=1 |
| `listen` | 常驻：每 `window_s` 落一条 `input.sample`（systemd 服务，见 §9） | 0；无权限=1 |
| `stats [--day YYYY-MM-DD]` | 某天的汇总（按键/点击/滚轮/光标距离） | 0 |

`--json` 一律可用。**权限**：需要 `input` 组。本机已把用户加进 `/etc/group`，但**已登录的会话拿不到新组**，
所以：交互式用 `sg input -c '...'`；服务在本机用 `ExecStart=/usr/bin/sg input -c '...'`
（`systemd --user` 不支持 `SupplementaryGroups`，实测报 `Unknown assignment`）。

### 2.13 `bin/hub-calib`（M14，校准）

把「手上 / 眼睛 / 在场」三路特征，跟你亲口说的状态对齐，算 AUC。**没有这一步，专注度就只是我在自说自话。**

| 子命令 | 干什么 | 退出码 |
|---|---|---|
| `rate <1\|2\|3>` | 记一次自报：1 专注 / 2 一般 / 3 走神（写 `self.report` 事件） | 0 |
| `report [--days N] [--window-min N]` | 每条自报取它之前 N 分钟的特征，算各路 AUC | 0（样本<6 只提示，不算失败） |

`--json` 一律可用。判读：AUC ≥0.65 算有判别力、≤0.35 算反向、中间算没区分度；1.0 完美、0.5 等于扔硬币。

### 2.14 `bin/hub-features`（M16，行为特征层）

把散落的事件**翻译成一行一小时的「人」的指标** —— 这就是 m00113 要的转译层的成品形态。

| 子命令 | 干什么 | 退出码 |
|---|---|---|
| `build [--day YYYY-MM-DD]` | 从 events 重算当天 24 行特征写入 `features` 表（幂等） | 0 |
| `show [--day YYYY-MM-DD] [--json]` | 打印当天特征表（每小时一行） | 0 |

`hub-rules derive` 每轮结束会顺带 `build` 当天与前一天的 24 行（前一天用于补 23 点后的尾巴；不新增定时器）。口径版本 `behavior.feat_version`（默认 `feat-1`）。

## 3. 配置键（`hub.config.yaml`）

| 键 | 语义 | 备注 |
|---|---|---|
| `root` | 数据根目录（库/报表/状态/日志都在其下） | 可被环境变量 `HUB_ROOT` 覆盖（测试用） |
| `db` | 库文件名（相对 `root`） | |
| `bus` | 事件 spool 文件名（相对 `root`） | |
| `reports_dir` | 报表目录名（相对 `root`） | M3 使用，M0 只建目录 |
| `retention_days` | `events` 保留天数 | 报表与修订不受影响 |
| `sessions_root` | DSH 会话存档根 | |
| `collect.<name>.enabled` | 是否启用该采集器 | 名字与语义见 §4 |
| `collect.<name>.*` | 各采集器私有键 | 见 §4 与配置文件注释 |
| `log_dir` / `log_name` | 日志位置 | |
| `notify.enabled` | 通知总开关 | **默认 false**：§一「不做主动提醒」的例外未落 AGENTS.md 前不开 |
| `notify.backend` | 发送后端 | `gdbus`（走 `org.freedesktop.Notifications.Notify`）/ `command`（argv 模板，测试用）/ `stdout` |
| `notify.command` | `backend=command` 的 argv 模板 | 占位符 `{summary}` `{body}` `{kind}` `{app}` |
| `notify.dnd_source` | 勿扰探测来源 | `gsettings` / `off`；**YAML 里 `off` 会被解析成 false，两种写法都当 off 处理** |
| `notify.idle_source` | 空闲探测来源 | `mutter`（`IdleMonitor.GetIdletime`）/ `off` |
| `notify.idle_max_s` | 空闲 ≥ 此值 ⇒ 不弹 | 用户 m01440：弹的那一瞬要贴着"我在做什么" |
| `notify.require_ui_event` | 是否要求一次新的 `window` 事件 | true 时"一次界面切换最多评估一次"（游标 `notify:ui`） |
| `notify.min_interval_s` / `max_per_hour` / `max_per_day_per_kind` | 节流三道 | 只统计 `shown=1` 的记录 |
| `notify.max_per_dispatch` | 每次最多弹几条 | 初值 1 |
| `phone_remind.bridge` | 手机强提醒走哪个 bridge MCP 端点 | 见 §2.7b；缺省 `http://<bridge-host>:9500/mcp` |
| `phone_remind.timeout_s` | 单次 MCP 调用超时 | 缺省 30 |

| `plugins.paths` | 额外插件根目录（绝对路径数组） | 可空；发现根顺序见 §10 |
| `plugins.<name>.enabled` | 单个插件开关 | 覆盖 `plugin.yaml` 的 `default_enabled` |
| `notify.effectiveness_window_days` / `ignore_after_hours` | 有效性统计窗口 / 多久没动作算被忽略 | 初值 14 天 / 24h |
| `notify.downshift_min_samples` / `downshift_threshold` / `downshift_max_per_day` | 降频门槛（样本数、忽略率、降频后每天上限） | 初值 5 / 0.5 / 1 |
| `notify.timeout_ms` / `send_timeout_s` / `app_name` / `summary` | 通知动作参数 | 见配置文件注释 |
| `notify.actions` | 通知上的按钮集（顺序即显示顺序，标签=动作键） | 默认 `["打开", "稍后", "忽略此类"]`；GNOME 横幅一般只显示前 3 个 |
| `notify.snooze_minutes` | 「稍后」推多久（分钟） | 初值 60；到点后靠下一次界面事件再提示 |
| `notify.open_command` / `open_url` / `open_timeout_s` | 「打开」执行什么 / 传进去的地址 / 超时 | 默认 `["xdg-open", "{url}"]` + 控制台地址；空列表 = 只留痕不开任何东西 |
| `digest.enabled` / `window_days` / `command` | 归纳层开关 / 归纳窗口 / 加工层命令（argv 模板，提示词走 stdin） | **换模型 = 换命令**；M4d 起默认 `true` + 每周一次，命令指向本地确定性后端 `bin/hub-digest-local`（离线、零 token） |
| `digest.max_events` / `max_todos` / `max_items` / `timeout_s` | 归纳预算 | 事件 payload 每条截 400 字符 |
| `digest.todo_evidence_events` | 待办证据事件**额外**进提示词的条数上限（默认 40） | M4d：待办依据可能老于 `max_events` 那条流水，不带上就会被审计丢掉 |
| `digest.local.evidence_per_item` | 本地后端每条归纳最多挂几个证据（默认 3） | `bin/hub-digest-local` 专用 |
| `report.name` | 报表名（→ `reports/<name>.md`） | 白名单字符 `[A-Za-z0-9._-]` |
| `report.blocks` | 参与重写的自动区块名与顺序 | 内置 `stats` / `todos` / `notifications` / `digest` / `history`（M8） |
| `report.history_days` | 报表里「浏览历史」区块回看几天 | 默认 1（M8） |
| `report.todos_limit` / `notifications_limit` | 报表里两类清单的条数 | |
| `report.revisions_show` | `hub-report revisions` 默认显示条数 | |
| `report.diff_max_chars` | 单次修订 diff 截断长度 | |
| `ui.enabled` | 将来是否随服务自启控制台 | 手动 `bin/hub-ui` 不受它限制 |
| `ui.host` / `ui.port` | 控制台绑定 | **只允许 loopback** |
| `ui.max_body_bytes` | POST 体积上限 | |
| `ui.quiet_access_log` | 是否静默访问日志 | 默认 true |
| `collect.camera.*` | M14 在座状态：`enabled`（默认 true）、`index`（默认 0）、`model`（YuNet 路径）、`min_face_ratio`（默认 0.05）、`yaw_max`（默认 0.25） | 只在内存判定，不存图，见 §4.19 |
| `collect.input.*` | M13 输入节奏：`enabled`（默认 true）、`window_s`（窗口秒数，默认 60） | 需要 input 组，见 §2.12 |
| `collect.activity.*` | 活跃采样（M10） | **`enabled` 默认 false**：它由 `info-hub-activity.timer` 单独驱动，混进 5 分钟的 collect 轮会把分辨率拉到 5 分钟 |
| `collect.phone-usage.*` | 手机屏幕使用时间（M17，USB adb 直采 `dumpsys usagestats`） | `enabled` 默认 **false**：要插线才采得到，不能当常驻 |
| `collect.study.*` | 学习状态采集：`enabled`、`log_path`（学习日志绝对路径）、`timeout_s` | 只读日志；每天一条 `study.pulse`，不发消息，见 §4 采集器表 |
| `rules.study-stale.*` | 学习停滞：`enabled`、`stale_days`（**必填正整数**）、`scan_limit` | 只产待办候选；`stale_days` 缺键 ⇒ 规则报错、不产候选，见 §4.5 |
| `behavior.*` | 片段与专注度口径（M10） | 见下方两条；口径版本 `def_version` 改动即须换号 |

配置文件**是唯一数值源**；新增可调参数必须同时改本表与配置文件。

- `collect.clipboard.*`：`command`/`args`/`max_chars`/`timeout_s` 之外，还有 **`backoff_after`**（连续失败几次进冷却，默认 3）
  与 **`backoff_s`**（冷却时长秒，默认 900）。冷却期内该采集器**直接返回空、不算失败**（不再每轮白等 `timeout_s`），
  状态写在自己的 `collector_state` 里（`hub-store stats` 能看到 `ERR …进入冷却`）。
- `collect.bilibili.*`：`enabled`（默认 false，要显式接来源）、`source`（`command`|`file`）、
  `command`（argv）、`input`（`source=file` 时的 JSON 路径）、`kind`（事件里标注 `favorite|toview|dynamic`）、
  `max_items`、`keep_ids`、`timeout_s`。
- `collect.browser-history.*`（M5b / M8）：`enabled`（默认 true）、`browser`（`edge`|`chrome`|`chromium`）、
  `profile`（档案目录名，空 ⇒ 自动取 History 最新的档案）、`db`（直接给 History 路径，给了就不猜）、
  `since_days`（M8 起默认 180，跟事件留存对齐）、`max_items`（**每类**上限：`repo`/`video`/`history` 各算）、
  `keep_ids`、`kinds`（M8 起默认 `[history]` ⇒ 全量历史；`repo`/`video` 仍在，见 §4.12）、
  `title_chars`（每条只留标题前 N 字，默认 120）。
- `collect.qq.*`（2026-11-18 定型为 OneBot spool 版）：`enabled`（默认 false）、`ssh_host`（默认 `root@<bridge-host>`，置空=本机执行同一条远端命令，供测试注入）、`spool_path`（默认 `/opt/qq-relay/queue/events.jsonl`）、`remote_cmd`（默认远端 `python3 -c`，收 `<spool> <offset> <max_bytes>` 三参，返回 base64 字节块 + 文件大小）、`limit`（每轮上限，默认 200）、`max_bytes`（单轮最多读多少字节，默认 4 MiB）、`timeout_s`（默认 20）、`seen_keys_max`（去重键保留上限，默认 500）、`include_self`（是否收自己发的，默认 false）。清单 `default_enabled: false` ⇒ **还必须** `plugins.qq.enabled: true` 才被注册（见 §10 的开关优先级）。
- `collect.phone-hub.*`（M15）：`enabled`（默认 true）、`ssh_host`（默认 `root@<wg-server-ip>`）、`ssh_key`（默认 `~/.ssh/id_ed25519`）、`ssh_proxy`（默认经本机 wireproxy 的 `ncat --proxy 127.0.0.1:11080 --proxy-type socks5 %h %p`）、`remote_cmd`（默认在 ECS 上跑 `hub_dump.py`）、`limit`（每轮最多取几条，默认 1000）、`head_chars`（标题/正文截断，默认 160）、`timeout_s`、`backoff_after` / `backoff_s`。**最终存储在个人服务器（家庭机 <bridge-host>）**：本机只存镜像，本采集器不联系手机，只从 ECS `hub.db` 按 `ts` 升序拉增量（`hub_dump.py` 必须 `WHERE ts > ? ORDER BY ts ASC`，否则游标会跳过历史）。
- `redact.*`（M15 验证码脱敏，语义见 §4.21）：`enabled`（默认 **false** —— 缺这一段就是旧行为；随仓库的配置文件里已打开）、
  `mode`（只认 `mask`：正文整段换成占位符；其它值按配置非法处理）、`placeholder`（替换后的正文，默认 `[验证码类]`；
  **它本身含「验证码」三字**，所以幂等不能靠词形，见 §4.21「幂等」）、`patterns_version`（判据版本，默认 `rd-1`；
  **改词表/判据必须换号**）、`window_chars`（字样与数字串最多隔几个字符还算「紧跟」，默认 24）、
  `min_digits` / `max_digits`（数字串位数区间，默认 4–10：4 位银行码与 6/8 位常规码都覆盖，少于 4 位当计数/年份）。
- `collect.screenshot.*`（M7）：`enabled`（**默认 false** —— 要先 `bin/hub-screencast consent` 点一次同意）、
  `backend`（`screencast`|`command`；`command` 是验收/替代后端，argv 里的 `{out}` 与 `$HUB_SHOT_OUT` 都是输出路径）、
  `command`、`interval_s`（两帧最短间隔）、`require_active`、`idle_max_s`（活跃判据：mutter 空闲 < 此值）、
  `idle_min_s`（**打字保护**：mutter 空闲 < 此值 ⇒ 视作「正在敲键 / 候选窗开着」，先等一次停顿再取；默认 3，`0` = 关）、
  `pause_wait_s`（等停顿的上限秒数，默认 20；等不到 ⇒ 这轮不取、`last_skip="typing"`，见 §4.13 采集器第 3 步）、
  `max_width`（等比缩放目标宽，窄画面不放大）、`jpeg_quality`、`dir`（空 ⇒ `<root>/screens`）、
  `keep_days`（默认 7）、`head_chars`、`capture_timeout_s`、`consent_timeout_s`、`token_file`（空 ⇒ `<state_dir>/screencast.json`）。
- `rules.bili-unintegrated.*`：`enabled`（默认 true；没有 bilibili 事件时天然无事发生）、`window_days`、
  `scan_limit`、`cache_days`、`name_min_len`、`kb_root`（默认 `~/knowledge_base`）；M5b 起证据可来自
  `bilibili` **或** `browser-history` 两个 source。
- `collect.activity.*`（M10）：`enabled`（默认 **false**，见上）、`interval_s`（两条采样最短间隔，默认 60）、
  `max_frames`（枚举窗口上限，默认 40）、`head_chars`（标题只参与哈希，截断长度，默认 120）、`timeout_s`（默认 10）。
- `behavior.*`（M10）：`idle_active_s`（空闲 ≤ 此值算在场，默认 **180**；2026-10-28 由 60 调宽 —— 看代码/想事情不动键鼠会被 60 秒门槛误判成离开，片段被切碎、专注分偏低。**采样里存了 `idle_s`，所以改门槛只重算、不重采**）、`gap_s`（相邻采样间隔超过它就断段，默认 300）、
  `min_active_s`（在场累计不足这么多秒不落库，默认 60）、`switch_per_min_full`（每分钟切几次窗口就让稳定度归零，默认 1.0）、
  `focus_weights`（`presence`/`stability`/`continuity` 三权重，合计 1.0，默认 0.5/0.3/0.2）、
  `heartbeat_s`（离开时每隔多久补一条心跳采样，默认 900）、`day_start_min_s`（当天累计在场够几分钟算「开始干活」，默认 600）、
  `def_version`（口径版本，默认 `ep-1`；**改公式必须换版本**，新旧行共存不互相覆盖）。

- `collect.phone-usage.*`（M17）：`enabled`（默认 **false**）、`adb`（adb 可执行文件，默认 `adb` 走 PATH）、
  `serial`（设备序列号，空 ⇒ 只认唯一设备）、`interval_s`（两次快照最短间隔，默认 1800 —— 屏幕时长是累计量，采太勤没信息）、
  `top_n`（payload 里保留几个应用，默认 15）、`timeout_s`（默认 60）、
  `games`（**M18**：哪些包算游戏，前缀匹配 —— 安卓不给可靠分类，所以由配置声明，代码不猜）。
  `behavior.phone_game_exclude_min`（默认 30）：该小时手机游戏在前台超过这么多分钟，
  就把这一小时的电脑在场按比例扣掉（口径见 §2.10 `day` 的「扣掉手机」一行）。

`vision.*`（M11 画面语义，见 §4.16）：`enabled`（**代码默认 false**；随仓库的 `hub.config.yaml` 里已打开 —— 关掉就完全退化成 M10 的行为）、
`engine`（默认 `rapidocr`）、`max_per_run`（单轮最多读几张，默认 8）、`min_hits`（一个类别至少命中几个词，默认 2，不够判 `unknown`）、
`keep_raw`（是否保留 OCR 原文，默认 **false**）、`retention_text_days`（原文保留天数，默认 7，`hub-store prune` 会清）、
`threads`（onnxruntime 线程上限，默认 4；**实测环境变量与 `sched_setaffinity` 都压不住**，只有建会话前改
`SessionOptions.intra_op_num_threads` 有效 —— 不设的话一张 1280×800 图会烧掉约 50 秒 CPU）。

`vision.vlm.*`（M12 画面粗判，见 §4.17）：`enabled`（默认 **false**；没有模型文件认出也自动跳过）、
`model` / `mmproj`（GGUF 路径；相对路径按 `HUB_ROOT` 解析）、`max_width`（送模型前缩到多宽，默认 512）、
`jpeg_quality`（缩图质量，默认 75）、`threads`（默认 **2**）、`ctx_size`（上下文，默认 512 —— 只放一张图一句话，
调大会涨内存）、`timeout_s`（单张超时，默认 60）、`max_per_run`（默认 4）。实测 2B-Q4 + F16 mmproj 峰值 RSS **2.8 GB**。

## 4. 采集器契约

统一签名：`collect(config, store) -> list[event]`：`config` 见 §3，`store` 用来读/写游标（`state_get` / `state_put`），事件由 `events.make_event()` 造（时间戳取法见 §5.1）。
统一要求：**只读**；不阻塞 > `timeout_s`；失败抛出异常由调用方兜住（§6）。
**退避纪律**：可能长期不可用的采集器（剪贴板、外部服务）必须把「连续失败次数 + 冷却截止时间」写进自己的游标，
失败到阈值后一段时间直接返回空 —— 否则每轮都白等一次 `timeout_s`，把定时采集拖成「每次都在等超时」。判据：
`tests/verify_m0.sh` T14（前两轮 `ok=false`、第三轮 `ok=true` 且耗时 < 250ms）。

| name | 观察什么 | `kind` | 游标（`collector_state.cursor`） |
|---|---|---|---|
| `dsh-session` | `sessions_root` 下的 `session.v4.jsonl.zstd` | DSH 事件原类型（如 `assistant/message`、`agent/inbox/spliced`） | `{"mtime":float,"lines":int}` 每会话一条（state key = `dsh-session:<session_id>`） |
| `clipboard` | Wayland 剪贴板当前文本 | `clip.copy` | `{"hash":"sha256…"}` |
| `window` | 无障碍树里的窗口（应用名 + 标题） | `window.snapshot` | `{"hash":"sha256…"}` |
| `fs` | `collect.fs.roots` 下改动过的文件 | `fs.change` | `{"mtime":float}` 每根目录一条 |
| `bilibili` | 账号收藏夹/稍后再看（`collect.bilibili`） | `bili.item` | `{"ids":[…]}`，只留最近 `keep_ids` 个 |
| `browser-history` | 本机浏览器历史（Edge/Chrome/Chromium 的 `History` 库，M5b / M8） | `browser.repo` / `browser.video` / `browser.history`（M8 全量类） | `{"ids":[…]}`，只留最近 `keep_ids` 个 |
| `phone-hub` | 手机通知（服务器为最终存储，本机镜像；M15） | `notify` | `{"last_ts":int,"fail_streak":int,"backoff_until_ts":float}`；本机库是镜像 |
| `qq` | 服务器 qq-relay 通知库里的 QQ 行（路径 A，2026-11-18） | `notify` | `{"last_id":int,"seen_keys":[str],"fail_streak":int}`；去重键 = 通知 `key` |
| `activity` | 键鼠在场状态（mutter 空闲时长）+ 窗口集合哈希（M10） | `activity.sample` | `{"epoch":float,"at":str,"active":bool,"frame_hash":str}` |
| `phone-usage` | 手机屏幕使用时间（`adb shell dumpsys usagestats` 日桶，M17） | `phone.usage` | `{"epoch":float,"day":str}` |
| `study` | 学习日志（`collect.study.log_path`）`## 记录` 区的最近一行 —— 学习状态采集（HUB_TASK_study-collector） | `study.pulse` | `{"last_day":"YYYY-MM-DD","fingerprint":str}`，state key = `study:<日志绝对路径>`；同一天同一份 payload ⇒ 返回空（幂等不靠 events 的 `(source,fingerprint,ts)` 索引：`ts` 每次都不同） |

**注册点（S3 起）**：采集器注册表 = `plugins/**/plugin.yaml` 里 `kind=source` 的插件（`src/infohub/sources.py:registry()`）；
本体不再 import 任何具体采集器。两道开关：`plugins.<name>.enabled`（装没装，缺省 true，显式 `false` 才卸掉）
与 `collect.<name>.enabled`（这轮跑不跑，缺省取清单 `default_enabled`）。`bin/hub-collect --source NAME`
**不看** `collect.<name>.enabled`（显式点名就跑），但仍受 `plugins.<name>.enabled=false` 约束。

### 4.5 规则契约（M1）

- 位置 `src/infohub/rules.py`；阈值只写在 `hub.config.yaml` 的 `rules.<name>.*`（本表只登记名字）。
- 签名 `rule(store, config, now) -> list[candidate]`，`candidate = {rule, dedupe_key, title, why, confidence, source_event_ids}`。
- **幂等键** `dedupe_key = "<rule>:<subject>:<YYYY-MM-DD（最新证据那天）>"`；再跑只刷新 `source_event_ids`，不新建、不动 `status`。
- 铁律：规则**只读** `events`、只写 `todos`；**永不触碰报表文件**（PROJECT.md §5.1）。单条规则抛错不影响其它规则（§6 同口径）。

| rule | 观察什么 | 阈值键 |
|---|---|---|
| `fs-repeat` | 同一文件在窗口内改动 ≥ N 次 | `rules.fs-repeat.{window_hours,min_count,scan_limit}` |
| `session-stale` | 某会话最新事件早于 `stale_hours`，且最后一条不在 `settled_kinds` | `rules.session-stale.{stale_hours,settled_kinds}` |
| `clipboard-domain-repeat` | 最近 N 条剪贴板里同域链接 ≥ M 条 | `rules.clipboard-domain-repeat.{scan_limit,min_count}` |
| `bili-unintegrated` | 窗口内 `bilibili` 或 `browser-history` 事件里提到的仓库，KB 里查不到（M5/M5b） | `rules.bili-unintegrated.{window_days,scan_limit,cache_days,name_min_len,kb_root}` |
| `study-stale` | 最新一条 `study.pulse` 里的学习记录已停 ≥ `stale_days` 天（只读 `events`，只产候选；不发消息；幂等键 bucket 取**最近一条学习记录那天**，不是运行日） | `rules.study-stale.{stale_days,scan_limit}`；`stale_days` **必填正整数**，缺键 ⇒ 规则报错且不产候选 |

### 4.6 通知契约（M2）

#### 触发面（M2f）：谁来"叫醒"通知层

门 1–8 只回答"这条能不能发"，不回答"什么时候去评估"。触发面就是后者，判据只有一条：
**这一轮采集真的收进了新的 `window` 事件**（`bin/hub-collect --once --notify`；`window_new > 0`）。

- 为什么不用定时器：`AGENTS.md §一` 例外条文①要求「只在检测到界面活动时评估……绝不按时间表推送」。
  若用 timer 触发，**评估**本身就变成按时间表发生 —— 条文①②就只剩门在挡，触发点失守。
  这里定时的只是**采集**（每 5 分钟一轮，M0 既有行为），**推送**永远挂在"收到新界面事件"上。
- 库里**旧**的 `window` 事件不算数（否则等于把定时器换个写法）：看的是这一次 `run_round` 的入库批次。
- `--notify` 是**显式 opt-in**：不传就完全不评估（`hub-collect` 默认行为不变）。装上系统单元时才在
  `ExecStart` 里带它（`packaging/info-hub-collect.service`）。
- `notify.enabled=false` 时触发面照走、门 1 挡住（`reason=disabled`，退出码仍 0）⇒ 采集语义不受通知影响。
- 验收：`tests/verify_m2f.sh`（9 项：opt-in / 收到界面事件才评估 / 只收 fs 不评估 / 旧事件不算数 /
  收敛 / 门 1 / 退出码 / 单元接线 / 帮助面）。

位置 `src/infohub/notify.py`。`dispatch(config, store, env=None, dry_run=False) -> dict`。

**五道门，顺序固定**（任一不过即跳过，`reason` 取下列字符串之一）：

| # | 门 | 不过时的 `reason` |
|---|---|---|
| 1 | `notify.enabled` | `disabled` |
| 2 | 勿扰（`show-banners=false`） | `dnd` |
| 3 | 空闲 ≥ `idle_max_s`（人不在） | `idle` |
| 4 | 新的 `window` 事件（`require_ui_event`） | `no-ui-event` |
| 5 | 节流：最小间隔 / 每小时 / 同类每天 | `throttled-min-interval` / `throttled-hour` / `throttled-day` |
| 5b | 降频中（M4a，见下） | `throttled-downshifted` |
| 6 | 该类被抑制（M2c，见 §4.6.2） | `kind-suppressed` |
| 7 | 候选被「稍后」推迟且未到点（M2d，见 §4.6.3） | `snoozed` |
| 8 | 候选**没有任何依据事件**（M3c，见 §5.8） | `no-evidence` |
| — | 没有还没提示过的 open 待办 | `no-pending-todo` |
| — | 勿扰/空闲探针本身失败 | `probe-error`（**fail-closed**：分不清就不弹） |

- 返回结构：`{at, enabled, sent, skipped, reason, detail, gates[], kind, todo{id,title,dedupe_key}, body, evidence, error, would_send?}`。
- **门 6 / 7 / 8 在同一处候选循环里判**（`continue`，不是 `break`）：被跳过的那条**不占位**，后面的候选照常评估 ——
  被压住的类别、被推迟的、没依据的，都不许把别的待办饿死。三条都拦下时 `reason` 取
  `kind-suppressed` > `snoozed` > `no-evidence` 中第一个非空的（看得见原因，而不是灰头土脸的 `no-pending-todo`）。
- `evidence` = 本次挑中的待办的 `source_event_ids` 条数（`0` 不可能出现：门 8 已经把它拦掉了）。
- 候选 = `todos` 里 `status='open' AND notified_at IS NULL`，最新在前，一次最多 `max_per_dispatch` 条。
- 成功：写 `notifications(shown=1)` + `todos.notified_at` + 消费界面事件游标（`collector_state['notify:ui']`）。
- 失败：写 `notifications(shown=0)`，**不标记 `notified_at`、不消费游标**（下一轮还会试，节流兜住）。
- 措辞纪律（PROJECT.md §6.1）：只陈述事实 + 依据条数，不带催促语；**没有依据不发**。

**门 5b：有效性降频（M4a）**。`notify.py: effectiveness(config, store) -> dict`；
`store.notification_effectiveness(window_days, ignore_after_hours, now=None) -> list[dict]`。
每条已弹通知（`shown=1`）按「有没有人动作」机械判定，按 `kind` 汇总：

| 归属 | 判据 | 是否进样本 |
|---|---|---|
| `acted` | 关联待办 `status='done'` 且 `todos.updated_at` ≥ 通知 `ts` | 是 |
| `ignored` | 未 acted 且 `now - ts ≥ ignore_after_hours` | 是 |
| `pending` | 未 acted 但还在判定期内 | 否 |
| `unlinked` | 老数据没有 `todo_id` 指针 | **否**（不冤枉） |

`ignored_rate = ignored / samples`（`samples = acted + ignored`；`samples=0` ⇒ `None`）。
`downshifted = samples ≥ notify.downshift_min_samples AND ignored_rate > notify.downshift_threshold`；降频后的效果**只是门 5b**：该类当天最多再弹 `notify.downshift_max_per_day` 条（降频 ≠ 屏蔽）。
`bin/hub-notify effectiveness [--json]` 只读摊开判定；`status` 的 `downshifted` 列出当前降频类别。

#### 4.6.2 类别抑制：「忽略此类」（M2c）

`notify.py`：`suppress(config, store, kind, days=None, note=None) -> dict`、
`unsuppress(config, store, kind) -> dict`、`suppressions(store, active_only=True) -> list[dict]`、
`effective_suppressions(store, now=None) -> set[str]`、`ActionError`。
表 `kind_suppressions(kind PRIMARY KEY, until, note, source, created_at, updated_at)`。

- **语义**：点通知上的「忽略此类」= ① 这一条待办 `dropped`（人点的，规则不许翻回去）+
  ② 整个**类别**（= 待办 `dedupe_key` 的规则段）压住 `notify.suppress_days` 天（默认 7）。
- **到期自动恢复**：门 6 只认 `until > now`（`effective_suppressions` 过滤），过期记录留在表里做历史，
  **不是永久屏蔽**。想提前恢复用 `unsuppress`。
- **不许饿死别的类别**：门 6 在**选候选时**跳过被压住的类别，而不是拦下整轮
  ⇒ 「A 类被压住」不会让 B 类的待办永远发不出去（`tests/verify_m2c.sh` T5 就是断言这条）。
- 动作键：`ACTIONS` 增加 `ignore-kind` / `忽略此类` ⇒ `ACTION_STATUS` 映射为 `dropped`；
  通知按钮文案与键名同源（改一处即改两处）。

#### 自检探针（`bin/hub-notify test`）

`notify.py`：`PROBE_BODY`、`probe(config, store, dry_run: bool = False) -> dict`。

回答的是**「这台机器上通知到底弹不弹得出来」**，不是「你有没有待办」。因此它**刻意绕开门 1–8**：
不看 `enabled`、不查勿扰/空闲、不等新的界面事件、不排队节流。理由是这个问题**不能依赖开关**，
也**不能依赖用户恰好切了界面**（否则自检本身就会被门拦住，得到「假阴性」）。
判据是 PROJECT.md §15 的 M2 行（「弹出一条 / 台账 +1」）。

| 项 | 语义 |
|---|---|
| 绕开门 | 真弹一条**探针**通知，正文固定 `PROBE_BODY`（写明「这条不是待办提示，不需要处理」，不催人） |
| 不碰待办 | 不改任何 `todos` 行、不写 `notified_at` ⇒ 不消费、不推迟、不勾掉任何待办 |
| 台账 | 成功 ⇒ 记一条 `kind='probe'`、`shown=1`、`todo_id=NULL`、`basis_event_ids=[]` |
| 不进样本 | `todo_id` 空 ⇒ `effectiveness` 记成 `unlinked`、**不进样本**（§4.6.1 表末行）⇒ **不污染降频判定** |
| 没弹出去 | 返回 `error`，**不记台账**（没弹就没有"弹过"），**退出码仍 0** —— 自检的结论是「通/不通」，不是「命令失败」 |
| `--dry-run` | 只回报会发什么：不弹、不记账 |

⇒ **`kind='probe'` 的行不构成 B 形态证据**：B 形态指的是
「按门评估 → 发 → 记账 → 按动作回写」那条闭环，判据看的是带 `todo_id` 的台账行。

#### 4.6.1 动作回写（M2b）

| 项 | 语义 |
|---|---|
| 按钮键 | `ACTIONS = ["acted","完成","open","打开","snooze","稍后","ignore","忽略","ignore-kind","忽略此类"]`；`ACTION_STATUS` 把键映射成待办状态（`acted/完成`→`done`，`ignore/忽略`→`dropped`）——**键名和状态的对应写死在代码里**，不靠猜 |
| 标签→稳定键 | `CANON_ACTION`（M2d）：D-Bus 回传的是**显示标签**，写库/回 JSON 一律用稳定键（`打开`→`open`、`稍后`→`snooze`、`忽略此类`→`ignore-kind`）⇒ 查询只有一套键 |
| 默认按钮 | `notify.actions`，默认 `["打开", "稍后", "忽略此类"]`；`status` 会把实际按钮集摊开 |
| `parse_dbus_id(text)` | 从 gdbus 回显 `(uint32 70,)` 取通知 id；取不到 `None`（不算错，只是回写不了） |
| 只认 gdbus 后端 | `notify.backend != gdbus` 时 `dbus_id` 一律 `None`（别的后端没有 D-Bus 回调，不假装有） |
| `act(config, store, dbus_id, action)` | 按 `dbus_id` 回查台账 → 映射状态 → `set_todo_status(todo_id, status, human=True)` → `notifications.action` 留痕。查不到/键不认识/无 `todo_id` ⇒ `ActionError` |
| `open`（M2d） | 只留痕 +（可选）执行 `notify.open_command`；**不改状态**；失败 ⇒ `open_error`，退出码仍 0 |
| `snooze`（M2d） | `store.set_todo_snooze(todo_id, until)`：写 `snooze_until` + 清 `notified_at`；门 7 跳过未到点的推迟 |
| `listen(...)` | 信号行 → `act()`；`--once` 处理第一个动作即退出；`--timeout S` 到点收工；`lines=` 供测试注入 |
| 真机实测 | Fedora 44 / GNOME Shell 50.5：`CloseNotification` 与到期**都不发** `NotificationClosed`（`closed` 可能恒 0）；`ActionInvoked` 要真人点 ⇒ 无头验收走 `hub-notify act` |

#### 4.6.3 「打开」与「稍后」（M2d）

- **门 7（`snoozed`）**：候选的 `snooze_until` 晚于当前时刻 ⇒ 跳过；全被推迟时 `reason=snoozed`，
  `detail` 里带上被推迟的待办 id 与时刻（把原因说出来，不伪装成「没有待办」）。
- **「打开」**：`notify.open_command`（argv 模板，占位符 `{url}`/`{kind}`/`{todo}`）+ `notify.open_url`；
  两者任一为空 ⇒ **只留痕、不开任何东西**。命令超时 `notify.open_timeout_s`，失败只记 `open_error`（退出码仍 0），
  **绝不改待办状态**（点开 ≠ 做完）。
- **「稍后」**：`store.set_todo_snooze(todo_id, until)` ⇒ `snooze_until = now + notify.snooze_minutes`、
  **`notified_at = NULL`**、状态仍 `open`；到点后要等一次**新的界面事件**才会再弹（不会自己按时间冒出来）。
- **判据**：[tests/verify_m2d.sh](tests/verify_m2d.sh)（T2–T4 推迟/门/到点再弹，T6–T7 打开与 fail-soft，T5 按钮集可配）。

### 4.7 报表契约（M3）

位置 `plugins/views/report/report.py`（S15 起是 view 插件，`entry: report.py:generate`）。核心是**分离**：报表文件是人拥有的，机器只重写标记出来的自动区块。

- 标记：`<!-- hub:auto:begin <name> -->` … `<!-- hub:auto:end <name> -->`；`name` 属 `report.blocks`。
- `merge(old, rendered, order)`：区块内换成新渲染；**区块外的文字逐字保留**；未知区块原样不动；缺的已知区块补在末尾。
- `generate(config, store)` = `merge(read_report(), render_blocks())` → `write_report(author="hub")`。
- `write_report(...)`：内容 hash 没变 ⇒ **不写文件、不留痕**（幂等）；变了才写并追加一条 `report_revisions`（`author` ∈ `hub` / `human`，含 `before_hash`/`after_hash`/`diff`）。
- 三个内置区块渲染器：`stats`（事件总数/时间范围/按来源/采集器异常）、`todos`（未完成待办 + 是否人改）、`notifications`（最近台账）。
- **报表只读系统数据**：M3 的任何路径都不写 `events`/`todos`/`notifications`。
- 路径纪律：报表名必须匹配 `^[A-Za-z0-9._-]{1,64}$`，解析后必须仍落在 `reports_dir` 内；越界抛 `ReportError`（对应退出码 1）。

### 4.8 本地控制台契约（M3）

位置 `plugins/views/ui/ui.py`（S16 起是 view 插件，`entry: ui.py:serve`，`kind: view`，`config_key: ui`）。`serve(config, store, host=None, port=None, once=False, report_name=None) -> dict`。

- 启用开关：`plugins.ui.enabled`（缺省 true）。停用 / 目录缺失 / 清单非法时 `hub-ui` 退出码 1，stderr 分别是「view 插件未启用: ui」「view 插件未安装: ui」「view 插件加载失败: ui」——**绝不静默起服务**（A15）。

| 方法 | 路由 | 语义 |
|---|---|---|
| GET | `/` | 单页控制台（系统数据只读 + 报表编辑区 + 修改记录） |
| GET | `/api/state` | 只读快照：`{system:{stats,todos,notifications}, report:{name,path,text,revisions}}` |
| POST | `/api/save` | `{report?,text,reason?}` → 人改报表（`author=human`），留痕 |
| POST | `/api/generate` | `{report?}` → 重写自动区块（`author=hub`），留痕 |
| GET | `/api/assets` | 媒体面板列表（只读）：`{kind,limit,items[{id,kind,name,size,mtime,url,thumb?}],nextCursor?}`；契约 `plugins/views/ui/README-ASSETS.md` |
| GET | `/asset/<id>` | 媒体原始字节（图片 inline / 文档 attachment；白名单 Content-Type + nosniff） |
| GET | `/thumbs/<id>` | 图片缩略图（有 PIL 现生成，无 PIL/坏图回原图） |

- 只绑 loopback（`ui.host` 必须是 loopback，否则拒绝启动）；校验 `Host` 与（若带）`Origin` 都是 loopback，`Sec-Fetch-Site: cross-site` 直接 403。
- **单线程 `HTTPServer`**：`sqlite3` 连接只能在其创建线程使用（实测 ThreadingHTTPServer 会 `ProgrammingError`）；本地单用户不需要并发。
- 请求体上限 `ui.max_body_bytes`；任何未预期异常都折成 500 JSON，不给空回复。
- `--once`：处理完一个请求就退出（验收脚本用）；启动时向 stdout 打一行 JSON `{ui,host,port,report,once}`（端口 0 时由内核分配）。

### 4.9 归纳层契约（M4b）

位置 `src/infohub/digest.py` + `src/infohub/privacy.py`。**加工层不写死在代码里，只是 `digest.command` 一个命令**（提示词走 stdin，结果走 stdout）⇒ 换模型 = 换命令。

| 函数 | 语义 |
|---|---|
| `build_input(config, store, window_days=None) -> dict` | 组装确定性输入：`{window_days, since, events[], todos[], event_total, per_source, redactions, event_ids}`；事件按时间正序，payload 先过脱敏、每条截到 400 字符 |
| `input_hash(payload) -> str` | **看内容不看时钟**：`sha256({window_days, events, todos})[:16]`（`since` 每秒都在变，不进指纹，否则同一份数据永远算新输入） |
| `render_prompt(config, payload) -> str` | 提示词；**输出契约写死在提示词里**（只输出 JSON、每条必须挂流水里出现过的事件 id） |
| `parse_output(text, valid_ids, max_items) -> {items, dropped}` | 机械审计：剥代码围栏 → 取 JSON → `evidence` 里**不在 `valid_ids` 的 id 被剔除**，剔完没证据或没正文 ⇒ 该条丢弃并计入 `dropped` |
| `run(config, store, window_days=None, force=False, dry_run=False) -> dict` | `disabled` → `dry-run` → `reused`（同一 `input_hash` 已有记录且未 `--force`）→ `ok`（落 `digests` 一行） |
| `render_block(store, config) -> str` | 报表区块 `digest` 的内容（每条带 `evidence: #id`）；只读库 |

- `privacy.redact(text) -> (text, hits)` / `redact_obj(obj) -> (obj, hits)`：出本机的文本先脱敏——键值形态凭据（`token=`/`password:`…）、URL 里的 `key=/token=/sig=`、`dsh-auth-…`、私钥块、≥32 位 hex、≥32 位 base64url。**只机械替换，不做语义判断**。
- 失败语义：`digest.enabled=false` ⇒ `reason=disabled` 退出 0；加工层命令不存在/超时/退出码非 0/输出不是 JSON ⇒ `DigestError` ⇒ 退出码 1，**不写任何 digest 行**（绝不把半截文本当归纳结果）。
- 表 `digests(id, ts, window_days, backend, prompt_hash, input_hash, output, items, dropped, elapsed_ms, error)`，`UNIQUE(input_hash)`。
- 现成后端例子：`bin/hub-digest-dsh --session <DSH 会话 id>`（把提示词丢给一个 DSH 会话，回复当归纳结果；换模型 = 换这个命令或换它指向的会话）。
- `bin/hub-digest run [--window-days N] [--force] [--dry-run]` / `show`；全局 `--json/--config/--quiet` 写在子命令**之前**。

**M4d：默认后端 = 本地确定性后端（`bin/hub-digest-local`）**

无人值守的每周归纳不能依赖「某个 LLM 会话恰好活着」，所以默认后端是本机确定性计算：不联网、零 token、
同一份输入永远同一份输出。

| 函数 | 语义 |
|---|---|
| `localdigest.build(config, store, window_days, prompt="") -> str` | 产出 `{"items":[{"text","evidence"}]}` 的 JSON 文本；**只许引用提示词里出现过的事件 id** |
| `localdigest.parse_prompt_ids(prompt) -> {id: {ts, source, kind}}` | 从上游提示词的流水行（`- id \| ts \| source/kind \| payload`）读出**允许引用的 id 集合** |

- **依据只在提示词里**：本地后端不另取一份数据，`evidence` 必须是提示词里出现过的 id —— 与 `parse_output`
  的审计同一把尺子（否则"本地后端自己产的依据"会被审计全部丢掉，等于白跑）。提示词里没有 id ⇒ `{"items": []}`。
- 待办条目优先（证据取 `store.todo_evidence`，跳过 `missing`，最多 `digest.local.evidence_per_item` 条），
  再按 source 分组给「最近 N 条事件里 X 占 M 条」的计数条目；**没有依据的条目一律不输出**（§4.6 门 8 同一条纪律）。
- `build_input` 会把 open 待办的证据事件**额外**带进提示词（`digest.todo_evidence_events` 上限，默认 40）：
  事件流水只取最新 `max_events` 条，而待办依据可能更老；不带上就会被审计当成"不在流水里"而丢掉。
- 入口 `bin/hub-digest-local [--window-days N] [--config X]`：提示词走 stdin（读掉但只用它的 id 集合），
  stdout 只出 JSON；配置非法/库不可读 ⇒ stderr + 退出码 1。
- 每周定时：`packaging/info-hub-digest.timer`（周日 23:30，早于周报 23:55）→ `bin/hub-digest --quiet run --window-days 7`。

### 4.10 周报契约（M4c）

位置 `src/infohub/week.py`；入口 `bin/hub-week`。**只读库、只写自己的文件**——周报是派生只读产物，可手改的是报表 `hub-report.md`（那份才留痕）。

| 函数 | 语义 |
|---|---|
| `week_bounds(weeks=1, week_of=None, now=None) -> (start, end)` | 本地时区 ISO 周：`start` = 目标周周一 00:00，`end = start + 7*weeks 天`；半开区间 `start <= ts < end`；`--weeks 2` 从上一周周一起算 |
| `build(store, config, weeks, week_of, now) -> dict` | 组装数据（不写盘）：事件按来源计数、新增/完成/未处理待办、弹出与失败通知、通知有效性、本周各次归纳及其条目（含 `evidence`） |
| `render(data) -> str` | 渲染 markdown：概览表 + 归纳条目（每条带 `evidence: #id`）+ 事件来源表 + 通知有效性表 |
| `write(config, data) -> Path` | 落 `reports_dir/hub-week-<ISO年>-W<周>.md` |
| `run(config, store, weeks, week_of, dry_run) -> dict` | `dry-run` 只返回 `text` 不落盘；否则 `reason=ok` + `path` |
| `list_weeks(config) -> list[dict]` | 已生成的周报文件（新的在前） |

- **文件名按区间最后一天所在的 ISO 周**：`--weeks 2` 的连报也落在当周文件，不会倒回去覆盖历史周文件。
- 每条归纳条目照样带真实事件 id；周报层不再做证据审计（M4b 已保证），只做渲染。
- `bin/hub-week`：`run [--weeks N] [--week-of YYYY-MM-DD] [--dry-run]` / `list`；全局 `--json/--config/--quiet` 写在子命令**之前**。
- 坏参数（`--week-of` 不是日期、`--weeks < 1`）⇒ `WeekError` ⇒ 退出码 1，不落盘；空库也能出周报（`reason=ok`）。

### 4.11 bilibili 账号监测（M5）

位置 `src/infohub/bili.py`（取数 + 提工具）、`src/infohub/kb.py`（只读全文检索）、
`plugins/sources/bilibili/collector.py`（来源插件）、`src/infohub/rules.py` 的 `bili-unintegrated` 规则。

**两段式：先采（把看到的条目落成事件），再对账（KB 里有没有）。**

| 函数 | 语义 |
|---|---|
| `bili.fetch(cfg) -> list[dict]` | `source=command` 跑 `command`（argv，stdout 必须是 JSON）/ `source=file` 读 `input`；失败抛 `BiliError` |
| `bili.load_items(payload)` | 接受 `{"items":[…]}` / `{"list":[…]}` / 裸数组 |
| `bili.extract_tools(items) -> list[dict]` | **只认机械形态**：`github.com/a/b`（可省 scheme，`.git` 去掉）+ `owner/repo` 形态词；裸名字**不猜**；返回 `{key,name,owner,repo,url,item_id,title,mentions}`，按 `key` 唯一 |
| `kb.text_files(root)` | 只读后缀白名单（`.md/.txt/.rst/.yaml/.yml/.json/.csv/.org`）、跳过 `SKIP_DIRS`、`MAX_FILE_BYTES=4MB`；**只读，不建索引、绝不写 KB** |
| `kb.search(root, needle, limit=5, word=False)` | 大小写不敏感；`word=True` 时两侧不得紧贴 `[A-Za-z0-9_.-]`（`/` 与空格算边界） |
| `kb.contains(root, needle, word=False) -> (bool, path)` | **fail-closed**：KB 根不可读 ⇒ 抛 `KbError`，绝不返回 `False`（否则会把已整合的工具误报成没整合） |

采集器 `bilibili`：游标 = 见过的条目 id（`collector_state['bilibili:items']`，只留最近 `keep_ids` 个，
避免无界增长）；每个新条目一条 `bilibili/bili.item` 事件（payload 见 §5.2）；来源失败 ⇒ `CollectorError`，
不影响其它采集器（§6）。

规则 `bili-unintegrated`（证据 = `bilibili` 事件）：
- 对窗口内每条事件提工具 → 先用**完整 key** `kb.contains(root, key)` 查；未命中且仓库名长度 ≥ `name_min_len`
  时再用**仓库名** `kb.contains(root, repo, word=True)` 兜底；两者都没命中才产出候选；
- 候选 `dedupe_key = "bili-unintegrated:<owner>/<repo>:<最近一条事件的 YYYY-MM-DD>"`；同一条工具的多个视频合成一条；
- KB 判定落缓存（`collector_state['kb:<key>']`，`cursor={found,at,path}`，`cache_days` 内不重复检索）；
- 关键词/阈值：`window_days`、`scan_limit`、`cache_days`、`name_min_len`、`kb_root`；
- **KB 不可读 ⇒ 抛 `RuleError`（记 error、零候选）**：宁可不提，也不提假待办。

### 4.12 浏览器历史采集（M5b）

位置 `src/infohub/browserhist.py`（读库 + 提条目）、`plugins/sources/browser-history/collector.py`（来源插件）。
**只读浏览器的库**：把 `History`（含 `-wal` / `-shm`）拷进临时目录再以 `mode=ro` 打开，读完删副本 ⇒
不锁库、不写库、不在浏览器档案目录留任何东西（判据：`tests/verify_m5b.sh` T7 比对假 `History` 的 sha256）。

| 函数 | 语义 |
|---|---|
| `browserhist.db_path(cfg) -> Path` | `db` 给了就用；否则 `~/.config/<BROWSER_DIRS[browser]>/<profile>/History`，`profile` 空 ⇒ 取 History 最新的档案目录；路径不存在 ⇒ `HistError` |
| `browserhist.read_rows(path) -> list[dict]` | 拷副本 → `SELECT url,title,last_visit_time,visit_count FROM urls` → 删副本；失败 ⇒ `HistError` |
| `browserhist.repo_item(row) -> dict \| None` | 仓库页（GitHub/GitLab）→ `{id:"repo:<owner/repo>", kind:"repo", title, url, desc, visits, ts}`；站点路径段（`github.com/settings/*`、`login/*`、`users/*`、`topics/*`、`password_reset/*`…）**不当仓库**，且 owner > 39 或 repo > 100 字符（GitHub 长度上限）直接丢 |
| `browserhist.video_item(row) -> dict \| None` | bilibili 视频页（`BV…`/`av…`）→ `kind:"video"`；标题去掉 `_哔哩哔哩_bilibili` 之类的站点尾巴 |
| `browserhist.history_item(row, title_limit=120) -> dict \| None` | **任意**一条历史 → `kind:"history"`（M8）：`{id:"visit:<sha1[:16]>", kind, title（去尾巴 + 截 title_limit 字）, url, desc:"", visits, site, purpose, ts}`；网址为空 ⇒ `None` |
| `browserhist.collect_items(*, db, since_days, max_items, kinds, now, title_limit=120) -> list[dict]` | 窗口 = `now - since_days`；同一 id 只留**最近一次**访问；`max_items` 是**每类**上限；返回顺序 = `kinds` 顺序、类内按时间倒序；**`history` 在 `kinds` 里时是兜底类**（命中它的行不再产 `repo`/`video`） |

- Chromium 时间戳是 1601 起算的**微秒**；`ts` 用访问时间（不是采集时间）⇒ 事件排序贴近真实发生顺序。
- 事件：`kind=browser.repo|browser.video|browser.history`，payload 见 §5.2，`privacy="sensitive"`（浏览器历史比其他 source 更私密，
  报表/提示词照样按既有脱敏管线走）。
- 失败语义：库不存在/不可读/SQL 坏 ⇒ `CollectorError`（退出码仍 0，错误进 `collector_state['run:browser-history']`，§6）。
- 规则侧：`bili-unintegrated` 同时扫 `bilibili` 与 `browser-history` 两个 source，`why` 会点明来源是
  「来自浏览器历史「…」」还是「来自 bilibili 条目「…」」；**M8 起历史来源先按 URL 判门**
  （`bili.GITHUB_RE` 不命中就不提工具）—— 全量历史里视频/小说标题的 `a/b` 不能当仓库（PROJECT.md §36）。

### 4.14 浏览历史分类与总结（M8）

位置 `src/infohub/history.py`（**纯函数层**：不碰库、不看时钟、不发事件）。用户口径（2026-10-16）：
「只留历史记录 + 一条总结……对所有浏览历史生效……可以做简单分类，按照用途和网址」，且**卡片不推送**。

| 函数 | 语义 |
|---|---|
| `history.PURPOSES` / `OTHER` | 16 类用途（本机服务/代码仓库/AI 服务/技术文档/博客/社区论坛/视频音频/小说阅读/学习课程/游戏/招聘求职/购物采购/网盘文件/资讯新闻/账号邮箱/搜索/其它）；认不出就落 `其它`（不猜） |
| `history.site(url) -> str` | host → 站点名（`a.b.example.co.uk` ⇒ `example.co.uk`，多段后缀表 `MULTI_TLDS`）；IP / `localhost` 原样 |
| `history.purpose(url, title="") -> str` | **host 规则 → 站点规则 → 路径规则 → `其它`**（顺序即优先级；规则表 `DOMAIN_RULES` / `PATH_RULES`） |
| `history.classify(url, title="") -> {"site","purpose"}` | 一条历史记录的分类结果 |
| `history.visit_id(url) -> "visit:<sha1[:16]>"` | 稳定 id：同一网址只算一条（与访问次数无关） |
| `history.clean_title(title, limit=120) -> str` | 去站点尾巴、折成一行、截 `limit` 字 |
| `history.one_line(ts, item) -> str` | 一条总结：`时间 · 站点 · 用途 ·「标题」` |
| `history.summarize(store, config=None, days=1, now=None, top=10) -> dict` | 窗口内历史（按 payload 的 `id` 去重）→ `{days,since,total,by_purpose,top_sites,line}`；`line` 是**一条总结** |
| `history.render_block(store, config=None, days=1) -> str` | 报表区块 markdown（一句话总结 + 用途表 + 主力站点） |

- **不产卡片、不推送**：分类与总结只进报表（区块 `history`，§4.7）与 `bin/hub-history` 的 stdout；
  通知候选里不会出现它们。
- 体积口径：每条事件 ≈ 300 字节（时间 + 网址 + 站点 + 用途 + 标题前 120 字）；`max_items` 仍是每类上限
  （默认 20000），窗口默认 180 天，与 `retention_days` 对齐。
- 验收：`tests/verify_m8.sh`（16 项：分类单元 12/12、条目字段、全量入库、幂等、总结计数、窗口、噪声门、报表区块、fail-soft、只读、CLI）。

### 4.13 静默截屏（M7）

位置 `src/infohub/screencast.py`（取帧后端）、`plugins/sources/screenshot/collector.py`（来源插件）。

**为什么不是「截图」**（真机实测，PROJECT.md §35）：`org.gnome.Shell.Screenshot` 对非授权调用者 `Access denied`；
`org.freedesktop.portal.Screenshot` 在无 app-id 的调用者下**不产出 PNG**，而且那条路有闪光动画 + 快门声（用户 2026-10-16 否掉）。
`ScreenCast` 是**视频流**，取帧无声无动画；代价是一次性的「共享屏幕」同意。

| 函数 | 语义 |
|---|---|
| `screencast.consent(config, timeout_s=None) -> dict` | CreateSession → SelectSources（`types=1` MONITOR、`multiple=false`、带旧 `restore_token`）→ Start；成功存 token，返回 `{restore_token,node_id,size,at,token_file,ok}` |
| `screencast.grab(config, out_path, timeout_s=None) -> dict` | 按 `backend` 取一帧到 `out_path`，返回 `{path,bytes,sha256,width,height,backend[,node_id,size]}`；失败 ⇒ `ScreencastError` |
| `screencast.target_size(max_width, size) -> (w,h) \| None` | 由 portal 报的流尺寸算**等比**目标尺寸：`min(max_width, 源宽)` + 按源宽高比算高；源尺寸拿不到 ⇒ `None`（调用方退回"只限宽"）。**真机教训**：只写 `width=[1,N]` 时 videoscale 会保留源高度 ⇒ 2560x1600 缩成 **1280x1600 变形**（见 PROJECT.md §35 bug 3） |
| `screencast._gst_desc(fd, node_id, max_width, quality, size=None) -> str` | 取帧管道串（单独成函数，好让验收机械断言"宽高都钉了"）：`pipewiresrc … num-buffers=1 ! videoconvert ! videoscale add-borders=true ! video/x-raw,width=W,height=H ! jpegenc quality=Q ! filesink name=sink`（`location` 用属性设，不走字符串插值） |
| `screencast.grab_screencast(config, out_path, timeout_s=None, allow_consent=True)` | portal 路：CreateSession（**本机 v5 签名 `CreateSession(a{sv})`，没有 `parent_window`**）→ SelectSources（`persist_mode=2`，带旧 token）→ Start → OpenPipeWireRemote（拿 PipeWire fd）→ `_gst_desc` 管道；无 token 且 `allow_consent=False` ⇒ **直接失败、不弹框**；返回里带 `token_from_portal`（portal 这次有没有下发新令牌） |
| `screencast.grab_command(config, out_path)` | `backend=command`：跑 `screenshot.command`，占位符 `{out}`；同时传 `$HUB_SHOT_OUT` / `$HUB_SHOT_MAX_WIDTH` / `$HUB_SHOT_QUALITY` |
| `screencast.jpeg_size(path) -> (w,h) \| None` | 从 JPEG 的 SOF 段读尺寸（不依赖 Pillow） |
| `screencast.prune(config, now=None) -> dict` | 删 mtime 早于 `keep_days` 的 `*.jpg` + 清空目录；返回 `{removed,kept,freed_bytes,keep_days}` |
| `screencast.load_token/save_token/token_path/shots_dir` | 令牌与落盘目录；`save_token` 原子写 + `0600` |

采集器（`source=screenshot`，`kind=shot.saved`）：

1. **间隔门**：`state['screenshot'].cursor.epoch` 距现在 < `interval_s` ⇒ 直接返回（无事件）。
2. **活跃门**：`require_active: true` 时先问 mutter 空闲（**复用 `notify.Env.idle_s()`**，不另造第二套探针）；
   `idle > idle_max_s` ⇒ 返回且 `cursor.last_skip="idle"`（**不推进 `epoch`**，人一回来就能取）；
   探针本身失败 ⇒ `CollectorError`（**不取帧**：探不到"人在"就当不在，fail-closed）。
3. **打字保护（④，2026-11-13 加）**：**只在上一步生效时才有**（`require_active: false` ⇒ 整段探针都不参与，照旧无条件取帧）。
   `idle_min_s > 0` 且 `idle < idle_min_s` ⇒ 判定「正在打字」，先**等一次自然停顿**：
   每 ≤0.5 秒重问一次 `idle_s()`，最多等 `pause_wait_s` 秒；等到 `idle ≥ idle_min_s` 就照常取帧，
   等不到 ⇒ 返回且 `cursor.last_skip="typing"`（`cursor.idle_s` 记当时的空闲秒数；**不推进 `epoch`**，下一轮重来）。
   这道门**探针失败时放行**（fail-open：payload 的 `idle_s=null`）—— 探不到空闲时长不等于「在打字」，不能因为探针坏掉就停采；
   **活跃门仍是 fail-closed**（第 2 步）：两处语义相反，不是笔误。
   **为什么**（用户 2026-11-13「屏幕监测会打断我的输入法」）：本机输入法候选窗由 GNOME Shell 扩展
   `kimpanel@kde.org` 渲染，而活跃门 `idle < idle_max_s` 恰好把取帧对在**「正在打字」**的时刻 ——
   一次取帧 ≈ 0.4 秒（portal 会话 + 合成器抓帧 + JPEG 编码）外加 pyatspi 全窗口遍历，
   任何瞬时扰动都表现为候选框闪一下 / 组词被打断。等到停顿时取，打字过程中镜头不动。
4. 取帧 → `screencast.grab` → `<dir>/<YYYY-MM-DD>/<HHMMSS>.jpg`（同名加 `-1`、`-2`…）。
5. 事件 payload 见 §5.2，`privacy="sensitive"`；`sha256` 是**文件内容**哈希（可对账）。
6. 失败语义：`ScreencastError` ⇒ `CollectorError` ⇒ 退出码仍 0，错误进 `collector_state['run:screenshot']`（§6）。

- 留存：`hub-screencast prune`（已挂进 `info-hub-prune.timer` 的第二条 `ExecStart`）删图；**事件元数据不动**（各按各的保留期）。
- 隐私边界：只取**屏**（MONITOR），不取窗口列表内容；文件名与元数据都在本机 `~/.dsh/info-hub/` 下，不出网。

**同意与令牌（真机实测，2026-10-16）**

- 首次 `hub-screencast consent`：屏幕弹出 `xdg-desktop-portal-gnome` 的**「共享屏幕」**对话框（截图里抓到过该窗口），用户点一次即得 `restore_token`（存 `state/screencast.json`，`0600`）。
- `persist_mode=2` **必须显式给**，否则 portal 不下发 `restore_token`（第一次同意就是这么白点的）；令牌按规范在 **Start 的 Response** 里返回（不是 SelectSources）。
- **本机 GNOME portal 复用时每次仍会回一个 `restore_token`，值不变**（实测连续 4 次取帧后落盘令牌一字未改、始终可用）⇒ 实现按"不同才写回"，`grab` 返回的 `token_from_portal` 供对账。规范说的"单次使用"在本机后端上不成立，**但代码不依赖它**：即使真换了也会写回新的。
- 无人值守的取帧（采集器）**永远不弹同意框**：没令牌就失败并把原因记进 `run:screenshot`。

### 4.15 活跃采样与片段（M10）

**`activity` 采集器**（由独立定时器驱动，**不**进 5 分钟的 `collect` 轮）：每 `interval_s` 采一条。

- 采样：`idle_s` 取 `Env().idle_s()`（mutter）；窗口集合取 `window.frames()`；`active = idle_s < behavior.idle_active_s`。
- **在场判定在派生层可重算**：payload 必须留 `idle_s`，片段层用当前 `idle_active_s` 现算 `active`；
  只有老事件缺 `idle_s` 时才回退读 payload 的 `active`。调门槛 = 重算（不换 `def_version`），改公式才换号。
- `frame_hash` = **应用名集合**的哈希（排序后，不含标题）：标题常带计时器（视频进度、字数、秒数），
  算进去会把「根本没换窗口」记成一次次切换，把专注度算成 0（实现在 `src/infohub/activity.py` 的 `app_hash`；窗口枚举由本体环境能力 `src/infohub/atspi.py` 提供）。
- 落库条件（**必须**，否则一天 1440 条白占库）：`active` 为真；或上一条是 `active`（在场边界，否则算不出结束时刻）；或距上一条 ≥ `heartbeat_s`（离开时心跳）。
- 失败语义：探针失败 ⇒ `CollectorError`，记 `run:activity`，**不落半条**。「不在场」是正常采样（`active=false`），「探不到」是失败 —— 两者必须可区分。
- `ts` 用观察时刻（同 `clipboard`/`window`）；`privacy=normal`（只留应用名与哈希，不留标题正文）。

**手机游戏排除（M18 口径，落在 `hub-activity day` 的一行里）**：把每段按小时切，取该小时
`features.phone_game_min`，重叠分钟 × `min(1, game_min/60)` 从在场分钟里扣掉；扣完给「电脑上净时间」。
超过 `behavior.phone_game_exclude_min`（默认 30）就按实际比例扣，不做二值判断 —— 挂 5 分钟不该抹掉一小时。

**`episodes` 转译层**（`src/infohub/behavior.py`，纯函数，可从 `events` 全量重算）：

- 段 = 相邻 `activity.sample` 序列，相邻间隔 > `behavior.gap_s` 即断段；`active=false` 的采样直接收段。
- `active_s` = 在场采样数 × `interval_s`；`span_s` = 末采样 − 首采样 + `interval_s`；`presence = active_s / span_s`。
- `switch_count` = 段内相邻采样 `frame_hash` 变化次数；`gap_max_s` = 段内相邻采样最大间隔。
- `stability = 1 − min(1, switch_count / (span_min × behavior.switch_per_min_full))`。
- `continuity = 1 − min(1, gap_max_s / max(behavior.gap_s, span_s × 0.1))`。
  **2026-10-28（ep-3）改分母**：原来固定用 `gap_s`（300 秒）作分母，一段 3 小时里只要起身一次 5 分钟，
  连续性就直接归零、该段专注分被压到 70 以下 —— 与「专注」的含义相反。改成本段长度的一成与 `gap_s` 取大者，
  同样一次 5 分钟起身，在 3 小时的段里只扣一点点。真机验证：10-24 高专注时间从 10 分钟回到 211 分钟。
- `focus = round(100 × Σ wᵢ·分量ᵢ)`，权重取 `behavior.focus_weights`。
- `active_s < behavior.min_active_s` 的段**不落库**；每段带 `def_version`。
- 幂等：`UNIQUE(day, started_at, def_version)`；重建 = 先删该 day 该版本再算 ⇒ 同一输入跑两次结果逐字节相同。
- `hub-rules derive` 每轮结束顺带重算当天与前一天的片段（幂等；某天无采样 ⇒ 0 段），不新增定时器。

### 4.16 画面语义（M11）

- **定位**：M7 已经每 5 分钟存一张 JPG（`events(source='screenshot')` 带 `sha256/path`）。本层只对这些图做 OCR，
  **不取帧、不截图、不改 screenshot 的任何边界**。
- **幂等键**：`sha256`。同一个画面（包括同一张图被再次扫到）只读一次，标签按 `INSERT OR REPLACE` 写。
- **判定**：词表子串命中计数（词表在 `src/infohub/vision_keywords.py`，是数据不是逻辑）；
  命中数最多的类别胜出；**最多命中数 < `vision.min_hits` ⇒ `unknown`**（宁可没有，不许乱猜）；
  并列时按词表声明顺序取第一个，保证同输入同输出。`score = n / (n + 2)`（0–1，仅表示「有多少词支撑」，不是概率）。
- **失败语义**：引擎缺失（缺 rapidocr）⇒ 本轮 `error` 字段 + 不写任何行，**derive 继续**；单张读失败 ⇒ 记 `method=missing|error:*` 的 `unknown` 行，不重试。
- **隐私**：默认 `keep_raw=false` ⇒ 只落 `category/score/hits/text_sha/chars/lines`，**原文不落库**；
  打开 `keep_raw` 才写 `shot_text.text`，并按 `retention_text_days` 过期（`hub-store prune` 清）。
  标签表与原文表都不进 `--json` 的默认输出之外的地方，不上传、不出本机。
- **版本**：`version` 列记算法版本（当前 `ocr-2`）。改分类算法/取词逻辑必须换版本；换词表不用换（词表是数据）。
- **加权**：弱词 1 分、强词 2 分（`vision_keywords.STRONG`）—— 浏览器标签栏里的应用名（淘宝/bilibili）会出现在几乎每张图上，
  只值 1 分，单靠它判不出类别 ⇒ `unknown`。这条是 v1 把 3 张桌面误判成 `chat` 之后加的。
- **`agent` 类别**：本机主界面就是 DeepSeek GUI（`127.0.0.1:3080`），它本身长得像聊天窗口，
  会被误判成社交 `chat`；单列 `agent` 类（强词：`127.0.0.1:3080 / deepseek harn / 智能体 / 上下文编队`）。

### 4.17 画面粗判（M12，工作 / 玩）

- **定位**：§4.16 是「细分类别」（十类、词表规则、可重算）；本节是「粗判两类」（工作 / 玩），由本地小 VLM
  直接看图给。**两套并存、互不覆盖**：`shot_labels` 记细类，`shot_verdicts` 记粗判。
- **为什么要模型**：词表分得清「bilibili 页面」，分不清「这个视频是教程还是娱乐」；画面整体布局（IDE 三栏 /
  视频全屏 / 游戏画面）是 VLM 的强项。**为什么不更大**：本机要 1 秒级出一张，0.6B 才够快。
- **输入**：M7 已存 JPG ⇒ **先缩到 `vision.vlm.max_width`（默认 512）再喂**（1280×800 原图无收益、慢约 4 倍）；
  缩图写进 `state_dir` 的临时文件，跑完即删，**不新增任何持久图片**。
- **提示词**：固定两行，要求只回一个词：`工作` 或 `玩`；解码用贪心（`--temp 0`）保证同图同输出。
- **类别可扩**：`vision_keywords.VERDICTS`（`{"work": [同义词...], "play": [...]}`）是**数据不是逻辑**；
  以后加「学习 / 会议」只改这张表 + 换 `version`，不改代码。
- **判定**：模型输出按同义词表映射；**表外词一律 `unknown`，不许猜**；`raw` 记模型原话。
- **失败语义**：二进制或模型缺失 ⇒ 结果带 `error`、**零写入**，`derive` 继续；单张超时/崩溃 ⇒
  `method=error:<类型>` 的 `unknown` 行，不重试；`enabled=false` ⇒ 整个阶段跳过。
- **幂等**：PK = `(sha256, version)`。同一版本重跑不重复写；**换模型/换版本 ⇒ 新行**，两版可并存对比。
- **隐私**：缩图临时文件只在 `state_dir`，跑完删；模型原话只在 `raw` 一列（不含原文全文）。
- **实测**（2026-10-24，本机 32 核 / 31 G）：`Qwen3-VL-2B-Instruct-Q4_K_M` + `mmproj-F16`，512 宽缩图，
  2 线程、`-c 512` ⇒ **8.6–12 秒/张**（竖屏 1280×1600 缩到 512×640 要 18 秒），峰值 RSS **2.8 GB**；
  4 张标定全对（bilibili 视频/淘宝 ⇒ 玩；代码编辑器/DeepSeek GUI ⇒ 工作）。
  0.6B 那一档（`SmolVLM2-500M`，峰值 ~0.6 G、5 秒/张）**判错过娱乐视频**，所以默认用 2B：
  这条任务的瓶颈是判断力不是速度 —— 快而错没有意义。

### 4.18 输入节奏（M13）

- **定位**：M10 只能判「在场/离开」；本节补「手上在动、动得多不多」。文献里这是**唯一有硬指标**的
  专注度相关信号（键盘节奏：3 档疲劳识别 98.11%，见 PROJECT.md 的出处）。
- **读什么**：`/dev/input/event*` 的 `input_event` 流（24 字节：timeval + type + code + value）。
  只累加计数：`EV_KEY` 且 `value==1`（按下，忽略抬起与自动重复）⇒ `keys`；按键码 0x110/0x111/0x112 ⇒ `clicks`；
  `EV_REL REL_WHEEL` ⇒ `wheel`；`EV_REL REL_X/REL_Y` 累加后取欧氏距离 ⇒ `dist_px`。
- **不读什么（硬边界）**：不记 keycode、不记时长序列、不记任何文本；payload 里**永远不会出现按键码**，
  验收脚本专门断言这一点。想看「打了什么字」是不可能的，本层结构上就做不到。
- **M16 键盘节奏**：允许在进程内算「按键间隔的中位数/变异/停顿数/连击长度/退格比」这**五个聚合值**
  （见 §5.12）。这是文献里 98.11% 那一路真正吃的特征，而它不需要存任何按键内容。
  连击段与退格比在内存里用到 code，但 code 不进 payload、不进日志。
- **节流**：一个窗口一条事件（默认 60 秒）。`listen` 是长驻服务；关掉用 `collect.input.enabled=false`
  （服务会自己退出 0，不会反复重启 —— `Restart=on-failure`）。
- **失败语义**：没权限（不在 input 组）⇒ `probe` 列 0 个设备、`sample/listen` 退出码 1，
  **不写半条**；单个设备打不开就跳过它（其余继续），不整体失败。
- **隐私**：只数不读；数据不出本机；不进 `--json` 之外的任何通道。

### 4.19 在座状态（M14）

- **定位**：M10 只知道「键鼠在不在动」，坐在那儿看屏幕不动 ≠ 走神；本节补「人在不在、脸朝不朝屏幕」。
- **引擎**：YuNet（232 KB，`models/yunet-2023mar.onnx`），OpenCV `FaceDetectorYN`，**12 ms/帧**。
  不用 2B VLM：便宜两个数量级，而且**摄像头画面根本不出本机内存**。
- **三档**：`away`（没检测到脸）/ `present`（有脸但不满足「正对且够近」）/ `facing`（正对屏幕）。
  判定：最大那张脸，`脸宽/帧宽 ≥ min_face_ratio` **且** `|鼻尖相对两眼中点的偏移|/眼距 ≤ yaw_max`。
- **取帧**：`hub-activity sample` 那一分钟顺带取一帧（不新增定时器）。**判完立刻释放，不落盘、不落库、不压缩、不上传。**
- **失败语义**：摄像头打不开/模型缺失/取帧失败 ⇒ 只跳过这一路，活动采样照常落库，退出码不受影响。
- **隐私**：不做表情、不做情绪、不做身份；同一张脸不建 ID、跨帧不关联。payload 里没有任何图像数据。

### 4.20 校准（M14）

- **为什么**：三路特征的判别力都是「猜的相关性」（文献 + 我的判断）。只有跟自报对齐过才知道真假。
- **做法**：`hub-calib rate` 记一次自报；`report` 对每条自报取前 `window_min` 分钟窗口，算
  `keys/clicks/dist/straightness/presence/facing` 六路特征，再算「专注 vs 不专注」的 AUC。
- **口径**：`label=1` 只代表「第 1 档（专注）」，2/3 都算负类；样本 <6 条只提示不判读。
- **结论怎么用**：AUC ≥0.65 的进专注度主链路；≤0.35 看是否反向（例如「按键越多越不专注」）；中间的不进。

### 4.21 验证码脱敏（M15）

- **为什么**：验证码没有信息量，却是一条**活凭据**。用户原话：「对验证码过滤，验证码没有信息量」。
  目标不是不记通知，而是**记事实不记秘密**：`source=phone-hub` 的正文落库前整段换成占位符，
  只留「这是验证码类」这个事实（`payload.redact`，字段见 §5.14）。
- **在哪一层**：`phone_hub.collect()` 造事件**之前**（`insert_events` 之前）对 payload 调
  `redact.apply(payload, config)` ⇒ 新行的 `fingerprint` 是**脱敏后**正文算的。
  `redact.enabled=false` ⇒ 完全跳过：payload 逐字节等于旧行为，也不加 `redact` 字段。
- **规则**（`src/infohub/redact.py`，`find(text) -> (kind, span)` 与 `apply(payload) -> (payload, 摘要)` 都是纯函数；
  词表是**数据**（同 `vision_keywords.py` 的定位），可调参数只读 `redact.*`）：

| kind | 判据 | 例子 |
|---|---|---|
| `cn-code` | 中文验证码字样（验证码／校验码／动态码／动态密码／短信登录／一次性密码）+ 一串像验证码的数字，两者间隔 ≤ `window_chars` | `验证码：631234`、`G-920234 是您的 Google 验证码` |
| `en-code` | `verification code` / `\botp\b` / `one-time password\|passcode` + 同上 | `Your verification code is 192837` |
| `cred` | `password\|passwd\|pwd\|token\|密码\|口令` **后面紧跟**一段数字/字母数字串且含数字 | `password=abc1234`、`密码：123456` |
| `cn-keyword` | 只有中文字样、**没有**数字串（弱证据） | `您的验证码已过期` |
| `en-keyword` | 只有英文字样、没有数字串（弱证据） | `Your OTP has expired` |

- **误杀口径**（强/弱分开；一句话记法：**有数字串就不是聊天，没数字串就没有秘密**）：
  强证据（`*-code` / `cred`）**不看来源应用**一律脱敏 —— 真码泄漏比误杀一条通知更糟；
  弱证据只在**短信 App** 上脱敏（`pkg`/`app` 命中 `redact.py` 里的内置白名单，`pkg` 缺失时从 `key` 的第二段取），
  群聊/视频标题/购物 App 里提一句「验证码」而正文没有数字串 ⇒ **一字不动**。
  实测反例（真库里的原话）：`苏妈神了！AMD送免费token！可使用…`、`大肥鱼向用户索要token`
  都**不**脱敏 —— `token` 后面没有值，只是话题词；`耗10亿token建造` 同理（数字在字样前面）。
- **写入什么**：命中 ⇒ 返回 payload 的**浅拷贝**：`text` = `redact.placeholder`，其余字段
  （`ts`/`pkg`/`app`/`title`/`key`）原样保留，并新增 `redact` 字段；未命中 ⇒ 原样拷贝、**不加** `redact`。
  只换 `text`：`title` 是发信人/应用名（"阅文集团"/"短信"），不含凭据。
- **幂等**：`text` 已经等于占位符、或 payload 已带 `redact.hit=true` ⇒ 直接放行
  （占位符本身含「验证码」三字，只靠词形会二次命中）。历史清洗连跑两次，第二次 `hit=0`（§8 M15）。
- **失败语义**：`redact.mode` 不是 `mask` ⇒ `apply` 抛 `ValueError`，该采集器这一轮失败（§6「单个采集器抛错」），
  **不落任何半条**；脱敏本身不做 IO、不联网。
- **隐私边界**：脱敏在**本机落库前**做 —— ECS 中转（手机→服务器）仍可能留原文，本层只管本机 `hub.db`。

### 4.22 行为特征层（M16）

- **定位**：这是 m00113 的落点 —— 原始事件 → 一小时的「人」的指标。前面 M10–M15 都是原料，本层才是成品。
- **输出**：`features` 表，每小时一行，字段见 §5.15。**所有列都是可重算的**（纯函数 + 事件库），换口径只需 `feat_version` 换号。
- **信号来源与诚实标注**：
  | 列 | 来源 | 可信度 |
  |---|---|---|
  | `presence_ratio` | activity 采样（mutter idle） | 实测 |
  | `facing_ratio` | camera 采样（YuNet） | 实测（只判朝向，不判情绪） |
  | `switch_count` | window 快照的应用集合变化次数 | **下界**（Wayland 拿不到焦点，只能看集合变化） |
  | `keys/clicks/dist/iki/pauses/bursts` | input 采样 | 实测 |
  | `phone_notif_min` | phone-hub 通知密度推断的手机使用分钟 | **下界估计**（通知少 ≠ 没用手机） |
  | `vision_cat` | 截屏语义标签众数 | 实测（覆盖率约 64%，其余算 unknown） |
  | `focus` | episodes 的专注度 | 三因子加权，非心理学判断 |
- **没有的**：真实「活跃窗口归属」（需要 GNOME Shell 扩展，Wayland 下 DBus/AT-SPI 都不给）、
  真实「手机屏幕使用时间」（需要 adb 无线调试配对或手机端上报）。
- **失败语义**：某路当天没数据 ⇒ 该列写 `NULL` 并在 `coverage` 位图里标出，**不填 0 冒充测量值**。
- **隐私**：特征行里没有人脸、没有通知正文、没有 keycode；`vision_cat` 只存类别名。

### 4.23 手机屏幕使用时间（M17）

**定位**：`dumpsys usagestats` 的**日桶**（第一个 `timeRange=` 聚合块；其后的周/月/年桶不要）
是 Android 自己算好的「每个 app 今天被看见多久」，不用我们猜前台、不用无障碍。
对「在手机上的时间」它比通知推断（M16 的 `phone_notif_min`）准得多，是那个下界指标的上界校准。

**取数**：`collect.phone-usage.adb`（默认 PATH 里的 `adb`）`-s <serial> shell dumpsys usagestats`，
一次读全部（约 2 MB / 1 万行），在内存里只解析日桶，不落原始文本。

**解析**（必须机械）：定位 `user=0` 之后第一个 `timeRange=` 行记为桶标签；块内每行取
`package=(\S+)` / `totalTimeUsed="([^"]*)"` / `totalTimeVisible="([^"]*)"`；
时长串跟 `DateUtils.formatElapsedTime` 一致：**两段＝`分:秒`**（`"53:31"` 是 53 分 31 秒）、**三段＝`时:分:秒`**（`"2:21:39"`）；换算成分钟时两段要按 `m + s/60`，不能当 `h*60+m`；
`totalTimeVisible` 是判定依据（`totalTimeUsed` 只作参考），缺字段的行跳过、不猜。

**节流**：`interval_s`（默认 1800）—— 屏幕时长是**累计量**，采太勤只是重复同一条信息；
落库时间戳取观察时刻（同 window/clipboard），`day` 取日桶里「结束时刻」的本地日期。

**失败语义**：adb 不在 PATH / 无设备 / 多条设备而 `serial` 为空 / 命令超时或非 0 ⇒ 抛 `CollectorError`，
**不落半条**；本采集器不重试、不冷却（由 `hub-collect` 的统一兜底与游标节流负责），
因为它的失败原因几乎都是「线没插」，重试没意义。

**隐私**：payload 只有包名与分钟数，没有窗口标题、没有通知正文；`top_n` 之外的应用**不落库**。

**M18 逐小时游戏时段（同一份 dump，不额外取数）**：`usagestats` 的「Last 24 hour events」段有
`ACTIVITY_RESUMED` / `ACTIVITY_PAUSED`，按包配对就得到「某个 app 什么时候在前台」。
只保留 `games` 里列的包，把每段按小时切分累加，落一条 `phone.usage.hours` 事件：
```
{"day":"2026-10-28","games":["kajfosz.antimatterdimensions"],
 "hours":{"2026-10-27T14":{"kajfosz.antimatterdimensions":41.0}}}
```
用途：**把人「坐在电脑前但没在跟 agent 干活」的时段，判成「在手机上玩」还是「在干别的」**。
注意事件段只有最近 24 小时，所以这份时间线也只覆盖最近 24 小时。

**为什么不做常驻**：`enabled` 默认 false —— 手机不插线时它必然失败，混进 5 分钟 collect 轮只会每轮报错。

### 4.24 QQ 消息镜像（2026-11-18 定型：OneBot v11 事件 spool）

数据源是家庭机 `<bridge-host>:/opt/qq-relay/queue/events.jsonl` —— 每行一条 OneBot v11 事件（JSON），
由 `onebot-tap.service`（`/opt/qq-relay/onebot-tap.mjs`）从 SnowLuma 的 ws 服务
`ws://127.0.0.1:3001` 实时跟写。SnowLuma 以注入方式挂在**真实 QQ 客户端**（`/opt/QQ`，账号「示例账号」，
uin 10001）上，消息来源是 QQ 协议本身，因此**有群名、发送者、消息段**，不再只是通知栏那六条。

- **过滤**：只看 `post_type=message`（meta/notice 丢弃）；默认跳过自己发的（`user_id == self_id`），
  `include_self: true` 才收。**不按群/好友白名单过滤**（那属于派生层的事）。
- **读取**：ssh 到 `collect.qq.ssh_host`（默认 `root@<bridge-host>`）执行 `remote_cmd`（默认一段
  `python3 -c`：按 offset 读至多 max_bytes 字节、返回 base64 + 文件大小）。本地只吃**完整行**
  （末尾无换行的那半行留到下轮），不 scp 整文件、不写远端。文件被轮转（size < offset）⇒ 从 0 重读。
- **增量轴**：字节游标 `collector_state.cursor.offset`；去重键 = `qq:<message_id>`
  （`seen_keys` 只留最近 `seen_keys_max` 个）。轮转/重放时靠 seen_keys 兜底。
- **事件映射**：事件 `time`（秒）→ `payload.ts`（毫秒）；事件 ts 走 `E.iso()`（ISO8601 带时区）。
  `payload` 六键与 phone-hub 同形：`app="qq"`、`pkg="qq.onebot"`、`title`=群名（群）或发送者 card/nickname（私聊）、
  `text`=消息段拼出的可读正文（`[图片]`/`[表情]`/`[语音]`/`[视频]`/`[文件]`/`[聊天记录]`/`[卡片]` 占位，
  `reply` 段忽略，`at` 段渲染 `@uin`）、`key`=去重键。`kind=notify`、`privacy=normal`；脱敏（§4.21）先行。
- **失效语义**：spool 不可读 / 行不是 JSON 对象 / 缺 `message_id`/`time` / ssh 超时 ⇒ `raise CollectorError`，
  宿主记 `sources.qq.ok=false` + `fail_streak`，采集轮退出码仍 `0`（§6）。恢复后 `fail_streak` 归 0。
- **测试注入**：`collect.qq.ssh_host` 置空 ⇒ 本机跑同一条远端命令，`spool_path` 指向临时 fixture 即可。
- **旧路径（已废）**：读 `/opt/qq-relay/qq_ingest.db` 的通知镜像自 2026-10-10 起零新增（手机侧上传配置已不存在），
  代码已删除；取证与选型见 `docs/qq-source-recon.md`、`docs/qq-source-verify.md`。

## 5. 数据契约

### 5.1 `events`（append-only，**人不可改、机器不可改**）

| 列 | 类型 | 语义 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `ts` | TEXT | 事件发生时间，ISO8601 带时区。**各 source 的取法**：`dsh-session` 用存档里的 `time`（ms→ISO）；`clipboard`/`window` 用观察时刻（秒）；`fs` 用文件 mtime |
| `source` | TEXT | §4 的 name |
| `kind` | TEXT | §4 的 kind |
| `payload` | TEXT | JSON 对象；各 source 的字段见 §5.2 |
| `fingerprint` | TEXT | `sha256(source + "\x1f" + kind + "\x1f" + canonical_json(payload))` 的前 16 位 hex |
| `privacy` | TEXT | `normal` / `sensitive`（含窗口标题、剪贴板正文时为 `sensitive`） |
| `ingested_at` | TEXT | 入库时刻 |

唯一索引 `UNIQUE(source, fingerprint, ts)` = 去重键。**注意**：`clipboard`/`window` 的 `ts` 随观察时刻移动，它们的重复抑制靠游标哈希；`dsh-session`/`fs` 的 `ts` 是确定值，去重键可直接生效。

- **唯一的 payload 例外（M15）**：验证码脱敏的历史清洗只 `UPDATE events.payload`（§5.14），
  `ts` / `source` / `kind` / `fingerprint` / `privacy` / `ingested_at` **一律不动**，**绝不 DELETE 行**。
  所以老行的 `fingerprint` 是**脱敏前**原文的哈希 —— 它只用于入库去重，清洗后保持不变；
  新采集的行指纹本来就是脱敏后算的。这样同一 `ts` 的两条不同验证码脱敏后正文相同，也不会撞唯一索引。

### 5.2 `payload` 逐 source

```jsonc
// dsh-session
{"session_id":"…","seq":12,"cwd":"…","event_type":"assistant/message","head":"前 200 字（可能为空）"}

// clipboard
{"head":"前 N 字（N 见配置）","chars":1234,"truncated":false}

// window
{"frames":[{"app":"ptyxis","title":"…"}],"count":3}

// fs
{"root":"/abs/root","path":"相对路径","mtime":1760500000.0,"size":123}

// bilibili
{"id":"BV1…","title":"…","url":"https://www.bilibili.com/video/BV1…","desc":"前 200 字","source_kind":"favorite"}

// screenshot（M7；privacy=sensitive）—— `idle_s` 是取帧那一刻的空闲秒数；打字保护探针失败时放行 ⇒ `null`（§4.13 第 3 步）
{"path":"/abs/…/screens/2026-10-16/112233.jpg","rel":"screens/2026-10-16/112233.jpg","bytes":189234,
 "sha256":"前 16 位 hex","width":1280,"height":720,"idle_s":3.2,"backend":"screencast",
 "app":"ptyxis","title":"活动窗口标题（前 120 字，可能为 null）"}

// activity（M10；privacy=normal）—— 只留应用名与窗口集合哈希，不留标题正文
{"idle_s":54.0,"active":true,"frame_hash":"3f3a16e306bb","frame_count":2,"apps":["gnome-shell","ptyxis"]}

// browser-history（M5b；privacy=sensitive）
{"id":"repo:owner/name","kind":"repo","title":"owner/name","url":"https://github.com/owner/name","desc":"历史里的页面标题（前 200 字）","visits":3,"source_kind":"history","site":"","purpose":""}

// browser-history 全量类（M8；privacy=sensitive）—— 每条历史一条事件
{"id":"visit:9f2c…（sha1 前 16 位）","kind":"history","title":"标题前 120 字（已去站点尾巴）",
 "url":"https://www.bilibili.com/video/BV1xx411c7mD","desc":"","site":"bilibili.com",
 "purpose":"视频/音频","visits":5,"source_kind":"history"}

// phone-hub（M15；kind=notify，payload 是 ECS hub_dump 那一行的镜像；privacy=normal）
{"ts":1760000000000,"pkg":"com.android.mms","app":"短信","title":"深度求索",
 "key":"0|com.android.mms|-1234567|null|10322",
 "text":"[验证码类]","redact":{"hit":true,"kind":"cn-code","version":"rd-1"}}
```
未命中时 payload 就是 `{ts,pkg,app,title,text,key}` 六个字段，**没有** `redact`（字段与命令见 §5.14）。

```jsonc
// qq（2026-11-18；kind=notify，payload 与 phone-hub 同形，数据源是 qq-relay 的 QQ 通知行）
{"ts":1760105102000,"pkg":"com.tencent.mobileqq","app":"QQ","title":"示例群(7条新消息)",
 "key":"0|com.tencent.mobileqq|514|null|10323","text":"某人: 第一条示例消息"}
// 命中脱敏时同样多一个 redact 对象（先脱敏再算指纹，与 phone-hub 一致）
```

`phone.usage.hours`（M18，逐小时游戏在前台分钟）：见 §4.23 末尾。

`phone.usage`（M17）：
```
{"day":"2026-10-28","bucket":"2026/10/27 17:04 – 2026/10/28 10:41","total_min":1226.0,
 "count":178,"packages":[{"app":"kajfosz.antimatterdimensions","min":883.1}, …最多 top_n 个]}
```
`total_min` 是日桶内全部应用 `totalTimeVisible` 之和 —— 它是**「各应用被看见的时间之和」**，不是「屏幕点亮时长」：
分屏/浮窗会让它偏大（实测桶跨 17.8 小时、合计 20.5 小时是正常的），所以只用来比趋势，不当作息依据。

### 5.3 其余表（M0 建表、后续期填）

| 表 | 用途 | 谁写 |
|---|---|---|
| `collector_state` | 采集器游标（PK = `source[:key]`）+ 最近成功/失败 | 采集器 |
| `todos` | 待办（M1） | 机器，人可覆盖 |
| `report_revisions` | 报表修订（M3） | 人（经 UI） |
| `notifications` | 通知台账（M2） | 通知器 |
| `episodes` | 行为片段（M10，派生层，可从 `events` 重算） | `bin/hub-activity episodes` |
| `shot_text` | 截屏 OCR 元数据（M11；`keep_raw=true` 才有原文） | `bin/hub-vision run` |
| `shot_labels` | 截屏类别标签（M11，派生层，可从原图重算） | `bin/hub-vision run` |

### 5.4 `todos` 与看板指针（M1）

- 规则产物写 `todos`（L2）：`dedupe_key` 唯一；`human_override=1` = 人改过状态，规则引擎不得覆盖。
- `board_card_id` 非空即"已建卡"；`board-sync` 只处理 `status=open AND board_card_id IS NULL`（幂等判据）。
- 出口默认关：`board.enabled=false`，打开后 `hub-rules board-sync` 才发请求。协议见 skill `dsh-task-board`（`POST /api/task-board/action`，`Origin` 必带，正文进 `prompt` 不进 `description`）。

### 5.5 `todos.notified_at` 与 `notifications` 台账（M2）

- `todos.notified_at`：NULL = 还没提示过（候选判据）；非空 = 已提示。列级迁移补列，不重建表。
- `notifications`：`{id, ts, kind, body, basis_event_ids, shown, action, dbus_id, todo_id}`；`shown=1` 才算"真弹出去"，节流三道只数它。
- `kind` = 待办 `dedupe_key` 的前缀（规则名），因此"同类每天上限"按规则生效。
- `collector_state['notify:ui']` 的 `cursor.event_id` = 已评估过的最新 `window` 事件 id。

- `notifications.dbus_id`（M2b）：系统通知 id（`gdbus` 后端才有）。动作回调按它回查"点的是哪条"。
- `notifications.action`（M2b/M2d）：被点的按钮的**稳定键**（`acted`/`ignore`/`ignore-kind`/`open`/`snooze`）；发送时为空，`act()` 落键。
- `todos.snooze_until`（M2d）：「稍后」把这条推到该时刻之后（ISO+08:00）；`set_todo_snooze()` 同时清 `notified_at` ⇒ 到点后同一条会**再**进候选；状态仍是 `open`。
  有效性（M4a）**不读这一列**，它按「待办是否 `done` + 时间」判 `acted`，这一列只作留痕/对账。

### 5.6 `kind_suppressions`（类别抑制，M2c）

- `kind`（主键，= 规则名）/ `until`（到期时间，ISO+08:00）/ `note` / `source`（`notify-action` = 人点按钮、
  `cli` = 命令行、`report` = 预留给 M3 报表侧）/ `created_at` / `updated_at`。
- 门 6 读法：`effective_suppressions(store) -> set[str]`（只回 `until > now` 的类别）。
- 与 `todos.status='dropped'` 的区别：`dropped` 管**这一条**，`kind_suppressions` 管**这一类**；
  两者独立，`unsuppress` **不会**把已 `dropped` 的待办翻回 `open`。

### 5.7 `todo_revisions`（清单改动记录，M3b）

- 列：`{id, todo_id, at, author, field, before, after, reason}`；`field` ∈ `status` / `snooze_until`。
- **谁写**：`set_todo_status(..., human=True|False)` 与 `set_todo_snooze(...)`。`author` = `human`（控制台 / CLI / 通知按钮）或 `hub`（规则引擎）。
- **幂等**：值没变 ⇒ **不写记录**（规则每次重跑不会刷出一堆"open→open"）。
- 人改过（`human_override=1`）的待办，规则引擎不得覆盖；这条与"有没有记录"分开断言（T4）。
- 与 `report_revisions` 的关系：**两张账本分离** —— 改清单不写报表账本，反之亦然（T9）。
- 入口：控制台 `/api/todo`、CLI `hub-store todo-set`（`reason=cli`）、通知按钮（`reason=notify-action`）、
  只读查看 `hub-store todo-revisions [<id>] [--limit N]`。

### 5.8 证据链（M3c）

**口径**：每条待办都必须能**摊开成它赖以产生的原始事件**；摊不开的，就不许变成一条通知。
这一节同时是门 8（§4.6）的数据契约 —— 通知的 `basis_event_ids` 与待办的 `source_event_ids` 是同一份依据的两种呈现。

- 存储面：证据**不新建表**，就是 `todos.source_event_ids`（JSON 数组，见 §5.4）。产出侧唯一写入者是规则引擎
  （`rules._candidate(...)` 必填 `event_ids`），所以"有没有依据"在产出那一刻就定了。
- 读接口（两个都**纯只读**）：
  - `store.todo_evidence(todo_id, limit=20) -> list[dict]` —— 按 `source_event_ids` 的顺序逐条回 `events` 行
    （`{id, ts, source, kind, payload, privacy, missing:false}`）。
  - `store.todo_detail(todo_id, evidence_limit=20, revision_limit=20) -> dict | None` ——
    `{todo, evidence, revisions}`；id 不存在 ⇒ `None`。
- **空洞如实暴露**：`source_event_ids` 里的 id 在 `events` 查不到（被 `prune` 清掉、或种子/人手写进了假 id）⇒
  该条回 `{id: <原 id>, missing: true}`，**不静默丢弃、不假装有依据**。证据链出现空洞本身就是该被看见的信号。
- **只读**：摊开证据不动 `events` / `todos` / `todo_revisions` 一个字节（`verify_m3c.sh` T7 断言）。
- 人看的三个出口：控制台「依据」按钮（`/api/todo/detail`）、CLI `hub-store todo <id>`、通知正文的「依据 N 条事件」。
- 门 8 的判据：候选的 `source_event_ids` 为空 ⇒ `reason=no-evidence`，**不弹**；且它是 `continue` 不是 `break`，
  别的候选不受影响（§4.6）。
- 与 M4b 归纳层的关系：归纳条目的 `evidence` 走**另一条**审计（`digest.parse_output` 把不在本次输入里的 id 剔掉，见 §4.9），
  两条路径都遵守同一条铁律 —— **"看起来有依据"不算数，id 必须真在库里**。

### 5.9 `episodes`（行为片段，M10）

| 列 | 类型 | 语义 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `day` | TEXT | 本地日期 `YYYY-MM-DD`（按 `started_at` 所在日） |
| `started_at` / `ended_at` | TEXT | ISO8601 带时区 |
| `active_s` / `span_s` | INTEGER | 在场秒数 / 跨度秒数 |
| `presence` / `stability` / `continuity` | REAL | 三因子，各 0–1（公式见 §4.15） |
| `focus` | INTEGER | 0–100。**不是"专注"的心理学判断**，只是上列三个可算量的加权 |
| `switch_count` / `gap_max_s` / `sample_count` | INTEGER | 窗口切换次数 / 最大空档秒 / 采样条数 |
| `def_version` | TEXT | 口径版本（§3 `behavior.def_version`）；改公式必须换号 |

唯一键 `UNIQUE(day, started_at, def_version)`。**谁写**：`bin/hub-activity episodes`（派生层；可从 `events` 全量重算，`events` 永不因它改动）。
保留策略：不受 `retention_days` 影响（它是聚合值，不含正文）；`hub-store prune` 只删 `events`。

### 5.10 `shot_text` / `shot_labels`（画面语义，M11）

`shot_text`（每张图一行，PK = 图的 `sha256`）：

| 列 | 类型 | 语义 |
|---|---|---|
| `sha256` | TEXT PK | 图的指纹（就是 `screenshot` payload 里那个，幂等键） |
| `ts` | TEXT | 图的时间（沿用事件的 `ts`） |
| `chars` / `lines` | INTEGER | OCR 文本字符数 / 行数 |
| `text` | TEXT | **原文**；仅 `vision.keep_raw=true` 时非空，否则 NULL |
| `text_sha` | TEXT | 文本指纹（16 位），用来在不存原文时比对「同一段文字」 |
| `engine` / `version` | TEXT | OCR 引擎名 / 算法版本（`ocr-1`） |
| `created_at` | TEXT | 处理时刻（保留期按它算） |

`shot_labels`（每张图一行，PK = `sha256`）：

| 列 | 类型 | 语义 |
|---|---|---|
| `sha256` / `ts` | TEXT | 同上 |
| `category` | TEXT | `code/terminal/doc/chat/video/shop/mail/design/edu/unknown` |
| `score` | REAL | 0–1，`n/(n+2)`，表示「多少词支撑」，**不是概率** |
| `hits` | TEXT | JSON 计数：`{"code":3,"doc":1}` |
| `method` | TEXT | `rules`（正常）/ `missing`（图丢了）/ `error:<类型>`（单张读失败） |
| `version` / `created_at` | TEXT | 算法版本 / 处理时刻 |

保留：`shot_text.text` 按 `vision.retention_text_days` 清（`hub-store prune`）；`shot_labels` 与 `shot_text` 的计数字段不过期。
**谁写**：`bin/hub-vision run`（derive 也会顺带跑）。删掉这两张表 = 丢掉派生结果，`events` 与图不受影响。

### 5.11 `shot_verdicts`（画面粗判，M12）

PK = `(sha256, version)` —— 同一张图可以有多行（不同模型/不同版本），用来对比。

| 列 | 类型 | 语义 |
|---|---|---|
| `sha256` / `ts` | TEXT | 图的指纹 / 时间（与 `shot_labels` 同源） |
| `verdict` | TEXT | `work` / `play` / `unknown` |
| `raw` | TEXT | 模型原话（截断 32 字），用来复盘判错 |
| `model` | TEXT | 模型文件名（如 `SmolVLM2-500M-Q8_0.gguf`） |
| `version` | TEXT | `vlm-<算法版本>`；换提示词/换类别表必须换版本 |
| `method` | TEXT | `vlm`（正常）/ `error:<类型>`（单张失败） |
| `img_w` / `img_h` | INTEGER | 实际喂进模型的尺寸（缩图后） |
| `latency_ms` | INTEGER | 单张耗时（含加载模型则不准，仅参考） |
| `created_at` | TEXT | 处理时刻 |

**谁写**：`bin/hub-vision vlm`（`vision.vlm.enabled=true` 时 derive 也会顺带跑）。删表 = 丢派生结果，`events` 与图不受影响。

### 5.12 `input.sample`（M13 输入节奏，事件 payload）

`events(source='input', kind='input.sample')`，payload 共十个字段（M16 加了五个键盘节奏指标）：

| 字段 | 类型 | 语义 |
|---|---|---|
| `secs` | REAL | 这一窗口多少秒 |
| `keys` | INTEGER | 键盘按下次数（不含抬起、不含自动重复） |
| `clicks` | INTEGER | 鼠标左/中/右键按下次数 |
| `wheel` | INTEGER | 滚轮格数（取绝对值） |
| `dist_px` | INTEGER | 光标移动距离（像素，窗口内累加位移的欧氏长度） |
| `devices` | INTEGER | 这一轮打开了几个设备（只做健康检查，不参与计算） |
| `iki_median_ms` | INTEGER | 相邻按键间隔中位数（只算 ≤3s 的间隔；0 = 没打字） |
| `iki_cv_ms` | REAL | 间隔的变异系数（标准差÷均值，越稳越小） |
| `pauses` | INTEGER | 窗口内 >2s 的停顿次数 |
| `burst_max` | INTEGER | 最长连击段（相邻 ≤500ms 的连续按键数） |
| `bs_ratio` | REAL | 退格数 ÷ 总按键数（**code 只在本进程内比较**，不落库） |

**没有 keycode、没有时间戳序列、没有文本** —— 这是契约，不是实现细节。
M16 的五个节奏指标是**聚合值**：算得出来、但反推不出你打了什么。

### 5.13 `camera.sample` / `self.report`（M14）

`camera.sample`（每 60 秒一条，跟活动采样同一分钟）：

| 字段 | 语义 |
|---|---|
| `state` | `away` / `present` / `facing` |
| `faces` | 检测到的脸数（0/1/2+） |
| `face_ratio` | 最大那张脸的脸宽 ÷ 帧宽（0–1） |
| `yaw` | 鼻尖相对两眼中点的水平偏移 ÷ 眼距（0 ≈ 正对） |
| `latency_ms` / `w` / `h` / `version` | 检测耗时 / 帧尺寸 / 版本（`cam-1`） |

**没有图像、没有人脸特征、没有身份** —— 这是契约。

`self.report`（你手动记的一条）：`{"level": 1|2|3}`，1 专注 / 2 一般 / 3 走神。别的什么都不写。

### 5.14 `payload.redact` 与历史清洗（M15）

`events(source='phone-hub')` 命中验证码时 payload 多一个 `redact` 对象（**不建表**，命中量直接查它）：

| 字段 | 语义 |
|---|---|
| `hit` | 恒为 `true`（未命中**不写**这个字段 ⇒ 有这个字段就是命中过） |
| `kind` | `cn-code` / `en-code` / `cred` / `cn-keyword` / `en-keyword`（§4.21 那张表） |
| `version` | 判据版本 = `redact.patterns_version`；换词表/判据必须换号，新旧行才能分开统计 |

历史清洗（`hub-store redact --source phone-hub`，命令见 §2.2）：

- 逐行读 `events.payload` → `redact.apply(payload, config)` → **只 `UPDATE events.payload`**
  （`ts`/`source`/`kind`/`fingerprint`/`privacy` 不动，绝不 DELETE），所以 `SELECT COUNT(*) FROM events`
  清洗前后**必须相等**；不新增表、不新增行；
- 幂等：已命中行带 `redact.hit=true`、或正文已等于占位符 ⇒ 跳过，连跑第二次 `hit=0`；
- `--dry-run` 只扫描并报 `hit` 条数，**不发任何 UPDATE**；
- `--limit N` 只扫前 N 行（按 `id` 升序），用来小步试跑；
- `redact.enabled=false` ⇒ 命令不碰库（只打一行说明，退出 0）。

### 5.15 `features`（M16，行为特征表）

`PRIMARY KEY (day, hour, feat_version)`；`hour` 取 0–23 本地小时；派生层，可整表重算。

| 列 | 类型 | 语义 / 缺失时 |
|---|---|---|
| `presence_ratio` | REAL | 这一小时里 activity 采样判「在场」的比例；无采样 ⇒ NULL |
| `facing_ratio` | REAL | camera 采样里 `facing` 的比例；无采样 ⇒ NULL |
| `switch_count` | INTEGER | 窗口应用集合变化次数（下界） |
| `keys` / `clicks` | INTEGER | 这一小时按键 / 点击总数 |
| `dist_px` | INTEGER | 光标净位移合计 |
| `straightness` | REAL | 轨迹直度均值（1 = 直线，越小越绕） |
| `iki_median_ms` / `iki_cv_ms` | INTEGER / REAL | 键盘节奏（按窗口按键数加权平均） |
| `pauses` / `burst_max` | INTEGER | 停顿总次数 / 最长连击 |
| `bs_ratio` | REAL | 退格比 |
| `phone_notif` | INTEGER | 这一小时手机通知条数 |
| `phone_notif_min` | INTEGER | 由通知间隔推断的手机使用分钟（下界，gap≤5min 视为连续） |
| `vision_cat` | TEXT | 截屏类别众数（`unknown` 也算结果） |
| `focus` | INTEGER | 该小时所覆盖片段的加权专注度；没片段 ⇒ NULL |
| `coverage` | TEXT | 六位位图 `presence/facing/input/window/phone/vision`，`1` 有数据 `0` 没有 |
| `phone_day_min` | INTEGER | 当天手机屏幕累计分钟（来自 M17 日桶快照，整天每行同值）；没有快照 ⇒ NULL |
| `phone_day_top` | TEXT | 当天用得最多的手机应用包名；没有快照 ⇒ NULL |
| `phone_game_min` | INTEGER | 这一小时里**配置里列为游戏**的 app 在前台多少分钟（M18）；没有该时段数据 ⇒ NULL |

## 6. 失败语义（必须可机械断言）

| 情形 | 行为 |
|---|---|
| 单个采集器抛错 | 记 `collector_state` 的最后错误 + 日志；**其余采集器继续**；该 source 在汇总里 `ok=false`；进程退出码仍为 0 |
| 前置条件缺失（无 `wl-paste`、无 `pyatspi`、会话目录不存在） | 同"采集器抛错"，并归入 `doctor` 的未就绪项 |
| 事件重复 | `INSERT OR IGNORE` 丢弃，计入 `skipped_duplicate`，**不报错** |
| 库不可写 / 配置非法 | 退出码 1，不产生任何部分写入 |
| spool 读取后入库失败 | 先入库、成功后才截断 spool（崩溃可重放，靠去重键幂等） |

## 7. 落位

```
info-hub/                     # 代码（工作区，入 git）
  INTERFACE.md  PROJECT.md  README.md  DELIVERY.md  hub.config.yaml  agent.md
  src/infohub/{config,store,bus,cli,deliver,privacy,redact,bili,kb,browserhist,history,behavior,vision_keywords,vlm,localdigest}.py
  src/infohub/{plugin,sources,views,derived,atspi,events,phone_push,phone_remind}.py   # 插件宿主 / 来源注册表 / view 解析缝 / derived 解析缝 / 本体环境能力 / 手机通道命令
  plugins/sources/{dsh-session,clipboard,window,fs,bilibili,browser-history,screenshot,phone-hub,phone-usage,study,qq}/{plugin.yaml,collector.py}
  plugins/processors/rules/{plugin.yaml,rules.py}                                    # S14 规则加工
  plugins/views/{report,digest,week,board,ui}/{plugin.yaml,<entry>.py}               # S15/S16 视图（ui=控制台）
  plugins/derived/{activity,vision,calib,features,input,camera,screencast}/{plugin.yaml,<entry>.py}   # S17 环境/派生层
  plugins/channels/desktop-popup/{plugin.yaml,channel.py}
  plugins/channels/phone-mcp/{plugin.yaml,channel.py,layout.schema.json,README.md}   # S9 手机通道 + layout v1
  plugins/channels/qq-onebot/{plugin.yaml,channel.py,README.md}                      # QQ 通道（OneBot v11）
  plugins/channels/dsh-ask/{package.json,index.mjs,selftest.mjs,README.md,cordis.patch.yml}   # S11 DSH 双弹插件（node）
  bin/{hub-collect,hub-store,hub-rules,hub-notify,hub-report,hub-ui,hub-digest,hub-digest-dsh,hub-digest-local,hub-week,hub-screencast,hub-history,hub-activity,hub-vision,hub-input,hub-calib,hub-features,hub-plugin,hub-deliver,hub-phone-push}
  packaging/info-hub-*.service  info-hub-*.timer
  tests/{verify_m0.sh,verify_m1.sh,verify_m2.sh,verify_m2b.sh,verify_m2c.sh,verify_m2d.sh,verify_m2f.sh,
         verify_m3.sh,verify_m3b.sh,verify_m3c.sh,verify_m4a.sh,verify_m4b.sh,verify_m4d.sh,
         verify_week.sh,verify_m5.sh,verify_m5b.sh,verify_m7.sh,verify_m8.sh,verify_m10.sh,verify_m11.sh,verify_m12.sh,verify_m13.sh,verify_m14.sh,verify_m15.sh,verify_m16.sh,verify_m17.sh,verify_m18.sh}
  tests/{check_deliver_http.sh,check_desktop_popup.sh,check_ui_deliver.sh,check_agent_md.sh,check_phone_mcp.sh,
         check_ui_plugin.sh,check_derived_plugins.sh,derived_mod.py,
         stubs/gdbus_stub.sh,stubs/mcp_stub.py}
~/.dsh/info-hub/              # 数据（不入 git）
  hub.db  hub.db-wal  state/bus.jsonl  state/collect.lock  state/screencast.json  state/vlm-tmp/  reports/  logs/  screens/
  vendor/llama.cpp/           # M12：llama.cpp 预编译二进制（不入 git、不进备份）
  models/*.gguf               # M12：本地小 VLM 权重（不入 git、不进备份；约 0.5–1 GB）
```

## 8. 验收判据

- M0：[tests/verify_m0.sh](tests/verify_m0.sh) 全过、退出码 0（含 `--selftest` 15 项）。
- M1：[tests/verify_m1.sh](tests/verify_m1.sh) 全过、退出码 0（合成 8 条事件 → 恰好 3 条待办；再跑幂等；人改状态不被覆盖；看板同步只推 open 且不重复；默认关不发请求；dry-run 不写库；规则开关生效）。
- M2：[tests/verify_m2.sh](tests/verify_m2.sh) 全过、退出码 0（默认关不发；无界面事件不弹；有界面事件弹一次并消费；最小间隔/每小时/同类每天三道节流各自生效；假 gsettings/gdbus 验勿扰与空闲两道门走真解析路径；dry-run 不发不写；发送失败落 `shown=0` 且不标记可重试；status 摊开现状）。
- M3：[tests/verify_m3.sh](tests/verify_m3.sh) 全过、退出码 0（generate 建三区块；重复 generate 幂等不留痕；人改追加留痕 `author=human`；系统数据变了再 generate 只重写自动区块、**人的文字逐字保留**；修订作者序列 `hub,human,hub`；`diff --rev` 可查；改报表不动 `events`/`todos`；`--report ../evil` 被拒；UI `/api/state` 只读快照可用；UI 保存留痕 `author=human`、可触发重写 `author=hub`；Host 非 loopback → 403、未知路由 404；只绑 `127.0.0.1` 无 `0.0.0.0`；UI 走一圈系统数据仍不变）。
- M4a：[tests/verify_m4a.sh](tests/verify_m4a.sh) 全过、退出码 0（合成「已响应/被忽略/判定期内/无指针」四类历史 → `samples`/`ignored_rate`/`downshifted` 数字精确；样本不足不降频；老数据无 `todo_id` 不进样本；降频类被门 5b 拦住且 `reason=throttled-downshifted`；阈值调高后同一数据正常弹 ⇒ 证明是 5b 起作用；`status` 列出降频类别）。
- M4b：[tests/verify_m4b.sh](tests/verify_m4b.sh) 全过、退出码 0（默认关 ⇒ `disabled`；`--dry-run` 组装提示词且**凭据已脱敏**、不写库；正常归纳落库且同输入复用；挂不存在事件 id 的条目被丢弃；输出非 JSON / 加工层退出码非 0 ⇒ 退出 1 且不写库；空条目合法；报表 `digest` 区块带 `evidence`；真库可组装提示词）。
- M4c：[tests/verify_week.sh](tests/verify_week.sh) 全过、退出码 0（生成周报：右边界事件不计入本周、条目逐条带真实证据、概览计数正确；**不改** `hub-report.md`；`--dry-run` 不落盘；`--week-of` 过去那周另落一份且计数 0；`--weeks 2` 区间 14 天且不倒回去覆盖历史周文件；`list` 可用；坏 `--week-of` 退出 1；空库也能出周报）。
- M5：[tests/verify_m5.sh](tests/verify_m5.sh) 全过、退出码 0（5 条收藏 → 5 条事件、再采游标挡住重复；只有 KB 里查不到的工具出待办，已在 KB 的（含只由仓库名命中的）静默；同工具两个视频合成一条且重复跑幂等；KB 判定落缓存不重复检索；KB 根不可读 ⇒ fail-closed 记 error 且零待办；`enabled=false` 不采；来源命令失败/输出非 JSON ⇒ 退出码仍 0；KB 检索大小写不敏感且词边界生效；提工具只认机械形态（裸名字不猜）。另：真实 KB 上只读实测 99 个文本文件 / 2.41 MB、单次检索 0.01–0.04 s）。
- M5b：[tests/verify_m5b.sh](tests/verify_m5b.sh) 全过、退出码 0（假 History 上：首次 4 条（2 仓库页 + 2 视频页）、`privacy=sensitive`；无关页面/站点路径段/窗口外记录都不入库；同一视频只留最近标题且去掉站点尾巴；再采游标挡住重复；规则恰好 1 条待办且 `why` 点明「来自浏览器历史」、证据是真实事件；`max_items` 每类各算；库不存在 ⇒ 退出码 0 且错误进 `run:browser-history`；`enabled=false` 不采；**取数后假 History 的 sha256 不变**（只读证据）；单元级：站点路径不出条目 / GitLab 认 host / 微秒→ISO / 失效重置链接（`password_reset/`+200 位 base32）与超长 owner 都不产假仓库）。
- M4d：[tests/verify_m4d.sh](tests/verify_m4d.sh) 全过、退出码 0（本地后端读**上游真提示词**：证据全真且都在提示词里、条数 ≤ `max_items`、text ≤ 60 字、待办优先、分组按条数倒序；同输入两次跑逐字节相同；提示词里没有事件 id ⇒ `{"items": []}`；空库 ⇒ `{"items": []}`；接进 `digest.run` ⇒ 落库 1 行、同输入 `reused` 且不再调后端；待办证据老于 `max_events` 也被带进提示词且条目没被审计丢掉；`digest.timer` 周日跑且早于周报、`hub-install --dry-run` 纳入安装面）。

**写验收脚本时的三条已知坑**（踩过，写在这是为了让下一期不必再踩）：
1. `hub-rules --json derive` 的 `items[]` 只回 `{key, rule, title}`，**没有 `why`** —— 要断言待办正文得回库里查。
2. `store.todos()` 的第一个位置参数是 `status`：`todos(10)` 是 `status=10`，几乎必然空结果；要写 `todos(limit=10)`。
3. 采集器失败时的状态键是 `run:<name>`（`cli.run_round` 的 except 分支），**不是** `<name>:items`（那是成功后的游标键）。
   同理，断言"产出"要按被测产出的**真实结构**（`items[].key` / 库里 `why`）来写，别把夹具写错的账算到功能上。
- M2b：[tests/verify_m2b.sh](tests/verify_m2b.sh) 全过、退出码 0（通知带上 `acted/完成` 按钮且台账落 `dbus_id`；点「完成」⇒ 待办 `done`+`human_override=1`+台账 `action=acted`，有效性把它算成 `acted`；点「忽略」⇒ `dropped`；未知 `dbus_id`/动作键/无 `todo_id` 指针 ⇒ 退出 1；`listen` 认 `ActionInvoked`、`NotificationClosed` 只计数、查不到通知记 `errors` 但退出 0、monitor 起不来退出 1；`backend=command` 时 `dbus_id=None`；真总线上 `listen` 可挂 3s 不报错）。

- M2f：[tests/verify_m2f.sh](tests/verify_m2f.sh) 全过、退出码 0（**触发面**：不带 `--notify` 完全不评估；
  这一轮真收进新 `window` 事件 ⇒ 采完评估一次且退出码仍 0；只收 `fs` 事件 ⇒ `reason=no-new-ui-event` 不弹；
  库里**旧**的 `window` 事件不算数；池子里的待办弹完收敛到 0；`notify.enabled=false` ⇒ 触发面照走、门 1 挡住；
  装的单元 `ExecStart` 带 `--notify`；`--help` 列得出它）。
- M2c：「忽略此类」按下 ⇒ 该类被压住（到期时间落 `kind_suppressions`）+ 这一条待办 `dropped`；同类再出现
  被门 6 拦住（`reason=kind-suppressed`）；**被压住的类别不阻塞别的类别**；`unsuppress` 后同类又能弹；
  到期的抑制自动失效；`--days 0` 打回（退出码 1）；`listen` 收到带「忽略此类」的 `ActionInvoked` 同样生效。
- M3b：[tests/verify_m3b.sh](tests/verify_m3b.sh) 全过、退出码 0（控制台改状态 ⇒ `human_override=1` +
  `todo_revisions` 记 `human/status/open→done/ui`；跑完规则不翻回且不多写记录；「稍后」⇒ `snooze_until` 落库 + 状态仍 open + `field=snooze_until`；
  非法状态 400 / 未知 id 404 / 越界 400 / Host 403；CLI `todo-set` 记 `reason=cli`；改清单不写报表账本）。
- M3c：[tests/verify_m3c.sh](tests/verify_m3c.sh) 全过、退出码 0（种子造**真事件**、规则引擎自己出待办 ⇒
  证据摊开成真实 `events` 行、`missing=0`；`/api/todo/detail` 同时给出证据链与改动记录；未知 id ⇒ UI 404 + CLI 退出 1；
  证据链只读（`events`/`todos`/`todo_revisions` 一字不动）；查不到的 id **如实标 `missing`**；
  门 8：候选无依据 ⇒ `reason=no-evidence` 且台账 0 条；**无依据的候选不许饿死有依据的候选**）。
- M2d：[tests/verify_m2d.sh](tests/verify_m2d.sh) 全过、退出码 0（默认三个按钮且真传给 gdbus；「稍后」⇒ `snooze_until` 落库 + `notified_at` 清空 + 状态仍 open，未到点 `reason=snoozed`、到点同一条再提示；「打开」⇒ 台账 `action=open`、状态不变、`{url}` 正确替换，命令起不来 ⇒ 退出码 0 + `open_error`；`notify.actions` 可改；未知键退出 1；`status` 带 `actions`/`snooze_minutes`）。
  见 [tests/verify_m2c.sh](tests/verify_m2c.sh)（11 项全过、退出码 0）。

- M7：[tests/verify_m7.sh](tests/verify_m7.sh) 全过、退出码 0（活跃 ⇒ 取 1 帧 + `shot.saved`/`sensitive` 事件 +
  图真落盘 + 事件 `sha256` == 文件哈希 + `rel` 是 root 相对路径；间隔内不再取；**不活跃（空闲 900s）⇒ 不取帧、
  不发事件、状态记 `idle`**；活跃度探不到 ⇒ 不取帧 + 记 `run:screenshot` + 退出码 0；`require_active=false` 时探针不参与；
  **打字保护：空闲 1s 且 `pause_wait_s=0` ⇒ 不取帧、`last_skip=typing`；空闲序列 1s→1s→10s ⇒ 等到停顿（10s）后仍取到 1 帧**；
  `enabled=false` 一张不取；`max_width`/`jpeg_quality` 真传给取帧命令；`prune` 删老图留新图且**事件元数据不动**；
  `status` 如实报；`grab` 立刻取一帧；空命令 ⇒ 退出 1 且不写文件；过期清理单元含 `hub-screencast prune`）。
- m2 追加 T15：空闲解析（`(uint64 29866,)` ⇒ 29.866s，< 60s ⇒ 门 3 放行；修掉"位宽被当数字吃掉"的解析 bug）。
- M8：[tests/verify_m8.sh](tests/verify_m8.sh) 全过、退出码 0（分类单元 12/12 含搜索/网盘/AI/本机/多段后缀；
  条目 = 时间 + 网址 + 站点 + 用途 + 标题前 120 字（去站点尾巴、空网址不出条目）；窗口内记录全量落库且
  `kind=browser.history`/`privacy=sensitive`；同一网址只留最近一次；再采一轮 `emitted=0`；
  `summarize` 的总数/用途/站点与库里一致且给得出**一条总结**；窗口收窄 ⇒ 更早的不进总结；
  **全量历史里只有仓库页产卡**（视频/小说标题里的斜杠词不产假工具）；报表含 `hub:auto:begin history` 区块；
  库不存在 ⇒ 退出码 0 + `run:browser-history` 记错；`enabled=false` 不采；假 `History` sha256 不变；CLI `classify`/`summary` 出数）。
- M10：[tests/verify_m10.sh](tests/verify_m10.sh) 全过、退出码 0（合成采样序列 ⇒ 片段切分 / 在场时长 / 切换数 / 三因子与专注度按 §4.15 公式精确；
  `gap>gap_s` 断两段；`active=false` 收段；`active_s<min_active_s` 不落库；同一输入重算两次逐字节相同、`events` 一字不动；
  `day` 输出含开始时刻 / 分钟数 / 专注度；无采样 ⇒ 输出「今天没有采样」且退出码 0；
  探针失败 ⇒ `sample` 退出码 1 且不落半条；`def_version` 不同 ⇒ 新旧行共存不互相覆盖；
  采集器落库条件：在场必落、离开边界落一条、离开期不足 `heartbeat_s` 不落）。
- M11：[tests/verify_m11.sh](tests/verify_m11.sh) 全过、退出码 0（词表判定按 §4.16 精确：`code/doc/chat/video/shop/terminal` 各命中一个样本、命中不足 `min_hits` 判 `unknown`、并列按词表顺序；
  同一画面只读一次（第二次扫描 `pending=0`）；`keep_raw=false` ⇒ `shot_text.text` 全为 NULL 且只落计数与 `text_sha`；
  `keep_raw=true` ⇒ 原文落库且 `hub-store prune` 按 `retention_text_days` 清掉；引擎缺失 ⇒ `error` 字段且零写入、`hub-rules derive` 退出码仍 0；
  单张图丢失 ⇒ 记 `method=missing` 的 `unknown` 行且不中断本轮；同一输入重跑两次标签逐字节相同；`bin/hub-vision classify` 不碰库）。
- M12：[tests/verify_m12.sh](tests/verify_m12.sh) 全过、退出码 0（**用假 runner，不依赖真模型**：`work`/`play` 正确映射到 §5.11 的 `verdict`；
  模型输出表外词 ⇒ `unknown` 且 `raw` 留原话；缺模型/缺二进制 ⇒ 结果带 `error`、零写入、`hub-rules derive` 仍 0；
  同一 `version` 重跑 `pending=0`；换 `version` ⇒ 两行并存；缩图后 `img_w == max_width`（等比，高度按比例）；
  临时缩图文件跑完不残留；`act` 汇总数字与库一致；`vlm` 不处理已判过同版本的图）。
- M13：[tests/verify_m13.sh](tests/verify_m13.sh) 全过、退出码 0（**纯合成事件，不碰真设备**：`keys` 只数按下、
  忽略抬起与自动重复；0x110/111/112 计点击、其它键盘码计按键；`wheel` 取绝对值；`dist_px` 等于位移欧氏长度；
  payload **只有 secs/keys/clicks/wheel/dist_px/devices 六个字段、绝无 keycode**；没设备时 `Sampler` 抛 `PermissionError`；
  `hub-input stats --json` 的数字与库里一致；`hub-activity day` 打出「手上」一行；配置键齐全）。
- M14：[tests/verify_m14.sh](tests/verify_m14.sh) 全过、退出码 0（**合成人脸框，不碰摄像头**：没人⇒`away`、
  空结果⇒`away`、大脸正对⇒`facing`、大脸侧着⇒`present`、小脸⇒`present`、多脸取大的；缺模型⇒`FileNotFoundError`；
  `calib.auc` 完美/反向/并列三种情形分别 1.0 / 0.0 / 0.5；造 6 条自报+特征后 `report` 能算出按键速率 AUC >0.8；
  `camera.sample` payload 字段白名单里没有任何图像字段；`collect.camera` 五个配置键齐全）。
- M18：[tests/verify_m18.sh](tests/verify_m18.sh) 全过（合成事件段：RESUMED/PAUSED 配对后按小时切分、跨整点正确、未闭合段截止到 now、
  非游戏包不进结果、白名单为空则不算、前缀匹配生效、features 的 `phone_game_min` 逐小时正确、跨零点小时键不倒串）。
- M17：[tests/verify_m17.sh](tests/verify_m17.sh) 全过、退出码 0（**合成 `dumpsys usagestats` 文本，不碰手机**：
  日桶 `timeRange` 之后的 `package=` 行被解析成 178 包 / 1226 分钟那一档；周/月/年桶**不进结果**；
  `H:MM` 与 `HH:MM:SS` 两种时长串换算一致；缺 `totalTimeVisible` 的行被跳过而不是记 0；
  `top_n` 之外的应用不进 payload；adb 缺失/无设备/命令非 0 ⇒ `CollectorError` 且库里零行；
  `interval_s` 内第二次调用返回空；真机跑一轮后 `hub-features build` 的当天行 `phone_day_min` 非 NULL、
  `phone_day_top` 是包名；同一天两次 build 逐字节相同）。
- M16：[tests/verify_m16.sh](tests/verify_m16.sh) 全过、退出码 0（合成事件算特征：一小时按键 600 次、
  间隔 100ms ⇒ `iki_median_ms≈100`、`pauses=0`、`burst_max` 明显大；间隔 2.5s 交替 ⇒ `pauses>0`；
  退格 10/100 ⇒ `bs_ratio=0.1`；无 camera ⇒ `facing_ratio` 为 NULL 且 `coverage` 对应位为 0；
  手机通知间隔 5 分钟内聚成一段 ⇒ `phone_notif_min` 等于跨度；同输入两次 `build` 逐字节相同；
  `show --json` 二十四行按小时升序；`features` 表受 `feat_version` 隔离）。
- M15：[tests/verify_m15.sh](tests/verify_m15.sh) 全过、退出码 0（**合成 payload，不碰真库、不碰网络**：
  短信验证码正文换成占位符且 `hit=true`/`kind=cn-code`，`ts/pkg/app/title/key` 原样保留；
  群聊里只提「验证码」而正文没有数字串 ⇒ **一字不动**（弱证据只在短信 App 上生效）；
  `verification code` / `otp` 同样替换；`token` 后面没有值的标题不误杀；
  `enabled=false` ⇒ 原样保留、不加 `redact` 字段；采集器接入点在 `insert_events` 之前（假 `ssh` 造 3 条 ⇒
  库里就是脱敏后的 3 条）；历史清洗只 UPDATE：**行数不变**、`ts/fingerprint/privacy` 不变、正文变占位符；
  连跑两次第二次 `hit=0`（幂等）；`--dry-run` 不写库；`redact.*` 七个键齐全）。


- S13（强推送两端）：[tests/check_strong_push.sh](tests/check_strong_push.sh) 11 项全 PASS、退出码 0（手机腿只建 `kind=strong` 的 pending 行；电脑腿把 zenity 返回值写成 `reply` + `status=replied`；`--no-reply` 只确认不写回复；`--phone` 只跑一条腿；`--dry-run` 不落库）。
- QQ 采集（2026-11-18 定型 OneBot spool 版）：[tests/check_qq_source.sh](tests/check_qq_source.sh) 76 项全 PASS、退出码 0（首轮只入 message 事件、自己发的不入、半行留到下轮、重复 message_id 只入一条、非文本段占位、轮转后 offset 归零且 seen_keys 去重、limit 小于积压量时逐轮补齐不跳行（T13）、spool 不存在/坏 JSON/超时三种失败都是 rc=0 + sources.qq.ok=false + fail_streak=1，恢复后归 0）。真实环境首轮 ingested=110、二轮 0、rc=0。
- QQ 通道（2026-11-18 新增）：[tests/check_qq_channel.sh](tests/check_qq_channel.sh) 35 项全 PASS、退出码 0（通道未装提示、成功 delivered+message_id、Bearer 鉴权头、群消息/名字解析、后端拒绝留 pending+attempts+last_error、--pending 重推、--dismiss 走 delete_msg、--dry-run 不发请求、回复结算（自己发的不结算、重复跑幂等）、选项 2 记标签、入参错退出 2）。
## 9. 运行面（谁在跑、什么时候跑）
部署 = systemd **user** 单元（无窗口、退出码透传给 systemd；见 `packaging/`，用 `bin/hub-install` 安装）：

| 单元 | 触发 | 干什么 | 退出码 |
|---|---|---|---|
| `info-hub-collect.timer` → `.service` | 每 5 分钟（`OnCalendar=*:0/5`, `Persistent=true`） | `hub-collect --once --notify`（收进新 `window` 事件才评估通知，见 §4.6「触发面」；M7 截屏按 `interval_s` + 活跃门取帧；M8 浏览历史全量入一条/网址） | 配置/存储致命=1，其余=0 |
| `info-hub-derive.timer` → `.service` | 每 15 分钟 | `hub-rules --quiet derive`（`board.enabled=false` 时不碰看板；`vision.enabled=true` 时顺带读 `max_per_run` 张没读过的截屏；`vision.vlm.enabled=true` 时再顺带做 `max_per_run` 张粗判，失败只记录；顺带写当天与前一天的 `features` 与 `episodes`，失败只记录） | 0 |
| `info-hub-input.service` | **常驻**（`Restart=on-failure`） | `sg input -c 'hub-input --quiet listen'`（每 60 秒落一条 `input.sample`；`collect.input.enabled=false` 时自己退出 0） | 无权限=1，其余=0 |
| `info-hub-activity.timer` → `.service` | 每 1 分钟 | `hub-activity --quiet sample`（在场/离开 + 窗口集合哈希；`collect.camera.enabled=true` 时顺带取一帧判在座状态；**独立于 5 分钟的 collect 轮**，否则分辨率被拉到 5 分钟） | 探针失败=1，其余=0 |
| `info-hub-report.timer` → `.service` | 每天 23:50 | `hub-report --quiet generate` | 0 |
| `info-hub-digest.timer` → `.service` | 周日 23:30 | `hub-digest --quiet run --window-days 7`（M4d 默认本地后端） | 0；加工层坏=1 且不写库 |
| `info-hub-week.timer` → `.service` | 周日 23:55 | `hub-week --quiet run` | 0 |
| `info-hub-prune.timer` → `.service` | 周日 23:40 | `hub-store prune`（按 `retention_days`，并清过期的 OCR 原文 `vision.retention_text_days`）**+** `hub-screencast prune`（按 `keep_days`，两条 `ExecStart`） | 0 |
| `info-hub-ui.service` | 常驻（`Restart=always`） | `hub-ui --host 127.0.0.1 --port 8799` | 常驻 |
| `info-hub-notify.service` | **不发** | `hub-notify listen`（动作回调） | 常驻；**装它要显式 `--with-notify`** |

- 安装/重装：`bin/hub-install [--with-notify] [--dry-run]`（幂等：单元内容没变就不覆盖；`daemon-reload` 后 enable --now）。
- 通知相关单元**故意不随默认安装生效**（`notify.enabled=false` 是设计，见 PROJECT.md §13）。
- 采集器自身的失败**不影响**整轮退出码：单源失败只记 `collector_state` 的 `last_error`（§6）。

## 10. 插件契约（插件宿主 `hub-plugin`）

本体只认插件清单，不认具体实现。发现根按顺序：`<项目>/plugins` → `$HUB_ROOT/plugins` → 配置 `plugins.paths` 里逐个列出的绝对路径（配置见 §3）。每个插件一个目录，目录里一份 `plugin.yaml`：

| 键 | 必填 | 取值 | 说明 |
|---|---|---|---|
| `name` | 是 | 唯一字符串 | 同名以先发现者为准，后发现的记 `shadowed: true` |
| `kind` | 是 | `source` / `processor` / `view` / `derived` / `channel` | 阶段；S1 只做发现与加载，不调实现 |
| `api` | 是 | 整数，当前只认 `1` | 不认 ⇒ `status=error` |
| `entry` | 是 | `相对文件路径:函数名` | `runtime=python` 时导入后取函数，必须可调用 |
| `runtime` | 否 | `python`（默认）/ `node` | `node` 只校验入口文件存在（DSH 侧插件由 INTERFACE.md §12 约定） |
| `default_enabled` | 否 | 布尔，默认 `false` | 配置 `plugins.<name>.enabled` 覆盖 |
| `config_key` | 否 | 字符串 | 例如 `collect.fs` |
| `bins` | 否 | 字符串数组 | 插件拥有的 `bin/*` 名字，仅用于展示 |
| `requires` | 否 | `{python: [模块…], commands: [命令…]}` | 缺依赖 ⇒ `status=error`，`reason` 点名缺什么 |

- **状态**：`ok`（加载成功）/ `error`（清单非法、entry 不存在、导入抛异常；`reason` 存原文）/ `disabled`（`enabled=false`）；`shadowed` 表示同名被前面的插件压住。
- **清单命令**：`bin/hub-plugin list --json` 在 stdout 打一行 JSON
  `{"plugins":[{name,kind,api,entry,runtime,path,status,enabled,reason,shadowed,config_key}]}`，按 `name` 升序；没有插件时 `{"plugins":[]}`。
- **隔离**：加载一个插件失败只影响该插件；`hub-plugin list` 退出码仍 0；采集轮与视图必须跳过 `error`/`disabled`。
- **失败语义**：`plugin.yaml` 读不出来、根不是映射、缺必填键 ⇒ 该插件 `error`，扫描继续；宿主本身不写库、不建目录。
- **加载副作用**：`runtime=python` 的插件在 `list` 时就会被导入（为了让"装得上"可机械验证）；导入必须幂等、不得在导入期写库或发通知。
- **解析缝（不许业务代码自己 import 插件文件）**：`kind=view` 走 `src/infohub/views.py`（`module/entry`，`ViewError` 三分），`kind=derived` 走 `src/infohub/derived.py`（`module/entry`，`DerivedError` 三分）。调用方把"未安装/未启用/加载失败"折成退出码 1 并原样点名，不许静默跳过（唯一例外：采集轮里派生计算失败只记一行、不拖垮整轮）。
- **兼容门面（S17）**：`src/infohub/{activity,vision,calib,features,inputs,camera,screencast}.py` 只剩约 20 行 import 门面，把 `plugins/derived/<name>/<file>.py` 原样暴露成旧 import 路径；实现不在本体。新代码用 `derived.module(config, "<name>")`，才会受 `plugins.<name>.enabled` 约束。
- **来源插件的开关优先级（S3 定，§4.4；2026-11-18 按实现校正）**：`plugins.<name>.enabled=false` ⇒ 一律不跑（卸掉）；否则看 `collect.<name>.enabled`（缺省取清单 `default_enabled`，`--source` 点名时不看）。**实现上**（`src/infohub/plugin.py` 的 `enabled = config.get(plugins.<name>.enabled, default_enabled)`）：清单 `default_enabled: false` 的来源在未写 `plugins.<name>.enabled: true` 时会 `status=disabled`、根本进不了 `sources.registry()`，`hub-collect --source qq` 也是静默不跑 —— 所以「默认不装」的插件（如 `qq`、`phone-usage`）要么写 `plugins.<name>.enabled: true`，要么别指望它被点名。

## 11. 投递台账（`deliveries` 表与 `bin/hub-deliver`，S4）

一条消息一行：手机 / 电脑 / DSH 三条投递通道共用同一本账。本节只定义台账与回执命令；怎么下发由通道插件负责（S7 / S9 / S11，§10），HTTP 与认领租约是 S5。

### 11.1 表结构

`deliveries`（主键 `id TEXT`；`CREATE TABLE IF NOT EXISTS` —— 老库首次跑任一 `hub-deliver` 命令时自动补表，不重写已有行）：

| 列 | 类型 | 语义 |
|---|---|---|
| `id` | TEXT PK | 投递 id；`hub-deliver add` 生成 `d-<yyyyMMddHHmmss>-<6 位十六进制>` |
| `created_at` / `updated_at` | TEXT | ISO 8601 带本地时区（与 `events.iso()` 同口径）；任何真写入都刷新 `updated_at`，幂等空转不刷新 |
| `channel` | TEXT NOT NULL | 建行的通道名（`computer` / `phone` / `dsh`，或通道插件自定义），不设白名单 |
| `mode` | TEXT | `single` / `status` / `question` / `NULL`；`NULL` = 通道自定（手机通道默认 `single`），见 §13 |
| `kind` | TEXT | 消息类别（规则名 / 待办 `dedupe_key` 前缀等），供通道算「同类抑制」；`hub-deliver add` 留 `NULL` |
| `todo_id` | TEXT | 关联待办 id；`NULL` = 与待办无关的纯消息 |
| `title` | TEXT NOT NULL | 标题（非空） |
| `body` | TEXT NOT NULL DEFAULT '' | 正文，允许空串 |
| `options` | TEXT | JSON 数组 `[{key,label,description}]`；`key` 是稳定键（§11.2）。`NULL` = 无选项 |
| `layout` | TEXT | 整份界面 JSON（§13）。`NULL` = 用通道默认版式 |
| `status` | TEXT NOT NULL DEFAULT 'pending' | 见 §11.2 |
| `action` | TEXT | 被选中的**稳定键**（§11.2）；发送时为空 |
| `reply` | TEXT | 自由文本，**原样**保存（不去空格、不截断、不转义） |
| `reply_at` / `acked_at` | TEXT | 首次 reply / ack 的时刻 |
| `remote_ref` | TEXT | 通道侧引用（系统通知 id / 手机卡片 id 等），用于回查 |
| `lease_owner` / `lease_until` | TEXT | 认领租约（S5 `POST /api/deliver/claim`） |
| `attempts` | INTEGER NOT NULL DEFAULT 0 | 下发尝试次数（S9 退避用） |
| `last_error` | TEXT | 最近一次下发失败原因 |

索引：`deliveries_status(status, created_at DESC)`、`deliveries_channel(channel, status)`、`deliveries_todo(todo_id)`。

- **`options` 只认 `key`/`label`/`description` 三键**：`hub-deliver add --options` 会丢弃多余键。`label` 只给界面显示用，**绝不落进 `action`**（结算只认 `key`）。
- **`options`/`layout` 存 canonical JSON**（键排序、无多余空白、保留中文）；`hub-deliver list --json` 读出来时已解析成 JSON 值，不是字符串。

### 11.2 取值

- `status`：`pending`（已建、未下发）/ `delivered`（已下发到通道）/ `acked`（人看见了）/ `replied`（人回了文本）/ `suppressed`（被「忽略此类」压住）/ `expired`（超时或对侧已答，作废）/ `failed`（下发失败且放弃）。
- `mode`：`single`（一次性卡片）/ `status`（常驻、原地更新）/ `question`（要回答）；`NULL` = 通道自定。
- 稳定动作键：`ack` / `done` / `snooze` / `ignore-kind` / `reply` / `open`；现行通知动作的旧写法 `acted` / `ignore` 也照收（S7 负责映射到待办语义，S4 只把键写进 `action`）。

### 11.3 命令（`bin/hub-deliver`）

全局参数 `--config PATH`（缺省 `$HUB_CONFIG` → 项目内 `hub.config.yaml`）。stdout 只放机器可读值，人读日志走 stderr（§2.1）。

| 命令 | 入参 | 语义与退出码 |
|---|---|---|
| `add` | `--channel C`（必填）`--title T`（必填）`[--body B] [--mode M] [--options JSON] [--layout JSON] [--todo-id ID] [--json]` | 写一行 `status=pending`。不带 `--json` 时 **stdout 只打新 id**，`已建投递 …` 摘要走 stderr；`--json` 打该行全文。`--channel`/`--title` 为空、`--mode` 不在枚举、`--options` 不是 `[{key,…}]` 数组、`--layout` 不是对象 ⇒ `2` |
| `list` | `[--json] [--status S] [--channel C] [--limit N]` | 按 `created_at` 倒序（同刻按 `id` 倒序）。`--json` 打**单行** `{"deliveries":[{…整行…}]}`；`--limit` 默认 50，`0` = 全部。`--status` 不在枚举 ⇒ `2`；空库 ⇒ 退出 `0` 且 `{"deliveries":[]}` |
| `ack <id>` | `[--action KEY] [--json]` | 只表示「看见了」：写 `status=acked` + `acked_at`，**不改待办状态**；带 `--action` 时同时把该键落 `action`（待办联动归 S7）。**幂等**：该行 `acked_at` 非空 ⇒ 一字不改、返回 `changed=false`、`acked_at`/`updated_at` 保持第一次的值。未知 id ⇒ `1`；`--action` 不在 §11.2 的键集 ⇒ `2` |
| `reply <id> --text T` | `[--json]` | 文本**原样**写 `reply`，`reply_at` 落时刻，`status=replied`；`acked_at` 与待办状态都不动。`--text ""`（或全空白）⇒ `1` 且**库不变**；未知 id ⇒ `1`。重复 reply 覆盖旧文本并刷新 `reply_at`——「只结算一次」由通道侧保证 |

`--json` 的 `add`/`ack`/`reply` 打该行最新全文；`ack` 额外带 `"changed": true|false`。退出码口径：`0` 成功；`1` 未知 id、空回复、配置/存储致命；`2` 用法错误（含取值不在枚举内、JSON 不合法）。

### 11.4 语义边界（机械可断言）

- **ack ≠ 完成**：`ack` 只记「人看见了」，`todos.status` 一律不动。改待办状态是通道插件按 `action` 键结算（S7）。
- **一次终态**：`reply` 比 `ack` 强。对 `status ∈ {replied, suppressed, expired}` 的行再 `ack` 只补 `acked_at`，`status` 不被降回 `acked`。
- **reply 原样**：不做 trim、不做 JSON 转义、不截断；判「空」只看 `str.strip()`，落库的是原字符串。
- **通道只管自己的行**：`deliver.py` 不调任何通道、不发通知、不认识手机/DSH 协议；它只读写台账。
- **迁移幂等**：`deliveries` 由 `store.migrate()` 的 `CREATE TABLE IF NOT EXISTS` 建；老库跑第二次不多表、不改已有行（验收见本节 S4 证据 `evidence/W2-S4-deliver.md`）。

### 11.5 HTTP 接口与认领租约（S5）

复用 `bin/hub-ui` 的进程与绑定（§4.8）：控制台与投递接口同一个端口，**只绑 loopback**；Host/Origin 守卫（§4.8）对下列路由一律先生效。四个路由，除 `pending` 外都是 POST JSON：

| 路由 | 方法 | 入参 | 语义 |
|---|---|---|---|
| `/api/deliver/pending` | GET | `?channel=C&limit=N` | 列出**可认领**的行：`status=pending` 且租约为空或 `lease_until` 已到；按 `created_at` 倒序（同刻按 `id` 倒序）。`channel` 省略 = 所有通道；`limit` 缺省 50，范围 1–500 |
| `/api/deliver/ack` | POST | `{id, action?}` | 与 `hub-deliver ack` 同语义（§11.3）：幂等、`action` 只落稳定键、不改待办状态 |
| `/api/deliver/reply` | POST | `{id, text}` | 与 `hub-deliver reply` 同语义：文本**原样**落 `reply`、`status=replied` |
| `/api/deliver/claim` | POST | `{id, owner, ttl_s}` | 把 `lease_owner`/`lease_until` 写进同一行；`ttl_s` 为整数 1–604800（秒） |

- **成功**一律 `200`，正文是该行**最新状态**的单行 JSON（字段与 `hub-deliver … --json` 同形；`ack` 额外带 `changed`）；`pending` 返回 `{"deliveries":[…整行…]}`。
- **失败语义**：非 loopback 的 `Host`、或带 `Origin` 且 `Origin` 非本机 ⇒ `403`；未知 id ⇒ `404`；`ack` 的 `action` 不在 §11.2 键集 ⇒ `400`；`reply` 的 `text` 缺失/非字符串/全空白 ⇒ `400`；`claim` 的 `owner` 为空或 `ttl_s` 缺失/非整数/越界 ⇒ `400`；`id` 缺失或空 ⇒ `400`；其它参数不合法 ⇒ `400`。错误正文是 `{"error":"…"}`。
- **认领**：`pending` 只返回未认领或租约已过期的行；租约到期后同一行重新可被取到（不自动清空 `lease_owner`，只按 `lease_until <= now` 判定）。重复 claim 同一 id ⇒ **最后一次的 owner 生效**（覆盖前一个租约），不报错。
- **语义复用**：HTTP 层只做参数校验与状态码映射，`ack` 幂等、`reply` 原样、空文本拒绝、认领写入分别复用 `deliver.py` 的 `ack`/`reply`/`claim` 与 `store.py` 的台账方法，**不在 HTTP 层重写**。
- **验收**：`tests/check_deliver_http.sh`（隔离根 + 临时端口；覆盖 Host/Origin 403、未知 id 404、空 reply 与坏 action 400、ack 幂等、reply 原样、租约遮挡与到期、重复 claim 最新 owner）。

- **expire（作废）**：§12.6 记的「§11.5 缺口」已补齐到 store 层——Store.expire_delivery / deliver.expire（status=expired + 清租约，终态不降级）；dsh-ask 的 ledger.py 改为直接调它，不再自己写 SQL。

### 11.6 通道插件接缝（S7 定型）

通道插件（`kind=channel`）只暴露一个函数 `entry(config, ...)` 形态的 `send(request: dict) -> dict`；
本体 `notify.dispatch` 按 §10 清单找**已启用的第一个**通道插件转发，没有通道 ⇒ `reason=no-channel`、退出码 0。
`request["op"]` = `dispatch`（缺省）/ `act` / `listen`；完整键表与返回值见 `plugins/channels/desktop-popup/README.md`。

- **无待办不弹**（用户 2026-11-14）：`todo_id` 缺失、待办不存在或状态不是 `open`/`doing` ⇒ `{ok:true, skipped:"no-todo"}`，不建行、不发通知；电脑弹窗只投真实待办，其余留在处理端（由它决定建定时任务还是即时消息）。
- **先台账后显示**：`op=dispatch` 先建 `deliveries` 行（`status=pending`、`channel=computer`、`mode=single`），再调系统通知；
  成功回写 `remote_ref`/\`status=delivered`，失败回写 `status=failed`/`last_error`/`attempts+1`（待办下一轮重试，记 `notifications(shown=0)`）。
- **先台账后结算**：`op=act` 先 `ack_delivery`/`reply_delivery`，再调 `notify.settle_action` 结算待办；`ack`/`reply` 不改待办状态；老数据找不到台账行时只做结算（向后兼容）。
- **动作键**只取自 `deliver.ACTIONS`（`open/snooze/ignore-kind/acted/ignore/ack`），按钮文案不落库；文案→键复用 `notify.CANON_ACTION`。
- **失败语义**：通道没装/没启用/导入失败 ⇒ `no-channel`（退出 0）；`notify.enabled=false` ⇒ 门 1 就 `disabled`（通道不被调到）；monitor 起不来 ⇒ `notify.ActionError`（`hub-notify listen` 退出 1）。
- **验收**：`tests/check_desktop_popup.sh`（14 项：先台账后显示、act 后 `acked`、停用 `no-channel` 且不建行、删目录后清单无该通道）。

### 11.7 手机通道（S9 定型）

通道名 `phone`，插件 = `plugins/channels/phone-mcp/`（`kind=channel`、`default_enabled: false`，配置键前缀 `phone_remind`）。
它只做三件事：**调手机 MCP 的 `hub_deliver` / `hub_dismiss`**、**建/更新 `deliveries` 行**、**把手机回执留给 §11.5 的 HTTP 接口**。

- **入口**：`send(request)`，`request["op"]` ∈ `dispatch`（缺省）/ `retry` / `dismiss`；键表与返回值见 `plugins/channels/phone-mcp/README.md`。
- **推送顺序（2026-11-18 修订：先台账，再尝试即时推送）**：新推一条 = 先生成 `delivery_id` 并建 `status=pending` 的 `channel=phone` 行 → 尝试即时推送（helper 的 `am start` / bridge 的 `hub_deliver`）→ 成功则回写 `status=delivered` + `remote_ref`。
  **即时推送失败 ≠ 投递失败**：手机端 `HomeSync` 是**轮询**取 `pending` 的，所以失败时行留 `pending`、写 `attempts`/`last_error`，返回 `transport=poll`，`bin/hub-phone-push` **退出 0**。只有「建行」本身失败才退出 1。
  旧实现（失败退出 1 且不留行）已废：`bridge` 侧 `hub_deliver` 随旧中转退役后只回 `Unknown tool`，旧判断把它当成功 ⇒ 台账写 `delivered` 而手机什么都没收到（2026-11-18 实测复现）。
- **界面 JSON**：有 `layout` 就整份透传（`layout.v=1` + `options[*].key`）；没有 `layout` 就只给 `title`+`text`（= body），由手机按 §13 回落标题+正文。
- **三种模式**：`mode=single/status/question` 原样透传；`question` 的选项、通知栏回复入口由手机 MCP 侧决定（S8），本通道不解释。
- **重推与退避**：`op=retry` 用于已存在的 `status=pending` 行；失败**留 pending** 并写 `attempts+1`/`last_error`，退避 60/120/240/600 秒（`channel.backoff_s`）；`bin/hub-phone-push --pending` 只挑到点的行。
- **收回**：`op=dismiss` 调 `hub_dismiss`；手机找不到卡片返回 `ok:true, dismissed:0`（不报错）。
- **传输复用**：MCP 调用走 `infohub.phone_remind.bridge_call`（initialize → `mcp-session-id` → tools/call，SSE/裸 JSON 都认）；端点 `phone_remind.bridge`（缺省 `http://<bridge-host>:9500/mcp`）、超时 `phone_remind.timeout_s`。
- **与旧强提醒的关系**：`bin/hub-phone-remind` 走 bridge 的 `device_action`（`hub_sync`/`reminder_*`），仍是独立老路径；新投递走本通道（helper 通知或轮询），两者共库不共行。
- **即时推送与轮询的分工**：有 USB/adb 且装了 helper ⇒ 即时（`transport=immediate`），通知立刻出现；没有 USB ⇒ `transport=poll`，手机下一轮（≤120s）自己取到并弹通知。两条路都不需要旧 hub。
- **验收**：`tests/check_phone_mcp.sh`（20 项：layout 透传/回落、即时推送失败仍退出 0 且留 `pending`（T3）、ack 不改待办、schema、pending 重推、dismiss），用 `tests/stubs/mcp_stub.py` 替身，不碰真手机。

#### 11.7b 第二后端：helper（App 自拉 + 通知栏直接回复，2026-11-18 定型）

jxev App 的 APK 里 `KEY_TEXT_REPLY` / `deliver_show` 出现 0 次 ⇒ 结构上发不出「通知栏可直接回复」的通知。
因此有自制 helper `com.chenty.hubreplier`（`info-hub/tools/hubreplier/`）。**2026-11-18 起它不需要服务端推送、也不需要 adb 在环**：

- **拉**：`HomeSync` 前台服务每 120 s GET `<base>api/messages?limit=80&archived=0`。`base` 由 `Endpoints.pick` 从设置页四栏候选里挑（内网 HTTP / IPv6 / Cloudflare https 直连，全不通才用 `LocalRelay` 的 SOCKS 中转，走手机 MCP 隧道 App）。只要 `status ∈ {pending, claimed}` 就画通知。
- **画**：每条待处理一条通知（notification id 由 delivery id 派生，同 id 覆盖不堆叠）：标题 + BigText 正文 + 1 个「回复」RemoteInput 动作 + 最多 3 个选项按钮；`setOnlyAlertOnce(true)`。subtext 只在 `mode` 非空且非字面 `"null"` 时显示 `hub:<mode>`——org.json 的 `optString` 会把 JSON null 变成字符串 `"null"`，必须挡。
- **常驻**：`NOTIF_ID=42001` 的不可划掉通知「info-hub · N 条待处理，点开看全部」（前台服务，随同轮刷新；连不上时显示「连不上 info-hub（<url>）」）。
- **回**：点选项按钮或填 RemoteInput ⇒ `ResponseReceiver`（显式广播、`exported=false`；PendingIntent 用 `FLAG_UPDATE_CURRENT|FLAG_MUTABLE`，Android 12+ RemoteInput 必须 MUTABLE，否则系统填不进结果）⇒ `HubClient.postReply` POST `<base>api/deliver/reply`，头 `X-Hub-Token`，2xx 算成功；失败 `Endpoints.invalidate()` 下轮重选地址。选项按钮落 `action=<option_key>`，文本落 `reply=<text>`。
- **回传文本**：选项按钮回 **label**（「收到」），key 只留本地 jsonl 的 `action`；输入框回原文。台账里 `reply` 因此是人话（`reply=等会儿`），要机器 key 从手机 jsonl 取。
- **结算**：POST 成功 ⇒ 同一条通知刷成「已回复：<答案>（已回传 info-hub）」并 autoCancel；下一轮拉取发现该行不再 pending/claimed ⇒ `Notifications.dismiss` 收回。
- **权限**：Android 13+ 未授 `POST_NOTIFICATIONS` 时通知全静默（`dumpsys notification` 显示 `AppSettings importance=NONE`）。主页顶部有琥珀横幅「通知权限没开：收不到可回复通知 [开启]」，点它走 `requestPermissions`（<33 跳系统通知设置页），授权后立即 `HomeSync.start`。
- **adb 的角色**：只剩装/卸/看日志（`install -r`、`logcat -s HubReplier`）。**旧路径已废**：`am start --es payload_b64` 推送、`run-as … all.jsonl` + `bin/hub-phone-replies --apply` 回执拉取都不再使用（`phone_remind.backend=helper` 的 adb 推送代码保留，只作 bridge 后端与换 ROM 兜底）。
- **易混点：手机侧「自 adb」不是 info-hub 的通道**。`com.jxev.ai` 的 `AdbLocalClient` 连手机 `127.0.0.1:5555`（靠 `adb reverse` 指向 PC 上的 `adb_facade_srv.py`）只跑 `shell:`；那个 facade **只支持 `adb shell`，push/pull 不支持**（push 打印成功后卡 20 s 超时、pull 约 1 s 报 `device offline`；拒绝后 shell 立刻仍可用）。细节与实测数字在 `~/.local/share/dsh-phone/README.md`，与 helper 的推送/回复链路无关。
- **服务端接口**：`POST /api/deliver/reply {"id","text"}` ⇒ 200 + 整行 JSON（status 变 `replied`、写 `reply`/`reply_at`）；`id` 缺失或空 ⇒ 400 `{"error": "id 必须是非空字符串"}`。
- **验收**：`tests/check_phone_api.sh`（42 项，含 T42「页面 JS 可 `node --check`」）；真机侧判据与实测步骤见 `tools/hubreplier/README.md` §2.6。
- **真机实测（2026-11-18 09:47–09:56）**：点「收到」⇒ `status=replied`、`reply=收到`；填 RemoteInput「111」⇒ `reply=111`；两次 `postReply … => 200`，通知刷成「已回复：…」并在下一轮收回。证据 /tmp/notif_expand2.png、/tmp/notif_t2_exp2.png。

### 11.9 消息中心（控制台输入 / 归档 / 附件）

控制台主页是**消息中心**：历史消息可回看、可归档，页面本身能发消息，消息里带的图片/文档在消息行里直接渲染。
数据仍只有 deliveries 一张表，只加两列（_upgrade() 里 ALTER TABLE 补）：

| 列 | 类型 | 语义 |
|---|---|---|
| archived_at | TEXT | 归档时间（ISO）；**NULL = 未归档**。归档不改 status、不删行 |
| attachments | TEXT | 附件数组的 canonical JSON：**数据根相对路径**（如 media/shot.png），不是绝对路径 |

- **默认只看未归档**：hub-deliver list 与 GET /api/messages 缺省都只列 archived_at IS NULL；--archived / ?archived=1 反过来只看归档。
- **归档幂等**：archive_delivery 用 archived_at = COALESCE(archived_at, ?)，重复归档时间不变；未知 id ⇒ CLI 退出码 1 / HTTP 404。
- **归档即退出派发**：pending_deliveries 加 archived_at IS NULL，归档行不会再被任何通道认领。
- **附件校验**：绝对路径、含反斜杠、NUL、或任一段为 . 或 .. 或空 ⇒ 拒绝（CLI 退出码 2 / HTTP 400）；只做路径校验，不做存在性校验（图可能后来被移走，渲染时降级为「附件缺失」）。
- **渲染**：/api/messages 返回的行里 attachments 已解析成数组；前端用 base64url(rel) 拼 /thumbs/<id>（缩略图）与 /asset/<id>（原图/下载），编码与 §4.8 媒体路由的 id 规则一致。

| 路由 | 方法 | 入参 | 语义 |
|---|---|---|---|
| /api/messages | GET | ?limit=N&archived=0|1 | 列消息（created_at 倒序；同一秒内按插入顺序倒序 —— `ORDER BY created_at DESC, rowid DESC`，否则 id 的随机后缀会让「最近一条」不确定）；limit 1–500，缺省 50；archived 只接受 0/1，缺省 0。返回 {"messages":[整行…],"archived":bool} |
| /api/messages | POST | {text, title?, attachments?, channel?} | 建一条消息：text 非空（全空白 ⇒ 400），channel 缺省 console、kind=message；title 缺省取 text 首行前 80 字。成功 **201** + 整行 JSON |
| /api/messages/archive | POST | {id} | 与 hub-deliver archive 同语义；成功 200 + 整行，未知 id 404 |

- **页面结构**（tests/check_ui_messages.sh 断言）：#msgin 输入框 + #msgsend 发送 + #msgarch 显示归档 + #messages 列表 + <template id="message-row">；待确认行仍带「确认 / 回复」（复用 §11.5 的 ack/reply）。
- **复杂度**：只新增两个查询过滤与一个幂等 UPDATE，不新增表、不改 §11.5 的四个投递路由语义；空文本/坏 limit/坏 archived 一律 400。
- **验收**：tests/check_ui_messages.sh（37 项：CLI 归档语义与幂等、POST 建行落库 channel=console/kind=message、archived 开关两向、附件 /thumbs//asset 200、页面结构与模板字段、四类 400/404、归档行不进 pending）。

#### 11.7c 强推送（S13：手机全屏 + 电脑对话框，2026-11-18 定型）

用户要的「强体系」是**一条消息两端都弹**，两端都能选/打字回复，且 info-hub 能看到这条消息与回复。
命令：`bin/hub-strong-push --title … --body … [--option 标签 …|--options JSON] [--phone|--desktop] [--dry-run]`
（缺省两条腿都发；`--phone` / `--desktop` 只发一条腿；返 `{"phone":{…},"desktop":{…}}`，全成功退出 0）。

| 腿 | 通道 | 怎么弹 | 怎么回 | 依赖 |
|---|---|---|---|---|
| 手机 | phone | 只建 `kind=strong` 的 pending 行，由 helper 的 HomeSync（30 s 一轮）拉起全屏页 | helper 的 `StrongActivity`：锁屏也整屏，有选项就每个选项一个按钮，也可直接在输入框打字 → `HubClient.postReply` POST `/api/deliver/reply` ⇒ 行 `status=replied` | 只要 helper 前台服务活着；**不再调** bridge 的 reminder_show（用户 2026-11-18 实测「info-hub 的和原来的并存」⇒ 只要一个弹窗） |
| 电脑 | computer | `infohub.desktop_popup.show` ⇒ zenity 模态框：有选项用 `--forms`（下拉选项 + 回复输入），否则 `--entry`（可打字），`--no-reply` 时才退回 `--warning` | 表单返回值直接写回同一行（`store.reply_delivery`，`status=replied`） | 本机图形会话有 zenity（GNOME Wayland；无会话变量时补 `DISPLAY=:2 WAYLAND_DISPLAY=wayland-0`） |

- **台账语义**：两条腿各建一行，`channel=phone` / `channel=computer`，`kind=strong`，`title/body/options` 与消息一致。
  手机行在拿到 bridge 确认后仍保持 `pending`（它就是「还没回」的标记，info-hub 页面与 `hub-deliver list` 都看得到）；bridge 失败时同行为 pending + `last_error`，不丢消息。
- **与 §11.7 的关系**：`hub-phone-push --strong` 只发手机腿（走同一个 `reminder_show`，`transport=reminder`）；`hub-strong-push` 是两端编排。
- **为什么不用系统通知**：GNOME 通知会滑走、可能被勿扰吞掉，不是「强」提醒；电脑腿必须是模态对话框（形状沿用本机 wx-popup-server 的 zenity 560 宽）。
- **验收实证（2026-11-18）**：`hub-strong-push --title 强体系选项自检 --option 收到 --option 稍后` ⇒ `phone {shown:true, fullscreen:true, keyguard:false}`、`desktop {reply:"收到", option:"收到"}`；电脑 zenity 之前一版 `--entry` 曾把 `--entry-text` 占位文字当回复提交，已改成提示写在正文里。

#### 11.7d QQ 通道（2026-11-18 新增：OneBot v11 发消息 + 回复回写）

后端 = 家庭机上的 SnowLuma（注入真实 QQ 客户端），OneBot v11 的 HTTP 口 `127.0.0.1:3000`
（ws 口 3001 只给事件 tap 用）。台账通道名 `qq`；插件 `plugins/channels/qq-onebot`（`default_enabled: false`，
要 `plugins.qq-onebot.enabled: true`）。命令：

- `bin/hub-qq-send --to <uin|群id|名字> [--group] --title … --body … [--options JSON] [--json] [--dry-run]`
  建一行 `channel=qq` 的投递并发出去 ⇒ `status=delivered`、`remote_ref={"type","id","message_id"}`。
  后端拒绝/不可达 ⇒ 行留 `pending` + `last_error` + `attempts=1`，**退出码 1**（账已记、没发出去，可重推）。
- `bin/hub-qq-send --pending`：重推 `channel=qq` 的 pending 行（op=retry），返回 `{retried,delivered,failed}`。
- `bin/hub-qq-send --dismiss <投递 id>`：OneBot `delete_msg` 撤回已发出的那条。
- `bin/hub-qq-replies [--json] [--dry-run]`：读同一个事件 spool（自己的游标 `qq-replies`），
  把对方回来的私聊/群消息结算成回复：有 options 时 `1..n` 或与 `label`/`key` 相等 ⇒ 记选项标签，
  否则原样记自由文本；写 `status=replied` + `reply`（不改待办状态，同 §11.4）。

| 配置键 | 缺省 | 语义 |
|---|---|---|
| `qq_onebot.enabled` | false | 通道装机闸（清单 `default_enabled: false` ⇒ 必须显式 true） |
| `qq_onebot.ssh_host` | `root@<bridge-host>` | 置空 = 本机执行同一条远端命令（测试注入） |
| `qq_onebot.api` | `http://127.0.0.1:3000` | OneBot HTTP 口（后端机视角） |
| `qq_onebot.token_file` | `/opt/qq-relay/onebot.token` | **HTTP 适配器**的 access token 文件（0600，0600 在远端；ws 口另有 token） |
| `qq_onebot.spool_path` | `/opt/qq-relay/queue/events.jsonl` | 事件 spool（采集器与回复结算共用） |
| `qq_onebot.timeout_s` | 25 | 单次远端调用超时 |

- **失败语义分两层**：远端鉴权/网络/接口错 ⇒ `PushError`（行留 pending，退出码 1）；入参错（缺 target、
  目标类型不认识）⇒ `ChannelError`（退出码 2，库不变）。
- **target 解析**：`--to` 既可给数字（uin / group_id），也可给好友备注/昵称/群名 ——
  先找完全相等，再找包含匹配；歧义时取第一个完全相等。
- **与 §11.7 / §11.7c 的分工**：QQ 是「聊天通道」（发出去进对方 QQ、回来的消息进台账）；
  手机/电脑是「提醒通道」（弹窗要人当场看）。
- **验收**：`tests/check_qq_channel.sh`（发送成功/失败留 pending/重推/撤回/回复结算/选项匹配/target 解析/通道未启用），
  真机实证见 `docs/qq-source-verify.md`。

### 11.10 手机主页（phone_api + helper WebView）

手机主页＝同一本 deliveries 台账的移动端入口：能看累计消息（含图片/文档）、能**回复提问**（选项 / 打字 / 提交 / 跳过）、能归档。
页面上**没有**「发一条新消息」的输入框——用户 2026-11-14 明确：手机上的「打字」指的是回复提问；POST /api/messages 接口保留给别的端调用，页面不再提供入口。
电脑控制台（§11.9）绑 127.0.0.1 且有 loopback 守卫，手机连不上，因此另起一个只读-HTTP 服务：可同时绑多个地址
（WG 地址给隧道/中转，局域网地址给同 Wi-Fi 的手机直连），入口按 token 鉴权。
由自研 helper（包名 com.chenty.hubreplier，WebView）加载；helper 按候选地址探测择优，**内网 HTTP / IPv6 / Cloudflare https 都走直连**，
com.jxev.ai 只是最后一档的传输管道（借它的用户态隧道 SOCKS5 口），不改不装不碰它的界面与数据。WireGuard 不是前提。

| 配置键 | 缺省 | 语义 |
|---|---|---|
| phone_api.enabled | true | false ⇒ 进程直接退出 0，不监听 |
| phone_api.hosts | ["0.0.0.0"] | 监听地址列表（也可写成逗号串，旧键 phone_api.host 仍认）。0.0.0.0 = 全部网卡：WG 地址供隧道/中转，局域网地址供同 Wi-Fi 手机直连；通配地址会独占端口，与具体地址同时写时只留通配（见 serve()） |
| phone_api.port | 8800 | 监听端口 |
| phone_api.token | 空 | **留空 = 自动生成**：首次启动生成 `hub-<32hex>` 写到 `$HUB_ROOT/phone_api.token`（0600），之后复用；要固定就填这里。每个请求都要校验 |
| phone_api.exposure | auto | 暴露面（**只剩诊断用**，不再驱动任何页面行为）。auto = 本机有非私有全局地址才算 public（_detect_public 读 `ip -j addr`）；经隧道/中转对外时本机仍是内网地址，显式写 public 才读得到真实暴露面 |

- **token 从哪来（2026-11-18 起）**：① 本键或旧键 `phone_remind.helper_token` 非空 ⇒ 用它；② 否则读 `$HUB_ROOT/phone_api.token`；③ 都没有就生成 `hub-<32hex>` 写入该文件（0600）。实现：`src/infohub/secret.py` 的 `get_token(config, explicit=None)`；APK 里不再有写死的 token。
- **鉴权：只有 token 一道**（用户 2026-11-17 22:56「有了 token 本身也不需要密码，可以删掉」）。请求头 X-Hub-Token 或查询参数 ?token=，用 hmac.compare_digest 比对；缺失/错误一律 401 `{"error":"bad-token"}`（页面也走同一门禁）。
  原来的访问密码（scrypt + secret_file + X-Hub-Password + 公网红横幅 + POST /api/settings/password）已整套移除；旧 secret 文件已移到 `~/backups/`。
- **并发**：ThreadingHTTPServer + 每线程独立 SQLite 连接（_ThreadStore 按线程缓存）。WebView 与常驻轮询会同时进来，单线程实测会互卡（2026-11-17 改）。
- **附件复用**：/thumbs/<id> 与 /asset/<id> 直接调控制台的 _asset_entry / _thumb_bytes / _ASSET_CTYPE（§4.8 的 id 编码与越界规则同一套：非法/越界 403、不存在 404）。
- **下载文件名**：非图片附件回 Content-Disposition: attachment; filename*=UTF-8''<urlencoded 原名>，helper 据此给文件起名（否则只能落到时间戳名）。

| 路由 | 方法 | 入参 | 语义 |
|---|---|---|---|
| / | GET | ?token=&helper=1 | 移动端页面（内联 HTML，不含任何构建步骤）；helper=1 ⇒ 显示「⚙ 设置」 |
| /api/settings | GET | — | 只读诊断：{exposure, public, hosts, port, token_set}（helper「测试连接」可拿它区分 200/401） |
| /api/messages | GET | ?limit=N&archived=0\|1 | 与 §11.9 同语义；limit 1–200（比控制台小），缺省 50 |
| /api/messages | POST | {text, title?, attachments?, channel?} | 建消息；channel 缺省 **phone**；成功 201 |
| /api/messages/archive | POST | {id} | 与 §11.9 同语义；未知 id 404 |
| /api/deliver/reply | POST | {id, text} | 回答提问：文本原样留档 + status=replied（§11.4 同语义）；空文本 400、未知 id 404 |
| /api/deliver/skip | POST | {id} | 跳过提问：status=expired + 清租约（已 acked/replied/suppressed 的行保留原终态）；未知 id 404 |

- **提问应答（用户要的「打字」就是这一块）**：status 为 pending/claimed 且（有 options 或 mode=question）的行，行内渲染「选项按钮 + 打字框 + 提交 / 跳过」。点选项等于用该选项的 label 发一次 reply（DSH 侧 buildAnswers 就是按 label 精确匹配回填 selected），打字提交等于自由文本 reply，跳过等于 expire——提问方会立刻收到「作废」而不是等到超时。
- **设置页 = 唯一的配置入口（用户 2026-11-17 22:51 澄清「不用 adb，装完手填配置」）**：消息页右上「⚙ 设置」→ 页面 location='hub://settings' → WebViewClient.shouldOverrideUrlLoading 拦下 → SettingsActivity（程序化 UI，无 layout XML）。字段只有四个：服务器地址（主）/ 备选地址（每行一个，可空）/ token / 中转目标（可空）；动作：保存（写 prefs + Endpoints.invalidate）、测试连接（逐候选探 api/messages，显示 可用 Nms / 401 token 不对 / 不通；直连全不通时按消息页同一回退规则试本机中转，命中显示「可用 Nms  http://127.0.0.1:8801/（经本机中转）」）。
  桌面图标 MainActivity：prefs 里没地址就**直接进设置页**，填过就进消息页。App 不自动填、不写回、不改服务端状态——缺什么就让人在设置页填。
- **页面结构**（tests/check_phone_api.sh 断言）：#arch 显示归档 + #reload 刷新 + #list 列表 + #lb 图片灯箱**没有 #tin/#send**（发消息入口已删）；待答行另有 .qbox（选项 / 打字 / 提交 / 跳过）；消息行不重复渲染 title 与 body（两者相同只显示一次）。
- **图片**：缩略图点开进灯箱（原图 /asset + 返回按钮），不整页跳转，避免 WebView 历史把页面顶掉。
- **文档**：链接带 download 属性，WebView 交给系统下载器（helper 的 DownloadListener → DownloadManager，落到 /sdcard/Download）。
- **候选地址择优（2026-11-17，不依赖 WireGuard）**：helper 每次启动/轮询先按顺序探测候选地址——`url` 主地址，然后 `urls` 里的备选——探测 = 带 token 的 GET `api/messages?limit=1`（连接/读超时各 2.5s），第一个 200 的胜出；结果缓存 60s，轮询失败即作废重探（网络切换不用重启 App）。
  - prefs（hub_home）：`url` 主地址（内网 http / IPv6 / Cloudflare https / WG 地址皆可）、`urls` 备选（逗号或换行分隔）、`relay`=`0` 关闭本机中转、`relay_url` 中转目标（缺省用 `url`）、`socks_port` 10800、`relay_port` 8801。也可用 am start 写：`--es url … --es token … --es urls "https://x.trycloudflare.com/" --es relay_url "http://<hub-host>:8800/"`。
  - **本机中转只是最后一档**：候选全不通且 `relay`≠0 时，helper 才在 127.0.0.1:8801 起字节级 TCP 中转，经手机 MCP（com.jxev.ai）的用户态隧道（SOCKS5 127.0.0.1:10800）送到 `relay_url`。SOCKS 不通退回直连。
  - 契约：Endpoints.java（探测/择优/缓存）+ LocalRelay.java（中转），§11.10 这两条即其文档。
  - **实测（2026-11-17 21:35，手机 5G 蜂窝 + 无 VPN）**：候选 = 内网 `http://<lan-ip>:8800/` + `https://cave-sake-scratch-ralph.trycloudflare.com/`；内网探测 2.5s 超时 → 选中 Cloudflare → 页面正常渲染、常驻通知同步成功（日志 `Endpoints picked https://…trycloudflare.com/`）。同一 URL 在手机 shell 里 curl：`/api/messages` 200、页面 200。
- **helper（tools/hubreplier）**：HomeActivity 用 WebView 装这个页面；url/token/urls/relay_url 从 am start 的 --es 写入后持久化到 SharedPreferences（hub_home），没配置过就 finish。地址择优在后台线程做（主线程探测必抛 NetworkOnMainThreadException）。
- **外部可达地址（2026-11-17，Cloudflare quick tunnel 已实测）**：手机不在同一 Wi-Fi、家里也没有公网 IPv6 时，用 Cloudflare 的免费隧道把 127.0.0.1:8800 映射成一个 https 地址，手机直连即可，不需要 WireGuard、不需要 com.jxev.ai。
  - 常驻：`systemctl --user status info-hub-tunnel.service`（ExecStart=`cloudflared tunnel --url http://127.0.0.1:8800 --no-autoupdate`，日志 `~/.dsh/info-hub/logs/tunnel.log`，Restart=always，Linger=yes 所以开机自起）。
  - 取地址：`bin/hub-tunnel-url`；写进 helper：`adb shell am start -n com.chenty.hubreplier/.HomeActivity --es urls "<该地址>/"`。
  - 实测（2026-11-17 21:39，手机 5G、无 VPN）：PC 与手机各 `curl /api/messages` 都 200；helper `Endpoints picked https://…trycloudflare.com/`，页面渲染、常驻通知同步 OK。
  - 限制：quick tunnel 的地址**每次重启都变**（helper 的 urls 要跟着换）；要固定域名得用 `cloudflared tunnel login` + 自己的域名建 named tunnel。
  - 暴露面：该地址公网可达，只靠 token 拦（页面能读消息、能回提问/归档）。token 泄露 = 等于把手机主页给出去。
- **IPv6 路线（2026-11-17 已跑通，走家庭出口机中转）**：`phone_api` 绑 `::`（双栈通配，IPv4 走 v4-mapped、IPv6 直听）；家庭出口机（Debian，WG <bridge-host>）有家宽全局 IPv6，它把 `[::]:8443` 转发到 WG 上的 `<hub-host>:8800`，手机用流量直连该 v6 地址即可，**不经过 WireGuard、不经过 Cloudflare**。
  - 实测地址：`http://[<v6-addr>]:8443/`（出口机 `enp2s0` 的 EUI-64 稳定地址；前缀 240e 由电信下发，重拨可能变）。手机 5G 实测 `/api/messages` 200、页面 200、helper 自动选中该候选。
  - 出口机侧件：`/root/hub-v6-relay.py`（纯标准库线程转发，无依赖）+ `/etc/systemd/system/hub-v6-relay.service`（enabled、Restart=always）；防火墙规则写进 `/etc/nftables.conf` 的 input 链：`tcp dport 8443 ct state new accept`（原文件备份 `/root/nftables.conf.bak-*`）。
  - 自检：`bin/hub-v6-check`（只读：全局 v6 地址 / 默认 v6 路由 / 监听 / [::1] 自测 / 出站 v6 / 防火墙）。
  - 前置条件：出口机要有家宽全局 IPv6（本机所在校园网不给 v6，ECS 也没分配，所以只有这一台能当 v6 入口）；地址变化后要更新 helper 的 `urls`。
  - 暴露面：8443 对全公网 IPv6 开放，只靠 token 拦；不想要就 `systemctl disable --now hub-v6-relay` + 删掉那条 nft 规则。
- **状态栏留白**：Android 15+ 强制 edge-to-edge。padding 必须加在 **WebView 的父容器**（FrameLayout）上并按 window insets 更新；给 WebView 自己 setPadding 实测无效（标题仍压在状态栏下面）。
- **验收**：tests/check_phone_api.sh（**45 项**：token 门禁四向、页面结构（含「无发消息框」）、发消息接口落库 channel=phone、归档两向、缩略图/原图/文档、越界 403、四类 400/404、灯箱与去重守卫、下载文件名头、图片不强制下载、提问行带选项、提交回复落库、跳过 ⇒ expired、空回复 400；T34–T41 只剩 token 一道：settings 只验 token、页面无横幅/无密码痕迹、设密码路由已删 404、只 token 200、无 token 401 且不要求密码、页面只需 token；T42 页面内联 JS 必须能被 node --check 解析）。T42 的由来：删密码那次漏写分号，整段脚本语法错误 ⇒ 消息页只剩标题栏与按钮、列表空白且没有任何报错，只有截图能看出来。

## 12. DSH 双弹插件契约（S11）

DSH 的 `ask_user_question` 被调用时，DSH 会话里的问题卡片与手机卡片**同时出现**；任一侧作答即结算，另一侧撤销。
插件目录：`plugins/channels/dsh-ask/`；它是 DSH 侧插件（`runtime=node`），不是 info-hub 的 python 通道，
因此 `hub-plugin list` 只登记、不加载——真正挂载由 §12.7 的 DSH 装载块完成。

### 12.1 缝与注册

| 项 | 值 |
|---|---|
| 清单 | `name: dsh-ask` · `kind: channel` · `api: 1` · `runtime: node` · `entry: index.mjs:apply` · `default_enabled: false` |
| DSH 入口 | `apply(ctx, config)`；`enabled !== true` ⇒ 直接返回，不注册任何监听 |
| 监听 | `ctx.on("user-questions/request", handler)`，挂在 bundle 级 ctx 上（无 scope 标签的监听者对 DSH 的 scope 过滤一律放行，能收到所有提问） |
| 请求 | `{agent?, questions, signal}`；`questions[] = {id, question, header?, options?: [{label, description?}], multiSelect?}` |
| 硬禁止 | 不得注册同名 `ask_user_question` 工具（`dsh-tools` 会抛 `already registered`）；只能走瀑布 |

处理器签名 `async (request, next) => answer`：**先调 `next()`** 让 DSH 自带答复器照常工作（卡片照弹），再并行推手机；
`next()` 的返回值/异常与手机答案用「先到先得」竞争，见 §12.4。

### 12.2 配置键（装载块里的 `config`）

| 键 | 必填 | 缺省 | 语义 |
|---|---|---|---|
| `enabled` | 否 | `false` | 只有 `true` 才注册监听；DSH 侧不装载时 info-hub 侧仍照常 |
| `hub_url` | 否 | `http://127.0.0.1:8799` | info-hub 控制台基址；只用于 `/api/deliver/pending` 探活与 `/api/deliver/claim` |
| `phone_mcp_url` | 否 | `http://<bridge-host>:9500/mcp` | 手机 MCP（streamable-http）；调 `hub_deliver` / `hub_dismiss` |
| `hub_root` | 否 | 插件目录上溯三级 = `info-hub/` | **只**拼 `PYTHONPATH=<hub_root>/src` 给 `ledger.py`；不设 `HUB_ROOT`（库位置仍由 `hub.config.yaml` 的 `root` 决定） |
| `hub_config` | 否 | 空 | 给了就作为 `HUB_CONFIG` 传给 `ledger.py`；空 = 用 `hub.config.yaml` 的默认发现顺序 |
| `timeout_ms` | 否 | `600000` | **手机侧等待预算**：到点释放认领、收回手机卡片，然后继续只等 DSH（不是整条提问的超时） |
| `poll_ms` | 否 | `500` | 轮询台账行看手机答案的间隔 |
| `hub_timeout_ms` / `phone_timeout_ms` / `ledger_timeout_ms` | 否 | `3000` / `30000` / `10000` | 单次 HTTP / MCP / 子进程调用的超时 |
| `python` | 否 | `python3` | 跑 `ledger.py` 的解释器 |
| `ledger_script` | 否 | 同目录 `ledger.py` | 台账脚本路径（调试/迁移用） |
| `owner` | 否 | `dsh-ask:<pid>` | `/api/deliver/claim` 的 owner |
| `debug` | 否 | `false` | stderr 日志开关（缺省静默，避免污染 DSH 日志）。为 `true` 时打**两类**：失败原因，以及成功路径的 `[dsh-ask] request … agent=yes|none` 与 `[dsh-ask] phone pushed <id>` 两行——不加这两行时「插件没被调用」与「调用了但没答案」在日志上分不开 |

### 12.3 台账行与手机载荷

先写台账（一行），再推手机；两处的 `options` **故意不同**：

| 落点 | `options` 的构造 |
|---|---|
| `deliveries.options`（台账） | **第一问的真实选项**：`{key: ack/done/snooze/later…（§11.2 稳定动作键）, label: 选项原文, description: 选项说明}`，最多 4 个；没有选项时单个 `{key: ack, label: 知道了}`。手机主页与电脑控制台直接拿它画按钮，点按钮回传 label ⇒ `buildAnswers` 能精确匹配回 selected（多问时按钮只答第一问，其余用文本，body 写「N 个问题」） |
| `hub_deliver.options`（手机） | 只能回传 §11.2 的动作键（§11.5 的 `ack` 会拒绝其它键），所以按**位置**映射第一问的选项：前 3 个依次用 `ack/done/snooze`，`label` 取选项 label、`description` 取选项 description；第一问没有选项 ⇒ 单个 `{key:"ack", label:"知道了"}` |

台账行固定：`channel=dsh`、`mode=question`、`title` = 第一问截断 80、`body` = `"<n> 个问题"`、
`layout` = §13 的 v1 整份 JSON（`title`=第一问、`body`=问题数、`input.enabled=true`、`options` 同手机的那份）。
手机调用：`hub_deliver(delivery_id=<台账 id>, mode="question", title, body, options, layout, timeout_s=ceil(timeout_ms/1000))`。

### 12.4 先到先得、返回与失败语义

**返回**：与 DSH 答复器同形的 `{answers: [{id, selected: [label…], custom?}]}`。DSH 服务要求**每个问题恰好一条**
（否则 `BAD_ANSWER`），所以未作答的问题补 `{id, selected: []}`（= 跳过）。

| 手机侧看到的状态 | 返回的答案 |
|---|---|
| `status=replied` | 第一问 `{id, selected: [], custom: <reply 原文>}`；若 reply 去空白后**逐字等于**第一问某个选项 label，则改为 `selected: [该 label]` |
| `status=acked` 且 `action ∈ {ack, done, snooze}` | 按位置取第一问第 0/1/2 个选项的 label 作为 `selected`；第一问没有选项 ⇒ `selected: []`（跳过） |
| 其余问题 | 一律 `{id, selected: []}` |

| 情形 | 行为 |
|---|---|
| DSH 先答 | 原样返回 DSH 答案；调 `hub_dismiss` **恰一次**；台账行置 `expired` 并清空租约 |
| 手机先答 | 台账写 `ack`/`reply`（幂等）+ 调 `hub_dismiss` 一次 + 返回手机答案；**不再等 DSH**，DSH 迟到结果丢弃、不二次结算 |
| `timeout_ms` 到点 | 台账置 `expired`（= 释放认领/清 `lease_owner`/`lease_until`）+ `hub_dismiss` 一次；**继续只等 DSH** |
| hub 不可达（探活非 200 / 异常） | 不建行、不推手机，只用 DSH 答案 |
| claim 失败 | 已建的行置 `expired`，只用 DSH 答案 |
| 手机推送失败（2026-11-17 修订） | **行保留 `pending`**，照常轮询等手机答案——手机主页与常驻通知靠轮询取行（§11.10 本机中转），推不动不代表手机侧不可用 |
| `next()` 抛 `NO_PROVIDER` | 只用手机答案（仍受 `timeout_ms` 约束）；手机也超时 ⇒ 原样抛该错误 |
| `next()` 抛其它错误 | 行置 `expired` + `hub_dismiss` 一次，原样抛 |
| 两路都失败 | 原样抛 DSH 错误 |

**结算纪律**：每条请求只有一次终态——先到者把 `settled` 置位后，输家的答案与禁止的回写一律丢弃；
`hub_dismiss` 每个请求最多一次；台账只有一行（`channel=dsh`）。
**降级**：台账不可达（探活非 200 / 建行失败）时行为与「没有这个插件」完全一致（DSH 卡片照旧）；只是推手机失败不再算降级，见上表。

### 12.5 `ledger.py`（插件自带台账工具）

用途：补 §11.5 的三个缺口（§12.6）。它是**独立进程**，用 `infohub.store` / `infohub.deliver` 直连同一份库，
不建表、不重写既有 SQL。调用方必须给 `PYTHONPATH=<hub_root>/src`（`index.mjs` 自动给）+ 可选 `HUB_CONFIG`。

| 子命令 | 入参 | stdout | 退出码 |
|---|---|---|---|
| `add` | `--channel C --mode M --title T [--body B] [--options JSON] [--layout JSON] [--todo-id ID] [--kind K]` | 新 id（一行） | `0`；取值/JSON 非法 ⇒ `2` |
| `get <id>` | id | 整行单行 JSON（`options`/`layout` 已解析） | `0`；未知 id ⇒ `1` |
| `expire <id>` | id | 整行单行 JSON | `0`；未知 id ⇒ `1` |
| `ack <id> [--action KEY]` | §11.2 的动作键 | 整行 JSON（含 `changed`） | 同 `bin/hub-deliver ack` |
| `reply <id> --text T` | 非空文本 | 整行 JSON | 空文本 / 未知 id ⇒ `1` |

`expire` 只写 `status/lease_owner/lease_until/updated_at`：**已是 `acked/replied/suppressed` 的行保留原终态**（§11.4），
但租约照样清空；未知 id 不做任何写入。这是本插件唯一一处直接 `UPDATE`（`store.py` 没有 `expire_delivery`），
原因记在 §12.6。

### 12.6 契约缺口与 S18 补齐项

`INTERFACE.md` §11.5 的 HTTP 只有 `pending` / `ack` / `reply` / `claim` 四个路由，缺：

1. **建行**（等价的 `bin/hub-deliver add` 有 CLI，但没有 HTTP）；
2. **按 id 读整行**（`pending` 只返回未认领的 `pending` 行，读不到已 `acked`/`replied` 的行）；
3. **置 `expired`**（作废 + 释放租约）。

当前方案：1/2/3 走 §12.5 的 `ledger.py` 直连库；HTTP 只保留探活（`pending?channel=dsh`）与 `claim`。
**S18 收口时补两个路由**：`POST /api/deliver/add`（入参同 `hub-deliver add`）、`POST /api/deliver/expire {id}`（同 §12.5 的
`expire` 语义），并让 `ledger.py` 退化成这两个路由的薄包装（CLI 形状不变，插件不用改）。
在此之前，`expire` 的直连 SQL 是**已知的临时接缝**，不是新契约。

### 12.7 装载方式（用户执行，不随代码提交自动生效）

DSH 侧（写进用户自己的 `~/.dsh/cordis.patch.yml`，与同文件 `:80-88` 的既有块同形）：

```yaml
- insert:
    - id: dsh-ask
      name: 'file://$HUB_ROOT/plugins/channels/dsh-ask/index.mjs'
      config:
        enabled: true
        hub_url: http://127.0.0.1:8799
        phone_mcp_url: http://<bridge-host>:9500/mcp
        hub_root: $HUB_ROOT
        timeout_ms: 600000
        python: python3
```

改完**需用户重启 `dsh web`**（本工位不重启；热装载未实测）。回滚：`enabled: false` 或整块删掉。
info-hub 侧登记（可选，只影响 `hub-plugin list` 的状态显示）：

```yaml
plugins:
  dsh-ask:
    enabled: true
```

`bash bin/hub-plugin list --json` 中该行的 `kind=channel`、`runtime=node`；未打开配置时 `status=disabled`（正常，
DSH 侧照样能装载，因为真正的挂载点是 §12.7 的 patch 块）。

### 12.8 验收与已知限制

- **A12 / A20**：`cd plugins/channels/dsh-ask && node selftest.mjs` 退出 `0`，12 项全过（全替身，不连真库/真手机）：
  清单键、DSH 先答、手机先答、手机先答后 DSH 迟到、hub 不可达、手机不可达、超时释放认领、文本回复、
  `NO_PROVIDER`、两路皆败、`apply` 只注册瀑布不注册工具、答案覆盖每个问题恰好一次。
- **真机待验（A13，归 S12）**：手机先答后 DSH 客户端卡片是否立刻消失——瀑布返回值结算 `ask()`，但客户端卡片的
  关闭路径未实测；不成立则按任务书预声明降级为「一期只做手机接管、DSH 不弹」。
- **探针测不到卡片那一半（2026-11-14 实测确认）**：`~/dsh-debug/probe/askprobe.mjs` 是 agent-less 调用，
  `dsh-api-remotes` 对无 `request.agent` 的瀑布请求直接 `next()` 跳过 ⇒ **卡片结构上不会下发浏览器**；
  探针能验的只有「建行 → claim → 推手机 → `/api/deliver/reply` → `ask()` 返回答案」这半条链（已验：行
  `d-20261114170715-885f56` 转 `replied`，`reply=从手机答`，`askprobe.log` 记 `answered`）。
  卡片那一半必须由**带 agent 的真实会话**触发，且要人点，脚本替不了。
- **手机 App 前置**：`deliver_show`/`deliver_dismiss` 需 App 实现（S10）；App 回传的 `action` 必须是 §11.2 的动作键，
  否则 §11.5 的 `ack` 会 `400`——这是 `ack/done/snooze` 位置映射的原因。
- **多问题**：手机一次只能答第一问（自由文本或按钮），其余补 `selected: []`；选项超过 3 个时手机只显示前 3 个，
  其余请在电脑上作答或文本回复。
- **DSH 侧 `next()` 的内部答复器**在手机先答后可能仍挂起，直到用户在 DSH 卡片上操作或会话结束；
  插件不 await 它（避免二次结算），迟到结果丢弃。

## 13. 手机界面 JSON（layout v1）

手机推送不再由各端各自排版：调用方给一份整份界面 JSON，手机 MCP 插件按它渲染；缺省或 `NULL` 时回落成 `{title, text=body}`。schema 文件：`plugins/channels/phone-mcp/layout.schema.json`（JSON Schema 2020-12，`additionalProperties: true`）。

```json
{"v":1,"title":"…","body":"…",
 "options":[{"key":"ack","label":"知道了","description":"…"}],
 "input":{"enabled":true,"placeholder":"回复…"},
 "actions":[{"key":"reply","label":"发送","style":"primary"}],
 "expires_at":"2026-11-14T12:00:00+08:00"}
```

| 键 | 必填 | 语义 |
|---|---|---|
| `v` | 是 | 版本，当前只认 `1`；不认识 ⇒ 手机侧当整份无效，回落标题+正文 |
| `title` / `body` | 否 | 缺 `title` ⇒ 用投递行的 `title`；缺 `body` ⇒ 用投递行的 `body` |
| `options` | 否 | `[{key,label,description}]`；`key` 是稳定键（`ack/done/snooze/ignore-kind/reply/open`），`label` 只是显示名、不落库 |
| `input` | 否 | `{enabled, placeholder}`；`enabled=false` ⇒ 不画输入框 |
| `actions` | 否 | `[{key,label,style}]`；`style` 取 `primary/default/destructive` |
| `expires_at` | 否 | ISO 8601 带时区；到点手机侧收起卡片，台账由调用方置 `expired` |

- **渲染纪律**：未知键一律忽略（向前兼容）；缺 `options` 时按 `mode` 给默认——`single` 或无 = 单按钮「知道了」，`question` = 单按钮「知道了」+ 回复输入框；整份 `layout` 为 `NULL` ⇒ 用 `{title, text=body}`。
- **降级**：手机 App 不认识 `layout` ⇒ bridge 侧仍必须用 `title`+`text` 成功显示（同一工具内判断，不要求调用方重试）；不支持原生可回复通知 ⇒ `question` 降级普通通知 + 页面内选项/输入。
- **透传**：info-hub 侧 `phone-mcp` 通道只把该 JSON 原样塞进 `hub_deliver(layout=…)`，不改键、不补默认值。
