<!-- 本文件由 project/blocks/ 组装生成，禁止手改；改块请改 project/blocks/B<n>-*.md 后重跑 pm-render.py -->
<!-- 块清单：B0 B1 B2 B3 B4 B5 B6 B7 B8 B9 B10 B11 B12 B13 B14 B15 B16 B17 B18 B19 B20 B21 B22 B23 B24 B25 B26 B27 B28 B29 B30 B31 B32 B33 B34 B35 B36 -->

# 信息中枢（info-hub）项目文档

- 版本：v1.0
- 日期：2026-10-15（M0–M6 实施完毕；§16–§30 为逐期记录）
- 状态：**M0–M6 已实施，系统在跑**（5 个 systemd 定时器 + 控制台 `http://127.0.0.1:8799/`）；
  通知（B 形态）默认关着，等 §13 例外条文经用户逐字确认；**通道自证**用 `bash bin/hub-notify test`（§31）。
- 文档索引：本文件 = **设计 + 规模评估 + 逐期记录**；接口/键名/数据表 = [INTERFACE.md](INTERFACE.md)；
  用法与验收 = [README.md](README.md)；交付现状与未决四项 = [DELIVERY.md](DELIVERY.md)。
- 立项依据：用户 2026-10-15 指令 —— 「我打算A+B，我希望B可以主动监测到我在做什么，能不能做成一个系统弹窗的形式？这样不烦人，主要是我希望做一个全局信息收集+处理中枢，然后可以自动拉一个代办清单，然后做一个ui让我可以手动修改（报表和系统数据分离，报表我能手动改，报表每次改动有记录），先写项目文档，评估规模，我之前的网络之类的就是它前期工作的一部分」
- 本文档不修改任何权威层文件。与 `AGENTS.md §一` 的冲突与修订草案见 §13（**待用户逐字确认，尚未落盘**）。

---

## 0. 一句话定位

把本机产生的活动信号（DSH 会话、文件、剪贴板、窗口、网盘/网页）收进一个**本地库**，加工成**待办清单**与**报表**，用**系统通知弹窗**在合适的时机轻量提示（不打扰），并提供一个**可手改、每次改动都有记录**的界面；系统数据（机器采集/推断）与报表（人可写）**物理分离**。

形态 = 用户选定的 **A + B**：

- **A（内部主动）**：观察与加工全在后台，产出进看板/库，不面向用户（与既有 `AGENTS.md §二` 一致）。
- **B（面向用户的主动）**：判断通过阈值时，用**系统通知**提示一句，附依据、可忽略、有节流。

---

## 1. 目标与非目标

### 目标

| 编号 | 目标 | 可判定的达成标志 |
|---|---|---|
| G1 | **采集**：本机可得信号统一入库（append-only 事件流） | `events` 表按天有数据，`hub-collect --once` 退出码 0 |
| G2 | **加工**：规则 + LLM 把事件归纳成待办与报表 | 产出的 todo/report 带 `evidence` 字段，可回溯到事件 id |
| G3 | **提示**：系统通知（弹窗）轻量告知 | `hub-notify test` 弹出一条探针，且节流器可机械断言 |
| G4 | **交互**：本地 UI 可手改报表与待办 | 改一次报表 → 修订表 +1 行、md 文件 hash 变化、git 有提交 |
| G5 | **分离**：系统数据与报表互不覆盖 | 机器写入永不改动 `reports/` 下的人写正文 |

### 非目标（本期明确不做）

- ❌ 读键盘/鼠标**内容**（本机不可行，见 §4.1；且是键盘记录器性质）
- ❌ 训练自己的 reward model（论文级复现，+5–10 人天 + 显卡，收益未验证）
- ❌ 多机同步/云端主存（Drive 仅作中转与备份，已有 `dsh-filebridge` 的 Drive 腿）
- ❌ 让中枢自动执行危险动作（执行类仍走既有桥 + 白名单 + 人工确认）
- ❌ 替代现有任务看板（中枢把待办**写进**看板，不另造一个看板）

---

## 2. 场景（先场景，后技术；每条挂真实锚点）

| # | 场景（用户在用系统做什么） | 真实锚点 | 判错/失败的代价 |
|---|---|---|---|
| S1 | 一个会话/任务开了头没结，过两天忘了收尾 | `~/.dsh/sessions/<slug>/<session-id>/session.v4.jsonl.zstd`；`~/.dsh/filebridge/outbox/*.result.json` | 遗留任务，返工 |
| S2 | 同一个文件连着改、同一条命令连着失败 | 工作区 `git status`/文件 mtime；桥结果里 `status=error` 的记录 | 白耗时间，无人提醒 |
| S3 | 资料收集断片：剪贴板里一条链接、窗口标题里一个关键词，过后找不到 | `wl-paste`（实测可用）；`pyatspi` 窗口标题（实测可用） | 资料散落 |
| S4 | 想做日/周回顾，但"这周干了什么"要手工翻记录 | 会话事件流 + 待办完成记录 | 回顾成本高，通常就不回顾了 |
| S5 | 中枢判断错了，弹了一条没用的通知 | —— | **信任下降**，用户会直接关掉整个能力 |

**S5 是设计的主约束**：整个通知子系统的复杂度，几乎全部来自"如何少弹、弹了必须有依据"。

---

## 3. 用法（四问；技术实现必须能回答每条）

### 3.1 谁触发

- **采集器**（常驻）：事件驱动 + 定时轮询（订阅/轮询粒度见 §4.1）。
- **加工器**：按批触发 —— 事件累积到阈值（如 ≥ 20 条）或每 5 分钟一次。
- **通知器**：只在加工器产出的条目**通过重要性阈值**时触发。
- **用户**：UI 上手动改报表/待办；通知里的按钮（打开/忽略此类）。

### 3.2 何时触发

- **只在空闲窗口**弹通知：`org.gnome.Mutter.IdleMonitor.GetIdletime` **实测可用**（返回 476 ms），规则如 `idletime ≥ 60s`。
- **不在**：正在打字、正在跑一次会话回合、屏幕全屏演示、系统勿扰模式开启时。
- 事件落定后处理（会话回合结束、命令结果回写），避免处理半成品。

### 3.3 延迟预算

中枢是**分钟级**系统，不是实时系统：

| 环节 | 预算 |
|---|---|
| 采集落库 | 秒级（≤5s） |
| 归纳出待办 | 1–5 分钟（批量） |
| 通知送出 | 落在最近一个空闲窗口，最坏 ≤ 15 分钟 |

延迟换来的正是"不打扰"。

### 3.4 失败语义（逐条明确，且必须可机械断言）

| 失败 | 语义 |
|---|---|
| 某个采集器挂掉 | 记日志、其余采集器继续；**绝不阻塞**用户操作 |
| 加工器失败 | 保留原始事件，下轮重试；不产出半成品待办 |
| 判断不确定 | **沉默**（默认不发） |
| 通知发送失败 | 降级为看板卡片，不重试刷屏 |
| UI 写入冲突 | 以人为准：人改过的字段机器不再覆盖，只追加"建议变更" |
| 中枢整体宕机 | 用户感知为零（无依赖、无阻塞、systemd 可重启） |

---

## 4. 系统分层

```
┌── 采集层 collectors（只读，独立进程/线程）────────────────────────┐
│ session-watcher  clipboard-watcher  window-watcher  fs-watcher  drive-watcher │
└───────────────┬───────────────────────────────────────────────────┘
                │ 事件（JSONL / Unix socket）
┌── 归集层 store ───────────────────────────────────────────────────┐
│  events（append-only，SQLite）+ 轮转 + 保留策略                    │
└───────────────┬───────────────────────────────────────────────────┘
                │
┌── 加工层 derive ──────────────────────────────────────────────────┐
│  规则引擎（阈值/模板/去重/冷却） → LLM 归纳 → todos / report_drafts │
└───────┬───────────────────────┬───────────────────────────────────┘
        │                       │
┌── 产出层 ─────────┐   ┌── 交互层 ────────────────────────────────┐
│ 看板卡片(待办)     │   │ notify（Notify+ActionInvoked，带节流）    │
│ 报表 md + 修订表   │   │ web UI（待办/报表编辑/事件流/设置）        │
└───────────────────┘   └──────────────────────────────────────────┘
```

### 4.1 采集层：本机实测可行性（2026-10-15 实测，Fedora 44 / GNOME Shell 50.5 / Wayland）

| 采集器 | 手段 | 可行性 | 实测证据 |
|---|---|---|---|
| `session-watcher` | 复用 `dsh-filebridge` 的 RPC 读会话事件（`session/list` + `session/page`） | ✅ | `dsh-filebridge/INTERFACE.md`；bridge 已稳定运行 |
| `clipboard-watcher` | `wl-paste --watch`（`wl-paste` 在 `/usr/bin`） | ✅ | `wl-paste --list-types` → `text/plain;charset=utf-8` |
| `window-watcher` | `pyatspi` 枚举无障碍树（**无需**开 `toolkit-accessibility`） | ✅（标题级） | 实测枚举出 `gnome-shell` / `Clash Verge` / `ptyxis` 及完整窗口标题；`org.gnome.Shell.Introspect.GetWindows` 实测被拒（`AccessDenied`） |
| `fs-watcher` | 工作区 mtime + `git status`（inotify 头文件在，可 ctypes 直调） | ✅ | 工作区 `~/文档/deepseek-harness/default-workspace`，git 仓库 |
| `drive-watcher` | 复用 `dsh-filebridge` 的 Drive 腿（rclone 1.75.2，走 7897 代理） | ✅ | `dsh-filebridge/tests/verify_drive_live.sh` 已全过 |
| ~~`input-watcher`~~ | `/dev/input/event*` | ❌ **不可行** | 设备是 `root:input 660`，用户在 `disk wheel` 组、无 sudo |
| ~~官方 ActivityWatch 窗口器~~ | aw-watcher-window | ❌ GNOME Wayland 下完全失效 | 官方 FAQ：Mutter 下"there is no way at all to get the current window"；要工作须装扩展 `Focused Window D-Bus` + `2e3s/awatcher`，**启用扩展要注销重登** |

**窗口采集的边界**（写进契约，别指望更多）：只能拿"应用名 + 窗口标题"，**拿不到应用内的文本**；Chromium/Electron 系默认不暴露 a11y（Edge 需 `--force-renderer-accessibility`），实测它就没出现在枚举结果里。窗口标题本身可能含文件路径/搜索词 → 属隐私面，见 §11。

### 4.2 归集层

- 单一 SQLite 库 `~/.dsh/info-hub/hub.db`（stdlib `sqlite3` 3.51.2，**无需外部依赖**）。
- 事件写入 append-only；采集器之间不共享内存，通过文件队列/Unix socket 解耦（一个采集器崩不影响其它）。
- 保留策略：`events` 默认留 180 天，热数据 30 天；报表与修订**永久留**（它们体量小且是资产）。

### 4.3 加工层

两级，先规则后模型（便宜的先做，模型只用于"归纳"）：

1. **规则引擎**：阈值型事实（同文件 3 次改动、同命令 2 次失败、剪贴板连续 3 条同域链接、会话 48h 未收尾）→ 直接产出结构化待办候选。
2. **LLM 归纳**：把一批事件压成"一句建议 + 依据 + 置信度"。模型选择见 §12 待定 2；输出必须**结构化**（JSON schema 校验），校验失败即丢弃。

### 4.4 产出层与交互层

- 待办 → 写既有看板（`~/.dsh/task-board/ledger-v2.json`，78,770 B / 2026-10-14；写入口径见 `dsh-task-board` skill 的 `/api/task-board/action`）。
- 报表 → `~/.dsh/info-hub/reports/*.md`（人可改，见 §5）。
- 通知与 UI → §6、§7。

---

## 5. 数据模型：系统数据 / 报表分离 + 改动留痕（本项目的核心设计）

### 5.1 三层分离

| 层 | 载体 | 谁能写 | 可否手改 | 用途 |
|---|---|---|---|---|
| **L1 events**（系统数据） | `hub.db: events` | 机器（append-only） | ❌ 不可改、不可删 | 审计、重放、重算 |
| **L2 derived**（机器推断） | `hub.db: todos / suggestions` | 机器，可被人覆盖 | 人可改**状态与字段**（写 `human_override`） | 待办与建议 |
| **L3 reports**（报表，人的资产） | `reports/<name>.md` + `hub.db: report_revisions` | **人为主**；机器只写"草稿/建议变更" | ✅ 随时手改 | 给人看的结论 |

**分离铁律**：机器**永不**直接改 L3 正文；它只能（a）新建草稿、（b）追加一条"建议变更"让人确认。这条是 G5 的唯一判据，要写进验收脚本。

### 5.2 报表改动留痕（双保险）

用户要求"报表每次改动有记录"。做法：

- **正文**：Markdown 文件（人可读、可 `git diff`、可用任何编辑器改）。
- **索引/记录**：SQLite 表

```sql
CREATE TABLE report_revisions (
  id          INTEGER PRIMARY KEY,
  report      TEXT NOT NULL,      -- 报表名（对应 reports/<name>.md）
  rev         INTEGER NOT NULL,   -- 该报表内递增
  at          TEXT NOT NULL,      -- ISO8601 带时区
  author      TEXT NOT NULL,      -- 'human' | 'llm' | 'agent:<name>'
  reason      TEXT,               -- 人写的改动备注（UI 上必填或可填）
  before_hash TEXT NOT NULL,      -- sha256(上一版正文)
  after_hash  TEXT NOT NULL,      -- sha256(本版正文)
  diff        TEXT NOT NULL,      -- unified diff（可截断存储，完整版在 git）
  UNIQUE(report, rev)
);
```

- **每次保存 = ① 原子写 md（临时文件 + rename）② 追加一行 revision ③ `git add/commit`（消息含 report/rev/author）**。
- 因此有两道保险：SQLite 修订表（查询快、自包含）+ git 历史（完整、防篡改、可回滚）。
- UI 必须能展示：这份报表的修订列表（时间、作者、备注、diff）。

### 5.3 事件与产出的最小 schema（草案，实施时落进 INTERFACE.md）

```jsonc
// events
{"id": 123, "ts": "2026-10-15T16:40:00+08:00", "source": "dsh-session|clipboard|window|fs|drive",
 "kind": "turn.end|clip.copy|window.focus|fs.change|drive.pull",
 "payload": {"...": "按 source 定义"}, "fingerprint": "sha256(payload)",
 "privacy": "normal|sensitive"}

// todos
{"id": "t-0001", "created_at": "...", "source_event_ids": [123, 130],
 "title": "…", "why": "一句话依据", "confidence": 0.62,
 "status": "open|doing|done|dropped", "human_override": false,
 "board_card_id": "…"}

// notifications（发送台账，用于节流与复盘误报率）
{"id": 7, "ts": "...", "kind": "todos_digest|focus_hint", "body": "...",
 "basis_event_ids": [123], "shown": true, "action": "open|ignored|dismissed|timeout"}
```

`notifications` 台账是**度量误报率**的唯一数据源（S5 的解药）：每周统计"忽略率"，超阈值就自动降频/停发某类。

---

## 6. 系统通知（弹窗）：能，已实测

用户问"能不能做成一个系统弹窗的形式"。**实测可用**：

- `gsettings get org.gnome.desktop.notifications show-banners` → `true`
- `gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications --method org.freedesktop.Notifications.Notify ...` → **返回 `(uint32 69,)`，屏幕弹出**
- 接口具备：`ActionInvoked(us)` 信号（按钮回调）、`NotificationClosed(uu)`、`GetCapabilities`

### 6.1 通知设计（"不烦人"的具体条文）

| 约束 | 值（初值，落在 `hub.config.yaml`，可调） |
|---|---|
| 全局频率 | ≤ 1 条 / 分钟，≤ 6 条 / 小时 |
| 同类频率 | ≤ 3 条 / 天 |
| 触发条件 | **界面事件驱动**（用户 m01440 定）：在"用户正在做什么"的边界上评估——窗口焦点变化 / 新应用被打开（含浏览器开页）时评估一次；`idletime ≥ 60s`（人不在）不弹；勿扰不弹 |
| 必带 | **依据**（可展开看原始事件）+ 一键 **「忽略此类」** |
| 措辞 | 只陈述事实与可选动作，不用催促语（"你该…了"） |
| 形态 | 普通 banner（有 `transient` 自动消失）+ 按钮：`打开` / `稍后` / `忽略此类` |

反 Clippy 纪律：**没有依据不发**。这条同时回答了论文那 ~50% 误报的现实问题——不是靠更聪明的模型，而是靠"发之前必须能指出是哪几条事件触发的"。

**触发时机（用户 m01440 原话）**：「应该是我在做什么的时候弹，比方说我打开浏览器的时候监测我的界面，这样就能知道我在干什么」⇒ 通知的**触发源是界面事件**（`window.snapshot` 变化 + 应用上下文），不是定时器。定时只用来"评估"，弹出的那一瞬必须落在一次界面切换上；这样通知天然贴着用户当下的注意力，而不是随机打断。代价（必须写进 M2 验收）：窗口标题在 Wayland 上对 Chromium/Electron 拿不到（M0 实测），"打开浏览器"只能靠进程名/标题前缀识别，不能靠页面内容——要精确到页面，得另加手段（浏览器扩展或 CDP）。

### 6.2 通知 → 交互闭环

`ActionInvoked` → 中枢写回台账（`action=open|ignored`）→ "忽略此类"直接写偏好（该类降频/停发）→ 周报里给出"通知有效性"统计。这样**打扰预算是可度量、可自动收敛的**。

---

## 7. UI 设计

- **形态（待定，见 §12-4）**：优先 **本地小 web**（stdlib `http.server` + 单页 HTML，绑 `127.0.0.1`），理由：零依赖、不动 DSH 本体、随时可开；次选做成 DSH GUI 的插件页。
- **四个页面**：
  1. **今日**：待办列表（勾选/改状态/手加/编辑 `why`）+ 今日通知台账（含"这条有用吗"一键反馈）
  2. **报表**：列表 + 编辑器 + 修订历史（时间/作者/备注/diff）
  3. **事件流**（只读）：筛 source/kind/时间，用来调试与审计
  4. **设置**：节流阈值、静默时段、各类通知开关、保留天数
- **写入门禁**：报表保存必须走"写 md → 记 revision → git commit"三步（§5.2），UI 不给绕过路径。

---

## 8. 复用件（本项目的"前期工作"，不重造）

| 复用件 | 位置 | 提供什么 |
|---|---|---|
| DSH 桥（会话读写） | `dsh-filebridge/`（`INTERFACE.md`、`bin/dsh-bridge`） | 按 `session_id` 发消息、读事件流、回写结果；采集层与加工层都能用 |
| Google Drive 腿 | 同上（`drive.py`、`verify_drive_live.sh`） | rclone 上下行、导出归一化、幂等 |
| 任务看板 | `~/.dsh/task-board/ledger-v2.json` + `/api/task-board/action` | 待办出口（A 形态的落地） |
| 浏览器 CDP 控制 | `~/.local/share/dsh-browser/cdp.py` | 网页侧采集/操作的通道（可选） |
| 通知/空闲接口 | D-Bus（§6、§3.2） | 弹窗 + 空闲判定 |
| 经验与线格式 | KB `30-Agent工程/30.11-Agent工具链踩坑实测.md`@2026-10-15（§40、§44） | DSH 0.2.0-rc.2 RPC 线格式、CDP 操作坑 |
| 调研背景 | `gemini-spark-mac-sandbox-调研-2026-10-14.md`（第十、十一轮） | 通道能力与约束（每往返 2–7s、绝不用 sync 等） |

---

## 9. 分期路线（每期独立可验收、可停）

| 期 | 内容 | 产出 | 验收（机械可判） |
|---|---|---|---|
| **M0 地基** | SQLite schema + 事件总线 + 3 个只读采集器（session/clipboard/fs） | 库里能看到本机在发生什么 | `hub-collect --once` 退出 0；`sqlite3` 查询近 1h 事件数 > 0；`--selftest` 全过 |
| **M1 出口 A** | 规则引擎 → 待办 → 写看板卡片（**不弹窗**） | 待办自动出现在看板 | 造 5 条合成事件 → 恰好产出 N 条待办；重复跑不重复建卡（幂等） |
| **M2 通知（B 生效）** ✅ 2026-10-15 | notify + 五道门（开关/勿扰/空闲/**界面事件**/节流）+ 台账 | 通过阈值的才弹，弹了有依据 | 见 §18：`tests/verify_m2.sh` 13/13 |
| ↳ **M2b 通知按钮真的有用** ✅ 2026-10-15（§24） | `notifications.dbus_id` + `act()` + `listen` | 点了按钮就改待办（留痕），按钮不再是摆设 | 见 §24：`tests/verify_m2b.sh` 16/16 |
| ↳ **M2c「忽略此类」** ✅ 2026-10-15（§26） | `kind_suppressions` + 门 6（选候选时跳过被压类别） | 一条通知点一下，压住**整类** 7 天（到期自动恢复、可解除） | 见 §26：`tests/verify_m2c.sh` 11/11 |
| ↳ **M2d 按钮补全** ✅ 2026-10-15（§27） | 动作键 → 稳定键映射 + `notify.open_command` / `todos.snooze_until` | 「打开」只留痕（可选开 URL、不改状态）、「稍后」推迟 60 分钟后**再**提示一次 | 见 §27：`tests/verify_m2d.sh` 11/11 |
| ↳ **M2f 通知的触发面** ✅ 2026-10-16（§34） | `hub-collect --notify`：只有**这一轮真收进新的 `window` 事件**才评估一次通知 | 让「只在检测到界面活动时评估」落在触发点上，而不是靠门挡 | 见 §34：`tests/verify_m2f.sh` 9/9 |
| ↳ **M2e 通道自检** ✅ 2026-10-16（§31） | `hub-notify test`（`probe()`：绕开门 1–8、不碰待办、台账 `kind='probe'`） | 用户随时可自证"这台机器弹得出来"；并更正「B 形态从未弹过」的错述 | 见 §31：`tests/verify_m2.sh` T13/T14 ⇒ 该套 15/15 |
| **M3 UI + 报表留痕** ✅ 2026-10-15 | web UI + 报表三层分离 + revision + git | 手改报表并留痕 | 见 §19：`tests/verify_m3.sh` 16/16 |
| ↳ **M3b 清单也能手改** ✅ 2026-10-15（§28） | `todo_revisions` + 控制台清单按钮 + CLI 查询 | 清单的每次改动留痕，两本账分离 | 见 §28：`tests/verify_m3b.sh` 9/9 |
| ↳ **M3c 证据链** ✅ 2026-10-15（§29） | `store.todo_evidence/todo_detail` + `/api/todo/detail` + `hub-store todo <id>` + 门 8「无依据不发」 | 每条待办能摊开成原始事件；摊不开的不许变成通知 | 见 §29：`tests/verify_m3c.sh` 9/9 |
| **M4 智能化** ✅ 2026-10-15（§20–§22） | LLM 归纳 + 周报 + 误报率统计与自动降频 | 周报与"通知有效性" | 见 §20/§21/§22：`verify_m4a.sh` 8/8 + `verify_m4b.sh` 9/9 + `verify_week.sh` 9/9 |
| ↳ **M4a 有效性统计 + 自动降频（本地可验收的那半）** ✅ 2026-10-15 | 通知响应/忽略判定 + 门 5b 降频 | 「哪些通知白弹了」可查、自动收紧 | 见 §20：`tests/verify_m4a.sh` 8/8 |
| ↳ **M4b LLM 归纳** ✅ 2026-10-15（模型解耦成配置，不再是阻塞项） | 归纳层 + 脱敏 + 证据审计 | 报表 `digest` 区块 | 见 §21：`tests/verify_m4b.sh` 9/9 + 真机闭环 |
| ↳ **M4c 周报** ✅ 2026-10-15 | 归纳条目/待办/通知按周聚合 | `reports/hub-week-<年>-W<周>.md` | 见 §22：`tests/verify_week.sh` 9/9 + 真机生成 |
| ↳ **M4d 每周归纳（本地后端）** ✅ 2026-10-16（§32） | 默认后端换成**离线确定性**后端 + 周日定时器 | 无人值守也能每周出一份带证据的归纳 | 见 §32：`tests/verify_m4d.sh` 12/12 |
| ~~**M5 外部源：bilibili 账号监测**~~ ✅ 2026-10-15（§23） | 新采集源 `bilibili`（收藏夹/动态里的工具）+ 与知识库对账 | 「我刷到的工具，哪些还没进 KB」自动成清单 | 合成 5 条收藏 → 恰好产出 N 条"未整合"待办；已在 KB 的工具**不**产出；重复跑幂等 |
| ↳ **M5b 浏览器历史当来源** ✅ 2026-10-16（§33） | 新采集源 `browser-history`（Edge/Chrome `History`，只读副本）+ 规则改双源 | 「我看过的仓库页」里 KB 查不到的自动成待办 | 见 §33：`tests/verify_m5b.sh` 17/17 |
| ↳ **M7 静默截屏** ✅ 2026-10-16（§35） | 新采集源 `screenshot`：ScreenCast 取帧（无闪光/无快门声）、**键鼠活跃才取**、缩放图 + `sensitive` 元数据、留 7 天 | 用户 m01149/m01154：把"我在做什么"也留成可查的事实（不活跃完全不反应） | 见 §35：`tests/verify_m7.sh` 24/24 + 真机取帧证据（1280x800、无弹框） |
| ↳ **M8 全量浏览历史 + 分类与一条总结** ✅ 2026-10-16（§36） | 历史来源改为**全量**（`kinds:[history]`，每条 = 时间 + 网址 + 站点 + 用途 + 标题前 120 字）+ `history.py` 16 类用途分类 + 报表 `history` 区块（一条总结） | 用户 m01809：「只留历史记录+一条总结……对所有浏览历史生效……可以简单分类，按照用途和网址」，且**卡片不推送** | 见 §36：`tests/verify_m8.sh` 16/16 + 真机 9335 条历史入库 |

| **M6 运维（真跑起来）** ✅ 2026-10-15（§25） | systemd user 单元（5 定时器 + UI）+ 安装器 `bin/hub-install` | 采集/派生/报表/周报/清理**自动**跑，重启自恢复 | `systemctl --user list-timers` 见 5 个 NEXT；journal 里每轮 `Starting` 且退出 0；`hub-store stats` 事件数随时间增长 |

### 9.1 M5 的口径（先定死，实施时逐条落进 INTERFACE.md）

- **场景挂了谁的锚**：用户刷 bilibili 看到工具（收藏/点赞/动态）→ 之后想找"当时那个工具" → 只在 KB 里找不到（S：KB 有 `30-Agent工程/` 等归档，检索成本 ≈ 0）。
- **触发**：定时拉一次（不进通知），或用户 `bin/hub-bili pull`；**失败语义**：拉不到就记 `last_error`、不产生待办，绝不猜。
- **对账判据（可机械判）**：工具名/仓库名（`owner/repo`、`xxx.exe`、`GitHub 链接`）在 KB 全文（`~/knowledge_base`）里的命中情况 ⇒ 0 命中 = 未整合，产出待办；命中 = 静默。
- **取数（待用户定，二选一）**：① 走已登录的 Edge（CDP，与 Gemini 那条通道同源）读收藏夹 API；② 用户手动导出/RSS。**默认从 ① 开始，只读、只取标题+链接**。
- **边界**：只做"工具清单对账"，不做视频内容解析（那属于 `video-analyze` skill）；不自动改 KB，只出待办。

每期结束都：`tests/verify_*.sh` 全绿 + 退出码 0 + 更新 `INTERFACE.md` + 提交（沿用 `dsh-filebridge` 的做法）。

---

## 10. 规模评估

### 10.1 人天（我按"我实际做过 `dsh-filebridge` 的速度"估）

| 工作流 | 内容 | 人天 |
|---|---|---|
| W1 存储与事件总线 | SQLite schema、迁移、保留策略、单写多读 | 2–3 |
| W2 采集器 ×4 | session（复用桥）/ clipboard / window(pyatspi) / fs | 3–4 |
| W3 加工层 | 规则引擎 + LLM 归纳 + 去重/冷却 + schema 校验 | 3–5 |
| W4 通知器 | Notify/ActionInvoked + 节流 + 空闲 + 台账 | 1–2 |
| W5 Web UI | 今日/报表/事件流/设置 + 修订历史展示 | 4–6 |
| W6 部署与运维 | systemd user 服务、日志轮转、备份、审计 | 1–2 |
| W7 验收与文档 | 各期 `verify_*.sh`、INTERFACE.md、README | 2–3 |
| **合计** | | **16–25 人天** |

单独可选项：复现论文判断层（Active-RM 本地推理 + 自造评测集）**+5–10 人天 + 显卡**；训练级复现**不建议**（且键鼠流不可行，见 §12-3）。

### 10.2 资源

| 资源 | 估计 | 说明 |
|---|---|---|
| 存储 | 每千事件 ≈ 0.5–1 MB；一年 < 1–2 GB | SQLite 足够；报表与修订可忽略 |
| 常驻内存 | 100–300 MB（4 个采集器 + 加工器） | LLM 走云端则≈0；本地 8B 4-bit ≈ 5–6 GB 显存 |
| 磁盘 I/O | 极低（事件批量写） | 不与用户操作抢资源 |
| 对新依赖 | **零**（stdlib + 已有 rclone/pyatspi/wl-paste） | 本机无 uv、pypi 不稳定，故坚持 stdlib |

### 10.3 关键风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 通知打扰（S5） | 能力被关掉 | 节流 + 依据必带 + 忽略率自动降频 |
| LLM 归纳不稳定/贵 | 待办质量差 | 规则优先，模型只做归纳；结构化校验；失败即沉默 |
| 窗口标题含隐私 | 数据敏感 | `privacy` 标记、标题可选不上库、本地留存 + 可一键清空 |
| 采集器与用户抢资源 | 卡顿 | 只读 + 低优先级 + 批量 |
| 报表被机器覆盖 | 用户资产损坏 | §5 分离铁律 + 验收脚本锁定 |
| 规则越权（弹窗变催促） | 违反 §一 本意 | §13 的例外条文 + 机械判据（无依据不发） |

---

## 11. 隐私与数据边界

- 全部数据**只在本机** `~/.dsh/info-hub/`；不上云（Drive 只在中转/备份场景显式使用）。
- `events.payload` 里窗口标题标记 `privacy=sensitive`（可选关闭）。
- 剪贴板只存**摘要/指纹 + 可选正文**（默认存正文但可一键清空 + 保留 30 天）。
- 一键"暂停采集"（UI 上明显位置）+ 一键"清空今日"。
- 不采集：键盘/鼠标事件、密码框内容、通知正文里的凭据。

---

## 12. 待定 / 前沿问题（五件套）

### 12-1 通知的类型白名单

- **是什么**：第一版只发哪几类通知？
- **为什么重要**：类型越多越容易烦人；先窄后宽才安全。
- **选项**：① 只发"每日待办汇总"一条；② 汇总 + "你有一条会话 48h 没收尾"；③ 再放开"连续失败/重复劳动"提示。
- **权衡**：① 最不烦但价值低；③ 价值高但最接近 Clippy。
- **推荐**：**① 起步**，两周看忽略率，再放开 ②。

### 12-2 加工层用哪个模型 —— **已解耦，不再是阻塞项**（2026-10-15，§21）

口径改成：加工层 = `digest.command` **一条命令**（提示词走 stdin、结果走 stdout），换来换来换模型都不改 info-hub 代码；契约（只输出 JSON + 每条挂真实事件 id）写在提示词里，输出另有机械审计兜底。已实测的后端 = 一个专用 DSH 会话（`bin/hub-digest-dsh --session …`，§21）。
**要不要接、接哪个、多久跑一次，仍是用户的决定**：默认 `digest.enabled=false`、`command=[]`。

### 12-3 要不要复现论文的判断层

- **是什么**：跑 `thunlp/ProactiveAgent` 的 Active-RM（LLaMA-3.1-8B）来决定"该不该打扰"。
- **为什么重要**：这是论文的核心贡献，但论文自己也只有 F1 66%（误报 ~50%）。
- **选项**：① 不复现，用"有依据才发"的硬规则；② 用 Active-RM 当第二道闸；③ 在你自己积累的事件上微调。
- **权衡**：① 简单可控；② 多一层依赖、未必更好；③ 需要标注数据，远期。
- **推荐**：① 起步；② 仅在 M4 有实测收益时再上。

### 12-4 UI 形态：独立本地 web 还是 DSH 插件

- **是什么**：中枢的界面挂在哪。
- **为什么重要**：影响开发量与"你愿不愿意每天打开"。
- **选项**：① 独立 stdlib 本地 web（`127.0.0.1:PORT`）；② 做成 DSH GUI 插件页；③ 纯文件 + 编辑器（不写 UI）。
- **权衡**：① 零依赖、独立可停；② 体验统一但要改插件与前端构建；③ 最省事但"手改+留痕"体验差。
- **推荐**：**① 起步**，若你天天用再考虑并入 DSH。

### 12-5 事件留存与隐私边界

- **是什么**：窗口标题/剪贴板正文是否入库、留多久。
- **为什么重要**：中枢的全部风险都在这里。
- **选项**：① 标题入指纹不入正文；② 全入，180 天；③ 全入，30 天 + 加密。
- **权衡**：① 最安全但归纳质量下降；② 质量最好；③ 折中。
- **推荐**：**② 起步但保留期 30 天 + UI 一键清空**，跑两周看实际收益再收紧。

---

## 13. 与 `AGENTS.md §一` 的关系（**需用户逐字确认，尚未落盘**）

现状：`AGENTS.md §一` 明令「**禁止**任何形式的到点提醒 / 主动推送 / 主动推荐（含"要不要开始"这类问句），不建提醒 cron」。

用户本次选择 **A+B**，B 正是"面向用户的主动通知" ⇒ **要让它合法，必须给 §一 开一条最小例外**。按 §六（权威层写入须逐字确认），以下是**草案原文**，请你过目、修改或点头后我才落盘：

> **例外（2026-10-15 定）：信息中枢的系统通知不受本节限制**，但必须同时满足：
> ① 只在检测到界面活动时评估（有新的 `window` 事件），且人不在（空闲 ≥ 60 秒）或系统勿扰时不发；**绝不按时间表推送**；
> ② 频率上限：全局 ≤ 1 条/分钟、≤ 6 条/小时，同类 ≤ 3 条/天；
> ③ 每条通知必须附「依据」（可展开到原始事件 id），**无依据不发**；
> ④ 通知必须提供可点的动作，且每个动作只做它字面说的事：**「打开」**只打开控制台并留痕（不改待办状态）、
> **「稍后」**把这条推迟 60 分钟后再提示一次、**「忽略此类」**把该类别压住 7 天（到期自动恢复，也可随时解除）；
> ⑤ 措辞只陈述事实与可选动作，不使用催促性表述；
> ⑥ 忽略率高的类别自动降频：忽略率 > 50% 且样本 ≥ 5 时，该类每天最多 1 条。

上面六条与代码的对应（都可机械验收）：① = **触发面（M2f，§34：只有这一轮真收进新的 `window` 事件才评估）**
+ 门 3（空闲）+ 门 4（界面事件），
② = 门 5（`min_interval_s`/`max_per_hour`/`max_per_day_per_kind`），③ = 通知正文只带依据条数、
措辞纪律见 §6.1，④ = 动作回写（`notify.act`：`open`/`snooze`/`ignore-kind`）+ 门 6 + `kind_suppressions`
（`tests/verify_m2c.sh` 11/11、`tests/verify_m2d.sh` 11/11），⑤ = 措辞纪律（§6.1 的`不催促`条），
⑥ = 门 5b 的降频（`tests/verify_m4a.sh` 8/8）。**在你点头之前，此例外不生效**——`notify.enabled` 保持 `false`。

在用户点头之前，**该例外不生效**，中枢只做 A（全部产出进看板/库，不弹窗）。

---

## 14. 落位与命名

```
$HUB_ROOT/
├── PROJECT.md              # 本文档（设计 + 规模）
├── INTERFACE.md            # 契约（实施时建：事件/todo/报表/通知 schema 与 CLI）
├── hub.config.yaml         # 唯一数值源（节流阈值、保留期、路径、模型）
├── src/infohub/
│   ├── config.py  store.py  bus.py
│   ├── collectors/{session,clipboard,window,fs,drive}.py
│   ├── derive/{rules.py, summarize.py}
│   ├── notify.py   ui.py
│   └── cli.py
├── bin/{hub-collect,hub-derive,hub-notify,hub-ui}
├── tests/verify_m0.sh … verify_m4.sh
└── packaging/info-hub-{collect,derive}.service
```

数据目录：`~/.dsh/info-hub/`（`hub.db`、`reports/`、`state/`、`logs/`）—— 与 `~/.dsh/filebridge/` 并列，保持"数据在 `~/.dsh` 下、代码在工作区"的既有格局。

---

## 15. 验收判据（每期都要机械可判，退出码 0）

- M0：`hub-collect --once` 退出 0；库里近 1h 事件 > 0；`--selftest` 全过。
- M1：5 条合成事件 → 产出确定条数待办；重复执行不重复建卡（幂等）。
- M2：1 分钟内触发 2 条同类 → 只弹 1 条；`--fake-idle 0s` → 不弹；`hub-notify test` → 弹 1 条探针且台账 +1（记 `kind='probe'`、不占 `notified_at`；`tests/verify_m2.sh` T2/T3/T4/T13/T14）。
- M3：改报表一次 → `report_revisions` +1、md sha256 变化、`git log -1 --format=%s` 含报表名与 rev；回滚该 commit 后内容等于上一版。
- M3b：控制台改清单一次 → `todo_revisions` +1（`human/status/open→done/ui`）、待办 `human_override=1`；再跑一次规则引擎**不翻回、也不多写记录**；「稍后」⇒ `snooze_until` 落库且状态仍 open；非法状态 400、未知 id 404、越界 400、Host 403；改清单**不写** `report_revisions`（两张账本分离）。
- M4a：合成「已响应/被忽略/判定期内/无指针」四类历史 → `samples`/`ignored_rate` 数字精确、`downshifted` 只在样本足且忽略率超阈值时为真；降频类被门 5b 拦住（`reason=throttled-downshifted`）；把阈值调高后同一数据能正常弹 ⇒ 证明拦住它的是 5b。
- M4a：见上（忽略率判据 + 降频门）。
- M4b：合成事件集 → 摘要每条带 `evidence` 且**指向真实事件 id**；挂不存在 id 的条目被丢；加工层不是 JSON / 崩了 ⇒ 退出 1 不写库；出本机的提示词里**凭据已脱敏**。
- 把某类忽略率灌到 > 50% → 该类自动降频（断言频率下降）。（M4a 已覆盖）
- M4c：周报文件生成、区间半开边界正确、条目逐条带 `evidence`、**不改**可手改的报表、`--dry-run` 不落盘。
- M2c：点「忽略此类」⇒ 该类被压住 + 这一条 `dropped`；同类再出现被门 6 拦住；**被压类别不阻塞其它类别**；`unsuppress` 后又能弹；到期自动恢复；`--days 0` 打回。
- M2d：默认按钮集 = 打开/稍后/忽略此类且真的传给 gdbus；「稍后」⇒ `snooze_until` 落库、`notified_at` 清空、状态仍 open，未到点被门 7 拦（`reason=snoozed`）、到点后**同一条**再提示一次；「打开」⇒ 台账记 `open`、状态不变、`{url}` 替换正确，命令起不来时退出码仍 0 只记 `open_error`；`notify.actions` 可改；未知动作键打回。
- M3c：种子造**真事件**、由规则引擎自己产出待办 ⇒ 每条待办的 `source_event_ids` 逐条能在 `events` 里查到（`missing=0`）；
  `/api/todo/detail?id=` 同时给出「证据链 + 改动记录」，未知 id ⇒ UI 404 + CLI 退出 1；证据链**只读**（`events`/`todos`/`todo_revisions` 一字不动）；
  证据里查不到的 id **如实标 `missing`**（不假装有依据）；门 8 落码：候选无依据 ⇒ `reason=no-evidence`、台账 0 条，
  且它是 `continue` 不是 `break` ⇒ **无依据的候选不许饿死有依据的候选**（[tests/verify_m3c.sh](tests/verify_m3c.sh) 9/9）。
- M6（运维）：`bin/hub-install --dry-run` 列出 7 个单元；真装后 `list-timers` 5 个 NEXT 非空；`systemctl --user start info-hub-collect.service` 退出 0；**关掉一个采集器命令后整轮仍退出 0**（失败隔离）；剪贴板连续失败 → 第 3 轮被冷却跳过（`tests/verify_m0.sh` T14）。
- M5b：假浏览器库上只采「仓库页 + 视频页」两类（无关页面、站点路径段、窗口外记录都不入库）；同一目标只留最近一次访问、视频标题去掉站点尾巴；
  再采游标挡住重复；`privacy=sensitive`；规则恰好 1 条待办且 `why` 点明「来自浏览器历史」、证据是真事件；`max_items` **每类**各算；
  库不存在 ⇒ 退出码 0 且错误进 `run:browser-history`；`enabled=false` 不采；**取数后浏览器库字节不变**（只读证据）；
  站点路径不出条目 / GitLab 认 host / 微秒→ISO（[tests/verify_m5b.sh](tests/verify_m5b.sh) 17/17）。
- M4d：本地后端只引用**上游提示词里出现过**的事件 id（不在提示词里的 id 不许出现在 `evidence`，否则会被 M4b 审计丢掉）；
  同输入两次跑逐字节相同；提示词里没有事件 id、或窗口为空 ⇒ `{"items": []}`（无依据不发）；
  接进 `digest.run` ⇒ 落库 1 行、同输入 `reused` 且**不再调后端**；待办证据老于 `max_events` 也被带进提示词、条目没被审计丢掉；
  `digest.timer` 周日跑且早于周报、`hub-install --dry-run` 纳入安装面（[tests/verify_m4d.sh](tests/verify_m4d.sh) 12/12）。

---

## 附：本块实测证据清单（2026-10-15）

| 结论 | 命令 | 结果 |
|---|---|---|
| 通知可用 | `gdbus call ... Notify` | 返回 `(uint32 69,)`，屏幕弹出 |
| 通知有按钮回调 | `busctl --user introspect org.freedesktop.Notifications` | `ActionInvoked signal us` 存在 |
| banner 开关 | `gsettings get org.gnome.desktop.notifications show-banners` | `true` |
| 空闲可查 | `gdbus call ... IdleMonitor.GetIdletime` | `(uint64 476,)` |
| 窗口标题可枚举 | `python3` + `pyatspi` | 列出 `gnome-shell`/`Clash Verge`/`ptyxis` 及标题 |
| Shell 内部 API 被拒 | `gdbus call ... Introspect.GetWindows` | `AccessDenied: GetWindows is not allowed` |
| 剪贴板可用 | `wl-paste --list-types` | `text/plain;charset=utf-8` |
| 键鼠流不可行 | `groups` + `ls -l /dev/input/event*` | 用户 `disk wheel`；设备 `root:input 660` |
| 存储/UI 底座 | `python3 -c "import sqlite3,http.server"` | sqlite 3.51.2 OK |
| 看板可作出口 | `ls -l ~/.dsh/task-board/` | `ledger-v2.json` 78,770 B |
| 既有服务 | `systemctl --user list-units` | `dsh-filebridge`、`dsh-wg-bridge` active |

---

- M8：[tests/verify_m8.sh](tests/verify_m8.sh) 全过、退出码 0（16 项：分类单元 12/12、条目字段与截断、全量入库 `browser.history`/`sensitive`、同网址只留最近、游标幂等、`summarize` 计数与一句话、窗口收窄、非仓库页不产假卡、报表 `history` 区块、fail-soft、只读、CLI 两面）。

## 16. M0 实施记录（2026-10-15，已完成）

- 代码：`INTERFACE.md` v0.1 + `hub.config.yaml` + `src/infohub/{config,events,store,bus,cli,selftest,entry_collect,entry_store}.py` + `src/infohub/collectors/{session,clipboard,window,fs}.py` + `bin/{hub-collect,hub-store}` + `tests/verify_m0.sh`。
- 契约决策（M0 内定下，改则同时改本节与 INTERFACE.md）：采集器签名 `collect(config, store)`；`--json` 时人读日志走 stderr（stdout 可直接 `json.load`）；锁文件 `state/collect.lock` 被占时本轮跳过、退出 0。
- 验收：`bash tests/verify_m0.sh` → **13 passed, 0 failed，退出码 0**（T1 selftest 15/15 内含：指纹稳定/去重/游标抑制/坏采集器不致命/spool 重放/prune/建表齐全）；真实数据首轮入库 **7784 条**（dsh-session 7732、clipboard 1、window 1、fs 50），库 4.1 MB。
- 实机发现：`pyatspi` 在且能列出 `gnome-shell` / `clash-verge` / `ptyxis` 三类窗口（与 PROJECT.md §4.1 的实测一致）；`wl-paste -n` 可用。
- **隐私现状（M0 就是"原样存"）**：剪贴板正文与窗口标题按 PRIVACY.md 标 `sensitive` 但**未脱敏入库**；实测首轮就采到一条含 `token=` 的 URL。脱敏/掩码属 M4，M0 的边界是"只存本机 sqlite、不出网"。

---

## 17. M1 实施记录（2026-10-15，已完成）

- 新增：`src/infohub/rules.py`（3 条规则 + `derive`）、`src/infohub/board.py`（看板建卡，幂等靠 `todos.board_card_id`）、`src/infohub/entry_rules.py` + `bin/hub-rules`、`tests/{verify_m1.sh,board_stub.py}`；`store.py` 加 `dedupe_key` 列级迁移与 `upsert_todo/todos/set_todo_status/set_todo_board_card/todo_exists`。
- 验收：`bash tests/verify_m1.sh` → **11 passed, 0 failed，退出码 0**；M0 回归 **13 passed, 0 failed**（列级迁移没破坏 M0）。
- 合成事件 8 条（同一文件 3 次改动 + 同域剪贴板 3 条 + 3 天没收尾会话 1 条 + 刚收尾会话 1 条）→ **恰好 3 条待办**，四个非目标事件一条不出。
- 幂等与人的优先权：再跑 `derive` → `created=0 refreshed=3`；`todo-set <id> done` 后规则不得翻回（`human_override=1` 保持）。
- 看板出口默认 **关**：`board.enabled=false` 时一个 HTTP 请求都不发（用量子服务器计数断言）；打开后只推 `open 且无卡片指针` 的待办 → 2 张卡，再跑 0 张。
- 待补（M2 才做）：通知与节流；`rules` 目前没有"同命令多次失败"规则，因为 M0 尚未采命令执行结果（需要新采集源或从 `dsh-session` 里解析）。


---

## 18. M2 实施记录（2026-10-15，已完成）

- 新增 `src/infohub/notify.py`（五道门 + 三个后端）、`src/infohub/entry_notify.py` + `bin/hub-notify`（`dispatch` / `status`）；`cli.main_notify`；`store.py` 加 `todos.notified_at` 列级迁移与 `pending_notify_todos` / `mark_todo_notified` / `add_notification` / `notifications` / `notification_gate_counts` / `latest_event`。
- **门（顺序固定）**：`enabled` → `dnd` → `idle` → `ui-event` → `throttle`（min-interval / hour / day-kind）；跳过不是错误，退出码 0，原因进 `reason`。
- **界面事件触发已落地**（用户 m01440 的口径）：`notify.require_ui_event=true` 时，必须有一次**新的** `window` 事件才评估；成功后消费游标 `collector_state['notify:ui']` ⇒ "一次界面切换最多弹一次"。
- **探针 fail-closed**：勿扰/空闲探不清 ⇒ 不弹（宁可不发也不打扰）。发送失败落 `shown=0` 且**不标记** `notified_at`，下一轮还会试（节流兜住）。
- 验收：`bash tests/verify_m2.sh` → **13 passed, 0 failed，退出码 0**；M1 回归 11/11、M0 回归 13/13。全程不弹真窗口（`backend=command` 走 `tests/notifier_stub.py`），两道环境门用假 `gsettings`/`gdbus` 走真解析路径。
- 真后端自检一次：`Env().capabilities()` → `['actions','body','body-markup','icon-static','persistence','sound']`；`send(...)` → `(uint32 70,)`，屏幕弹出一条带按钮的通知。
- 坑：YAML 把 `off` 解析成 `false` ⇒ 代码按 `False == "off"` 归一（`_source()`）；验收种子造 window 事件必须**每次不同**（指纹含 payload），否则第二次 `INSERT OR IGNORE` 被去重掉，"新界面事件"根本没进库。
- 仍未做（M3/M4）：报表与手改留痕、UI、LLM 归纳；**`AGENTS.md` §一 的例外条文仍未落盘，等用户逐字确认**（草稿在 §13）。因此 `notify.enabled` 默认 **false**。

---

## 19. M3 实施记录（2026-10-15，已完成）

- 新增 `src/infohub/report.py`（自动区块 + merge + 修订留痕）、`src/infohub/ui.py`（单线程本地控制台）、`entry_report.py`/`entry_ui.py` + `bin/hub-report`/`bin/hub-ui`；`cli.main_report`/`cli.main_ui`；`store.report_revision()`（取某次修订含 diff）；`hub.config.yaml` 加 `report.*` / `ui.*` 两段；`tests/{m3_seed.py,verify_m3.sh}`。
- **分离怎么落的**：报表 `<root>/reports/<name>.md` 是人拥有的文件；机器只重写 `<!-- hub:auto:begin X -->…<!-- hub:auto:end X -->` 区块，**区块外文字逐字保留**；三个内置区块 `stats`/`todos`/`notifications` 全部只读系统数据。任何一次写入（`author=hub` 生成 / `author=human` 手改）都追加 `report_revisions`（`before_hash`/`after_hash`/`diff`/`reason`）；**内容 hash 没变就不写文件、不留痕**。
- **UI 只有两条写入面**：`POST /api/save`（改报表，`author=human`）、`POST /api/generate`（重写自动区块，`author=hub`）；`GET /api/state` 只读。只绑 loopback、校验 `Host`/`Origin`、`Sec-Fetch-Site: cross-site` → 403、报表名白名单 + 目录逃逸拒绝（`--report ../evil` ⇒ 退出码 1）。
- 验收：`bash tests/verify_m3.sh` → **16 passed, 0 failed，退出码 0**；回归 M2 13/13、M1 11/11、M0 13/13。
- 坑（实测，已修）：① 控制台起初用 `ThreadingHTTPServer` ⇒ sqlite 连接跨线程 `ProgrammingError`（空回复、状态码都没有）；改**单线程 `HTTPServer`**，并把任何异常折成 500 JSON。② 全局参数（`--json`/`--config`/`--report`）在 argparse 里必须写在子命令**之前**（`hub-report --json revisions`），写在后面会 `unrecognized arguments`。
- 仍未做：M4（LLM 归纳 + 周报 + 误报率自动降频）、M5（bilibili 账号监测）；**`AGENTS.md` §一 例外条文仍待用户逐字确认**（§13），`notify.enabled` 仍为 false。

## 20. M4a 实施记录（2026-10-15，已完成；M4b 见 §21）

M4 拆两半：**M4a 纯本地可机械验收**（有效性统计 + 自动降频），**M4b 需要 LLM**（归纳/周报）——M4b 卡在 §12-2 未定（加工层用哪个模型），先做 M4a 并把它用完。

- 新增 `store.notification_effectiveness(window_days, ignore_after_hours, now=None)`：把窗口内 `shown=1` 的通知按有无 `todo_id`、关联待办是否已 `done`、离现在多久，机械归入 `acted` / `ignored` / `pending` / `unlinked` 四类，`samples = acted + ignored`、`ignored_rate = ignored/samples`；**老数据没有指针 ⇒ `unlinked` 不进样本**（不冤枉，也不用编造数据）。
- `notify.effectiveness(config, store)` 判定 `downshifted = samples ≥ downshift_min_samples 且 ignored_rate > downshift_threshold`；`notify.dispatch()` 新增**门 5b**：降频类当天最多再弹 `notify.downshift_max_per_day` 条（**降频，不是屏蔽**），并回 `reason=throttled-downshifted` + `downshifted` 明细。
- 新增 `bin/hub-notify effectiveness [--json]`（只读摊开）；`status` 增加 `downshifted`；`hub.config.yaml` 加 5 个键；`tests/{m4a_seed.py,verify_m4a.sh}`（夹具可造任意「已响应/被忽略/判定期内/无指针」历史，时间相对 now，纯离线）。
- 验收：`bash tests/verify_m4a.sh` → **8 passed, 0 failed，退出码 0**；回归 M3 16/16、M2 13/13、M1 11/11、M0 13/13。
- 真机观察（诚实记账）：真库当前**窗口内 0 条已弹通知**（`notify.enabled=false`，M2 的真实那条是 §18 的自检，已不在窗口）⇒ `hub-notify effectiveness` 真机输出「（窗口内没有已弹通知）」，判定逻辑靠夹具验（8/8）；另把真库复制到临时 root、叠 8 条合成历史跑了一遍：`fs-repeat 弹 9 响应 2 忽略 6 待判 1 忽略率 75% ⇒ 降频`，`dispatch` 回 `throttled-downshifted`（与单测一致）。
- 遗留：M4b 当时以为要等 §12-2 拍板——**已在 §21 解耦成配置并做完**；M5（bilibili）待 §9.1 取数口径。

## 21. M4b 实施记录（2026-10-15，已完成；模型选择**解耦成配置**）

- **关键设计**：加工层不是代码里的某家 SDK，而是 `digest.command` **一条命令**（提示词走 stdin、结果走 stdout），所以 §12-2「加工层用哪个模型」**不再是阻塞项**——换模型 = 换命令。默认 `enabled: false`、`command: []`（什么都没接，不会自己出本机）。
- 新增 `src/infohub/digest.py`（`build_input` / `input_hash` / `render_prompt` / `parse_output` / `run` / `render_block`）、`src/infohub/privacy.py`（`redact` / `redact_obj`）、表 `digests`（`UNIQUE(input_hash)`）、报表区块 `digest`、`bin/hub-digest` + `entry_digest.py`、现成后端 `bin/hub-digest-dsh`（把一个 DSH 会话当加工层，用 `dsh-filebridge` 的 `drop`/`result` 通道）。
- **机械审计**（这是 M4 判据的骨头）：提示词里写死输出契约（只输出 JSON、每条挂流水里出现过的事件 id）；`parse_output` 把不在本次输入里的 id 剔掉，剔完没证据 ⇒ 丢条并计 `dropped`。**"看起来有依据"不算数，id 必须真在库里。**
- **脱敏**：出本机的文本先过 `privacy.redact`——键值凭据、URL 里的 `key=/token=/sig=`、`dsh-auth-…`、私钥块、≥32 位 hex、≥32 位 base64url；只机械替换。`--dry-run` 会把脱敏后的提示词整段打出来（人可复核）。
- 幂等：`input_hash = sha256({window_days, events, todos})`（**看内容不看时钟**，`since` 每秒都变，不进指纹）；同一份输入再跑回 `reused`，不重复花 token；`--force` 才重算。
- 失败语义：命令缺失/超时/退出码非 0/输出不是 JSON ⇒ `DigestError` ⇒ 退出码 1 且**不写任何 digest 行**。
- 验收：`bash tests/verify_m4b.sh` → **9 passed, 0 failed，退出码 0**；回归 M4a 8/8、M3 16/16、M2 13/13、M1 11/11、M0 13/13（六套共 70 项全绿）。
- **真机闭环（2026-10-15 18:08）**：新建专用会话 `session-d1f07377-f0ad-4c0d-99d1-68f019801413`（cwd `/tmp/info-hub-digest`），用真事件库跑了一次真归纳：`reason=ok · 60 条事件 · 提示词 12878 字符 · 脱敏 12 处 · 93.2 秒 · dropped=0`，产出 5 条中文条目，`evidence` 共 18 个 id **全部真实存在于 `events` 表**；已写进 `digests` 并渲染进报表 `digest` 区块（`hub-report generate` → rev#4）。
- 仍未做：周报（把 digest 按周聚合，很小）；`digest.enabled` 仍为 false（要不要每天自动归纳是用户的事；自动归纳本身不弹窗、不违反 §一）。

## 22. M4c 实施记录（2026-10-15，已完成）

- 新增 `src/infohub/week.py`（`week_bounds` / `build` / `render` / `write` / `run` / `list_weeks`）、`src/infohub/entry_week.py`、`bin/hub-week`、`tests/verify_week.sh`；`store.py` 加 `event_counts(since, until)` 与 `digests(since=)`。
- **周报是只读派生产物**：只写 `reports/hub-week-<ISO年>-W<周>.md`，**不碰**可手改的 `hub-report.md`（留痕纪律只属于报表那份）；验收里用 md5 对照证明它一个字节都没动报表。
- 周界 = 本地时区 ISO 周，半开区间 `start <= ts < end`；**文件名按区间最后一天**所在周 ⇒ `--weeks 2` 连报也落在当周文件，不会倒回去覆盖历史周文件。
- 内容：概览表（事件/待办/通知/归纳计数）+ 归纳条目（每条 `evidence: #id`）+ 事件来源表 + 通知有效性表（近 N 天，复用 M4a 判定）。
- 验收：`bash tests/verify_week.sh` → **9 passed, 0 failed，退出码 0**；七套合计 79 项全绿。
- 真机：`./bin/hub-week run` → `reports/hub-week-2026-W42.md`，事件 7784（clipboard 1 / dsh-session 7732 / fs 50 / window 1）、归纳 1 次 5 项、5 条都带真实 `evidence`。
- 仍未做：M5 bilibili 账号监测（等用户定取数口径）；`notify.enabled` 仍 false（等 §13 逐字确认）。

## 23. M5 实施记录（2026-10-15）

**交付**：`bili.py`（可替换来源 + 机械提工具）、`kb.py`（KB 只读全文检索，fail-closed）、
`collectors/bilibili.py`（游标挡重）、规则 `bili-unintegrated`（KB 对账 → 只出待办）、
`tests/{bili_source_stub.py,verify_m5.sh}`（12/12 PASS）。

**口径落实**（§9.1 的每一条 → 落点）：
- 取数不写死：`collect.bilibili.source = command|file`，换来源 = 换配置（与归纳层 M4b 同思路）；
- 只认机械形态的工具（`github.com/a/b` 或 `a/b`）：**裸名字不猜**，宁可漏也不误报假待办；
- KB 对账先查完整 key、再用仓库名兜底（词边界 `[A-Za-z0-9_.-]` 之外才算命中，`owner/tool` 里能命中 `tool`）；
- **KB 不可读 = 抛错不产出**（fail-closed）：绝不把「查不出来」当成「没整合」；
- 判定有缓存（`kb:<key>`，默认 30 天），重复跑不反复全文检索；
- 只出待办、**绝不自动改 KB**（KB 是权威层，AGENTS.md §六）。

**真机实测**：真实 KB（99 个文本文件 / 2.41 MB）单次检索 0.01–0.04 s ⇒ 纯 Python 全文检索够用，不需要索引；
反例正是它要抓的东西：`ProactiveAgent`、`screenpipe` 在 KB 里 0 命中（只在 workspace 报告里出现过）。

**未做 / 待用户拍**：真实 bilibili 取数口径（① CDP 驱动已登录 Edge 只读收藏夹 / ② 手动导出 JSON 喂 `source=file`）；
`collect.bilibili.enabled` 默认 false，等口径定了再开。

## 24. M2b 实施记录（2026-10-15）：通知的按钮真的有用

**问题**：M2 已经把按钮键发出去（`['acted','完成','ignore','忽略']`），但没有东西接 `ActionInvoked`⇒ 按钮是摆设。

**改法**：`notifications.dbus_id` 记下系统通知 id（只从 `gdbus` 后端解析，别的后端一律 `None`，不假装有回调）；`act()` 按 `dbus_id` 回查 → 映射状态（`acted/完成`→`done`，`ignore/忽略`→`dropped`）→ `set_todo_status(human=True)` + 台账 `action` 留痕；`listen` 挂 `gdbus monitor` 把 `ActionInvoked` 转成 `act()`。CLI：`hub-notify act <dbus_id> <action>` / `hub-notify listen [--once] [--timeout S]`。

**验收**：`tests/verify_m2b.sh` **16/16 PASS**（含真总线：`listen` 挂 3s 不报错；离线：假 gdbus 吐信号 → 待办真的被改）。

**真机发现**：Fedora 44 / GNOME Shell 50.5 下 `NotificationClosed` 在 `CloseNotification` 与到期时**都不发**⇒ `closed` 计数可能恒 0（只影响账）；`ActionInvoked` 必须真人点 ⇒ 无头验证走 `act`（同一套代码路径）。

**部署**：`packaging/info-hub-notify.service`（systemd user 单元，**没有 enable**）：`notify.enabled` 仍为 false，等 `AGENTS.md` §一 例外条文逐字确认后才开；开了之后这个单元负责收按键。

---

## 25. M6 实施记录：把它变成「真在跑的系统」（2026-10-15）

**动机**：M0–M5 全部靠人手动 `./bin/hub-*` 跑 —— 文档里的系统不算系统。

**做了什么**（提交见 git log）：
- 新增 6 个 systemd user 单元模板 + 1 个 UI 常驻单元（`packaging/info-hub-{collect,derive,report,week,prune}.{service,timer}`、`info-hub-ui.service`、`info-hub-notify.service`），全部 `oneshot` + `Environment=HUB_CONFIG=` 指向本仓库配置（`Documentation=` 只能用 ASCII：中文路径会被 systemd 拒，第一次安装就撞上了）。
- 新增 `bin/hub-install`：默认装 5 定时器 + UI；`--with-notify` 才装通知单元；**幂等**（单元内容没变不覆盖、不重复 enable）；`--dry-run` 只打印。
- 装了通知单元也**不会**弹：`notify.enabled=false`（§13 未落 `AGENTS.md` 前不开）。

**实测证据**（2026-10-15 18:2x）：

| 结论 | 命令 | 结果 |
|---|---|---|
| 5 个定时器真在排 | `systemctl --user list-timers info-hub-*` | collect `*:0/5`、derive `*:1/15`、report 23:50、week 周日 23:55、prune 周日 23:40，NEXT 都非空 |
| 定时器真触发过 | `journalctl --user -u info-hub-collect.service` | 到点 `Starting …` → 退出 0 |
| 失败隔离 | `hub-collect --once`（剪贴板此刻卡死） | 整轮**退出 0**，只有 `[clipboard] FAILED`，其余源照常入库 |
| 重启后自恢复 | `systemctl --user is-enabled` | 单元在 `timers.target.wants` / `default.target.wants` |
| UI 常驻 | `curl 127.0.0.1:8799/api/state` | 200 |

**跑起来才暴露的真问题（已修）**：
1. **剪贴板卡死会拖垮每轮采集**：本机 `wl-paste` 与 `wl-copy` 此刻都**永久挂住**（连 `wl-copy` 也超时 ⇒ 不是采集器写错，是桌面剪贴板数据源真的坏了）。5 分钟一轮 × 每次白等 `timeout_s` = 定时任务永远在等超时。修法：`collect.clipboard.backoff_after/backoff_s`——连续失败到阈值进冷却，冷却期内直接返回空（`tests/verify_m0.sh` T14 断言第 3 轮 < 250ms）。
2. **中文路径不能当 `Documentation=`**：systemd 直接 `Invalid argument`，安装器第一版就失败 ⇒ 单元里只放 ASCII 路径，注释写中文。
3. **oneshot + 定时器的退出码纪律**：`Type=oneshot` 下非 0 退出会让 `systemctl start` 失败并写进 journal —— 采集器单源失败**必须**不冒泡成整轮失败（否则定时器每轮都记一次 failure）。

**仍未做**（等用户）：`notify.enabled` 仍 false；M5 取数口径（①已登录 Edge CDP / ②手动导出）；`PROJECT.md` §13 例外条文逐字确认。

---

## 26. M2c 实施记录（2026-10-15）：一键「忽略此类」

**动机**：§13 例外条文草案里那条「一键忽略此类」原本只是措辞 —— 按钮发得出去，但按下只能改这一条待办，
下一轮同类待办照样弹。B 形态要可收敛，必须能**压住一整类**。

**做了什么**
- 新表 `kind_suppressions`（类别的抑制期）+ `notify.suppress_days`（默认 7）。
- 门 6（选候选时跳过被压住的类别）+ 动作键 `ignore-kind`/`忽略此类`。
- CLI `suppress` / `unsuppress` / `suppressions [--all]`；`status` 带上抑制现状；控制台左栏列出 + 提示解除命令。
- 验收 `tests/verify_m2c.sh` **11/11**（含「不许饿死别的类别」这条关键断言）。

**实测（真机）**：`./bin/hub-notify --json suppress fs-repeat --days 1` → `until=2026-10-16T18:40:56+08:00`；
`suppressions` 列出；`unsuppress` → `removed=true`；列表回到 `[]`（跑完恢复原状，没留残留状态）。

**踩到的一个真 bug（值得记）**：`suppressions` 分支写成 `if --json: … elif not rows: … for row in rows: print(…)`
—— 那个 `for` 不在 `else` 里，于是 `--json` 时**JSON 后面又跟了一行人读表格**，stdout 不再是纯 JSON
（验收 T6 立刻红）。教训：`--json` 分支必须把**其余所有**打印都放进 `else`，不能只让部分打印受控。

**仍未做**（等用户）：`notify.enabled` 仍 false（等 §13 逐字确认）；M5 取数口径；页面级上下文是否进后续期。

## 27. M2d 实施记录（2026-10-15）：通知按钮补全（打开 / 稍后）

**动机**：§6.2 的按钮设计表里 `打开` / `稍后` 一直只写在文档里 —— 实际发出去的按钮只有 完成/忽略/忽略此类。
外加一个真缺口：**「稍后」在原设计里根本没有实现路径**（点了要么当作 ignore，要么无声退出）。

**做了什么**
- 动作键 → **稳定键**映射（`notify.CANON_ACTION`）：D-Bus 回传的是显示标签（可能是中文），台账与 JSON 一律落 `open`/`snooze`/`acted`/`ignore`/`ignore-kind`，
  不再把「打开」这种标签写进库（否则查询要同时兼容中英文两套键）。
- 「打开」：`notify.open_command`（argv 模板，占位符 `{url}`/`{kind}`/`{todo}`）+ `notify.open_url`（默认指向控制台 `http://127.0.0.1:8799/`）；
  只留痕、**不改待办状态**；命令起不来只记 `open_error`（fail-soft，退出码仍 0）。
- 「稍后」：`notify.snooze_minutes`（默认 60）⇒ `store.set_todo_snooze()` 写 `todos.snooze_until` 并**清空 `notified_at`**；
  状态仍是 `open`（人没说做完）。`dispatch` 新增**门 7**：`snooze_until > now` 的候选跳过，全都被推迟时 `reason=snoozed`（看得见原因，不是灰头土脸的 no-pending-todo）。
- `listen` 计数器加 `opened` / `snoozed`；`status` 输出加 `actions` / `snooze_minutes`；`notify.actions` 可改按钮集（默认 `[打开, 稍后, 忽略此类]`）。

**证据**
- `bash tests/verify_m2d.sh` → **11 passed, 0 failed**；全量回归 **11 套 130 项全绿**
  （m0 14 / m1 11 / m2 13 / m2b 16 / m2c 11 / m3 16 / m4a 8 / m4b 9 / week 9 / m5 12 / m2d 11）。
- 真机（真 D-Bus）：`Env().capabilities()` = `['actions','body','body-markup','icon-static','persistence','sound']`；
  `send(...)` 带三个新标签 → `(uint32 98,)`，`parse_dbus_id` = 98（GNOME 接受中文动作标签）。
- 顺带修的回归：`tests/verify_m2b.sh` T1c 原来断言默认按钮是 `acted/完成`（M2d 改了默认集）⇒ 改成断言默认三个标签。
- 验收里抓到的真 bug：`notify.act` 原样把标签写进 `notifications.action`（应为稳定键）—— 已在 `CANON_ACTION` 修掉。

**仍未做**（等用户）：`notify.enabled` 仍 false（§13 例外条文未逐字确认）；M5 取数口径；页面级上下文是否进后续期。

## 28. M3b 实施记录（2026-10-15）：清单也能手改，且每次改动留痕

**动机**：用户 m01271 的原话是「**自动拉一个代办清单，然后做一个 ui 让我可以手动修改**（报表和系统数据分离，
报表我能手动改，报表每次改动有记录）」。M3 把**报表**那一半做成了"人拥有 + 每次改动留痕"，
但清单那一半还是只读的：控制台只能看，要改得去敲 `hub-store todo-set`。
⇒ 这一期把清单补成同样的形态，并把两本账分开放。

**做了什么**
- 新表 `todo_revisions`：`{id, todo_id, at, author, field, before, after, reason}`，`field ∈ status|snooze_until`。
  写入点只有两个：`store.set_todo_status()` 与 `store.set_todo_snooze()`；**值没变不写**（规则每次重跑不会刷"open→open"）。
- `author` 区分 `human`（控制台 / CLI / 通知按钮）与 `hub`（规则引擎）——"谁改的"必须能查。
- 控制台新路由：`POST /api/todo`（`{id, status?}` 或 `{id, snooze_minutes?, reason?}`）、`GET /api/todo/revisions?id=`；
  页面上的待办表加了三个按钮（完成 / 忽略 / 稍后 1 小时），下面直接列出最近的改动记录。
- CLI 对称入口：`hub-store todo-revisions [<id>] [--limit N]`；`todo-set` 的留痕 `reason=cli`。
- 失败语义写死：非法状态 / 越界 `snooze_minutes` ⇒ 400，待办不存在 ⇒ 404，Host/Origin 不合法 ⇒ 403。

**证据**
- `bash tests/verify_m3b.sh` → **9 passed, 0 failed**；全量回归 **12 套 139 项全绿**
  （m0 14 / m1 11 / m2 13 / m2b 16 / m2c 11 / m3 16 / **m3b 9** / m4a 8 / m4b 9 / week 9 / m5 12 / m2d 11）。
- 关键断言：T4「跑完规则仍是 `done` 且记录数不变」、T9「清单账本 3 条 / 报表账本 0 条」——两件事分开验，不互相顶替。

**仍未做**（等用户）：`notify.enabled` 仍 false（§13 例外条文待逐字确认）；M5 取数口径；页面级上下文是否进后续期。

## 29. M3c 实施记录（2026-10-15）：证据链 + 门 8「无依据不发」

**动机**：§13 草案③写的是「每条通知必须附依据，无依据不发」。到 M3b 为止，只有**通知正文**里带了「依据 N 条事件」，
但"依据"始终是 `todos.source_event_ids` 里的一串数字 —— **没有人能把它摊开看**，也没人校验这些 id 是否真存在。
换句话说：**"有依据"当时只是一句自述，不是可核对的事实。** 这一期把它变成事实。

**做了什么**
- `store.todo_evidence(todo_id, limit)`（新）：按 `source_event_ids` 逐条回查 `events`，
  查不到就回 `{id, missing: true}` —— **空洞如实暴露，不静默丢弃**（`prune` 清掉老事件后必然出现这种情况）。
- `store.todo_detail(todo_id, ...)`（新）：`{todo, evidence, revisions}` 三件一起给；id 不存在 ⇒ `None`。
- 三个出口共用这一个视图：控制台待办表的**「依据」按钮** ⇒ `GET /api/todo/detail?id=`（新增路由，纯只读）、
  CLI `hub-store todo <id> [--json]`（新子命令）、通知正文原有的「依据 N 条事件」。
- **门 8**（`notify.dispatch`）：候选 `source_event_ids` 为空 ⇒ `reason=no-evidence`，一条不发。
  它在候选循环里是 `continue` 而不是 `break` ⇒ **无依据的那条不许饿死别的候选**（与 M2c 门 6 同一条纪律）。
  返回结构新增 `evidence` = 本次挑中待办的依据条数。
- 门编号归位：M2c/M2d 的抑制与推迟是门 **6 / 7**（都在候选循环里判），新门因此是门 **8** ——
  代码注释里原先临时写的「6.5」已改成 8，与 INTERFACE.md §4.6 表格一致。

**两个真 bug（都是验收脚本自己抓出来的，值得记）**
1. **假证据 id**：M2 系列种子 `m2_seed.py` 直接手写 `source_event_ids=[1,2,3]`，而库里只有一个 window 事件 ⇒
   拿它验"证据能摊开"必然 → `missing`。**教训：验收夹具必须走被测的那条产出路径**（真事件 → 规则引擎 → 待办），
   自己手写的字段值会把验收变成自证。已加 `tests/m3c_seed.py`（真事件：3 次同文件改动 + 3 条同域剪贴板链接 + 1 条界面事件）。
2. **拿解析过的字段又解析一次**：`store.todos()` 已经把 `source_event_ids` `json.loads` 成 list，
   验收里再 `json.loads(r['source_event_ids'])` ⇒ `TypeError`。**教训：`todos()` / `pending_notify_todos()` 返回的是解析后的对象，
   原始的 TEXT 列只该在直查 SQLite 时出现。**

**证据**
- `bash tests/verify_m3c.sh` → **9 passed, 0 failed，退出码 0**（T1 证据逐条可查 / T2 UI 摊开 / T3 未知 id 双出口 /
  T4 CLI 打印 / T5 `missing` 如实暴露 / T6 证据链与清单留痕合流 / T7 只读 / T8 门 8 / T9 不饿死别的候选）。
- 全量回归 **13 套 148 项全绿**（m0 14 / m1 11 / m2 13 / m2b 16 / m2c 11 / m2d 11 / m3 16 / m3b 9 / **m3c 9** / m4a 8 / m4b 9 / m5 12 / week 9）。

**仍未做**（等用户）：`notify.enabled` 仍 false（§13 例外条文待逐字确认，**门 8 的代码已就位但整条 B 形态仍关着**）；M5 取数口径。

## 30. 收尾记录（2026-10-15）：README + 交付报告 + 全部一次性对账

**这一节干的事**：M0–M6 逐期都有验收，但**没有一份"从外面看这个项目"的入口文档**，
也没有一份"交付了什么、凭什么、还差什么"的交付报告 —— 接手者（人或 agent）只能从 28 节实施记录里自己拼。
补两份文档 + 把文档里的悬空指针一次对齐。

**新增**
- [README.md](README.md)：定位一句话、**现在跑着什么**（真机数据原文）、装/用/验收/落位/隐私、默认关着的四项及原因。
- [DELIVERY.md](DELIVERY.md)：交付物清单 + 机械证据（13 套 148 项 + 真机 14093 事件/5 timer/UI 200）+
  逐期落点 + **未决四项各五件套**（待定的是什么/为什么重要/选项/权衡/推荐）+ **已知局限 6 条**。

**对账（把"悬空指针"清零）**
- 本文档抬头状态从「设计阶段，未写代码」改为「M0–M6 已实施，系统在跑」并加文档索引 ——
  这句从 M0 完成那天起就是错的，一直没人回头改（**教训：状态行属于最容易烂的地方**）。
- §9 的 **M4 父行**补 ✅（M4a/b/c 三个子行早就 ✅，父行漏了）。
- INTERFACE §5.8 / §4.6 门 8 / §2.2 / §8（M3c 那一轮补的）与 README/DELIVERY 的交叉引用逐条对上。

**如实记下的三条局限**（详见 DELIVERY §4，这里只记结论）
1. **走门的 B 形态闭环在真机一次都没跑过**（`notifications` 表 0 行）—— 门 1–8 的"能工作"目前只有 stub 证据。
   **但通知通道本身在真机弹过**（§18 真后端自检 `(uint32 70,)`、更早手工探针 `(uint32 69,)`，用户亲眼看到）——
   那是绕过门的探针，不进台账、不算闭环；随时可复现的自证命令是 `bash bin/hub-notify test`（见 §31）。
2. `bilibili` 采集器未被真实账号数据跑过（M5 只验了合成源）。
3. **window 采集很稀疏**（全库 4 条，隔夜 11 小时只多 1 条）：只在界面集合真的变了才落库 ——
   设计如此（省噪音），但会让 `notify.require_ui_event=true` 这条门把通知拖得很晚才评估。
   **开通知前必须先用 `backend=stdout` 实测一轮节奏**，必要时把口径放宽成"有新的**事件**即可"。

**证据**
- 全量回归仍 **13 套 148 项全绿**；`bash bin/hub-install --dry-run` 退出 0 且列出 6 项 enable（第 7 项 notify 显式跳过）；
  `bash bin/hub-store stats` 事件数 14093（首 2026-10-12T21:34:09+08:00）；`curl / 127.0.0.1:8799` → 200。
- **隔夜复查（2026-10-16 08:45）**：事件 14093 → **14451**（末 08:41:40，采集器整夜没停）；
  `info-hub-report.timer` LAST=**07:01:05** —— **报表定时器自己跑过一轮**，`hub-report.md` 1748→3279 字节；
  夜里只新增 1 条事件（不自造活动）；报表里如实记着 `clipboard` 的 `wl-paste 退出 1: This seat has no keyboard`
  （夜里无输入席位，fail-soft 不中断整轮）。**M6 的"在跑着"由这一组坐实，而不是由"装上了"坐实。**
- 提交：`5a8fbbe` M3c（代码 + 验收 + 文档同一次提交，不留"代码已提交、文档下轮再说"）；
  `c3467db` 收尾文档。

---

## 31. 通知通道自检补齐 + 一处错述更正（2026-10-16）

**起因**：交付报告里写了「B 形态（系统通知）从未在真机发过一条」，用户当场回「我收到过系统通知啊」。
核对下来是**我错了**：

- `~/.config/systemd/user/` 里确实没有 `info-hub-notify.service`，`journalctl --user -u` 也是空，
  全机只有一个库 `~/.dsh/info-hub/hub.db`，`notifications` 表 **0 行** —— 这三条都真。
- **但**本文件 §18 白纸黑字记着真机自检：`Env().capabilities()` →
  `['actions','body','body-markup','icon-static','persistence','sound']`、`send(...)` → `(uint32 70,)`「屏幕弹出一条带按钮的通知」；
  更早的手工探针还留过 `(uint32 69,)`（§6 与附录）。**用户看到的正是这两条。**

⇒ 教训（比这次改动更值钱）：**"台账里没有"只能证明"没走台账那条路"，
不能证明"这件事没发生过"。** 我把一个表的行数当成了"通道从没通过"的证据，
把「闭环没跑过」说成了「通知没弹过」——两句话差着一个已被用户亲眼确认的事实。

**两处悬空指针**：`PROJECT.md:33`（G3 目标验收）与 §15 的 M2 行都写着 `hub-notify --test`，
但这个子命令**从来不存在**（`hub-notify` 只有 dispatch/status/effectiveness/act/suppress/unsuppress/suppressions/listen）。
按 AGENTS.md §五「不留悬空」，这里选择**实现它**而不是删掉判据 —— §15 是"验收判据必须机械可验"，
把判据删掉等于承认 M2 那条从没验过。

**本轮落地**
1. `notify.py`：`PROBE_BODY` + `probe(config, store, dry_run=False) -> dict`。**刻意绕开门 1–8**
   （自检要回答"这台机器弹不弹得出来"，答案不能依赖 `enabled`，也不能依赖用户恰好切了界面）；
   **不碰 `todos`**（不改状态、不写 `notified_at`）；台账记 `kind='probe'`、`todo_id=NULL`
   ⇒ `effectiveness` 归 `unlinked`、**不进样本**，不污染降频；真弹失败 ⇒ **不记台账**、退出码仍 0。
2. `cli.py`：`hub-notify test [--dry-run]`，人读输出摊开"弹没弹 / 系统通知 id / 台账 #"。
3. `tests/verify_m2.sh`：**T13**（`enabled=false` 也真弹 → stub 1 行 + 台账 `probe/1/todo_id 空/待办 0 条`）、
   **T14**（`--dry-run` → 没弹、台账 0 行）⇒ 该套 **15 passed, 0 failed**（原 13）。
4. 契约：INTERFACE §2.4 表加 `test [--dry-run]` 行 + §4.6 新增正文小节「自检探针」；
   §15 与 G3 的 `--test` 改成 `test` 并注明台账记 `kind=probe`、不占 `notified_at`。
5. 表述更正：README / DELIVERY §1、§3-1、§4 / 本文件抬头与 §30 局限 1 ——
   一律改成「**通道真机弹过（探针 id 69/70，用户亲眼看到）；走门的 B 形态闭环从未在真机发生，台账 0 行**」。

---

## 32. M4d 实施记录（2026-10-16）：每周归纳（本地确定性后端）

**动机**：M4b 已经把归纳层做出来了，但它当时是「关着的 + 只有一个 LLM 后端例子」——
`digest.enabled=false`、`window_days=1`，而例子指向的 `bin/hub-digest-dsh` **要求那个 DSH 会话正活着**
（`bridge drop` 之后得有人消费才有回复）。无人值守的每周定时器不能建立在这个前提上：
本机没有本地 LLM（`ollama`/`llama-cli` 都不在），`dsh` CLI 也没有「建会话」子命令。

**决定**：默认后端 = **本地确定性后端**（离线、零 token、同输入必同输出）。
理由：归纳层要的东西是"这周发生了什么、有什么依据"，本机库里全都有；把它算出来不需要模型。
LLM 路径**不删**，仍是一个可换的 `digest.command`（想换回模型，把命令改成 `bin/hub-digest-dsh --session <id>` 即可）。

**落地**
1. 新 `src/infohub/localdigest.py`：`parse_prompt_ids(prompt)` 从上游提示词读出**允许引用的事件 id 集合**；
   `build(config, store, window_days, prompt)` 产出 `{"items":[{"text","evidence"}]}`；待办条目优先
   （证据取 `store.todo_evidence`，跳过 `missing`，最多 `digest.local.evidence_per_item` 条），
   再按 source 给「最近 N 条事件里 X 占 M 条，最新 …」的计数条目；**没有依据的条目一律不输出**；条数 ≤ `digest.max_items`。
2. 新 `src/infohub/entry_digest_local.py` + `bin/hub-digest-local`：提示词走 stdin（读掉，只用它的 id 集合），stdout 只出 JSON。
3. `packaging/info-hub-digest.{service,timer}`：`OnCalendar=Sun *-*-* 23:30:00`（早于周报 23:55，周报才能引用它）；
   `bin/hub-install` 的 `units=(...)` 纳入 `info-hub-digest.timer` ⇒ 默认安装面从 6 个变 7 个。
4. `hub.config.yaml` 的 `digest:` 段：`enabled: true` / `window_days: 7` /
   `command: [<repo>/bin/hub-digest-local, --window-days, 7]` / 新键 `todo_evidence_events: 40`、`local.evidence_per_item: 3`。
5. `tests/verify_m4d.sh`（12 项）+ `tests/verify_m4b.sh` 的 T1 解耦（改为显式钉 `digest.enabled=False`，
   测"关掉的语义"而不是"默认值恰好是 false"）。

**真机落地（2026-10-16 09:33）**
```
$ bin/hub-digest run --window-days 7
digest #2 条目 6 丢弃无依据 0 耗时 48ms（后端 …/bin/hub-digest-local --window-days）
  · 待办：「info-hub/hub.config.yaml」24 小时内改了 7 次  evidence=[7752, 11065, 11285]
  · 待办：工具「tencent-connect/dsh-qqbot」在知识库里查不到  evidence=[15387]
$ bin/hub-digest run --window-days 7      # 第二次
digest skip reason=reused（同一份输入已经归纳过，不重复花 token）
```
报表 `digest` 区块与周报（`hub-week-2026-W42.md`：`归纳次数 2 / 归纳条目 11`）都带上真实 `evidence` 指针 ⇒
"每周归纳"这条链路（库里 ↔ 报表 ↔ 周报）在真机上闭合了。

**关键设计：本地后端不许自己另取一份数据。**
第一版实现是"后端自己扫库、自己挑证据"，看起来更独立，实际上**会被 M4b 的审计整条丢掉**——
`parse_output` 只认提示词里出现过的事件 id，而自己扫出来的 id 未必在提示词里（提示词只带最新 `max_events` 条）。
所以改成**提示词驱动**：提示词就是唯一允许集合，`evidence` 必须是它的子集 ⇒ 与审计同一把尺子。
反过来，`build_input` 也得配合：事件流水只取最新 `max_events` 条，而待办依据可能更老
（`fs-repeat` 的依据是 24 小时窗口里那几条），所以**额外把 open 待办的证据事件带进提示词**（上限 40 条）。

**教训**：跨期功能组合时，两个各自"正确"的模块（一个只取最新 N 条、一个只引用提示词里的 id）
会互相拆台；判据是**端到端**那条（"证据到底进没进最终产物"），不是各自的单元测试。

---

## 33. M5b 实施记录（2026-10-16）：浏览器历史当来源

**动机（用户 m00480 的原话：「2直接我看我账号的历史记录」）**：M5 的 bilibili 源当时没有真数据
（`collect.bilibili.enabled=false`，取数口径待定）。用户指定按**他自己账号的历史记录**取数。

**先测再定口径（这一步决定了整个 M5b 的形状）**：拿真机 Edge 的 `History`
（`~/.config/microsoft-edge/Profile 1/History`，只读打开）实测：
- bilibili 视频页 3846 行；最近 14 天去重 **1072** 条真实标题 ⇒ 喂给 `bili.extract_tools` 提出 **0 个工具**
  （标题里 github/gitee/仓库 = 0）。⇒ **只按 bilibili 历史接，`bili-unintegrated` 会恒为 0 产出**，
  等于接了个永远不响的源。
- 真信号在**仓库页**：历史里 github/gitlab URL 242 条 ⇒ 去重 `owner/repo`：全期 **73** 个、30 天 68、14 天 40。
- 噪声也在那儿：`github.com/settings/*`、`login/*`、`users/*`、`topics/*` 会被 `GITHUB_RE` 误认成 `owner/repo`
  （旧 `BAD_OWNERS` 下提出 84 个、含 11+ 假货）。

⇒ 口径定为「**浏览器历史里的仓库页 + 视频页**」，不只 bilibili。这条要如实地讲给用户听：
他点的 bilibili 那条路本身产不出东西，值钱的是同一份历史里的仓库页。

**落地**
1. 新 `src/infohub/browserhist.py`：`db_path` / `read_rows` / `ts_iso` / `repo_item` / `video_item` / `collect_items`。
   - **只读证据**：先把 `History`（含 `-wal`/`-shm`）拷进临时目录，再 `mode=ro` 打开，读完删副本 ⇒
     不锁浏览器的库、不在档案目录留东西（`tests/verify_m5b.sh` T7 比对取数前后 `History` 的 sha256 不变）。
   - `max_items` 是**每类**上限（否则视频页会把仓库页挤出窗口）；同一 id 只留最近一次访问；`since_days` 之前不看。
   - `privacy="sensitive"`（浏览器历史比其他 source 更私密，但照样走既有脱敏管线）。
2. 新 `src/infohub/collectors/browser_history.py`，注册进 `registry()`；游标 key `browser-history:items`，
   失败进 `collector_state['run:browser-history']`（整轮退出码仍 0）。
3. `hub.config.yaml` 的 `collect.browser-history`：`enabled: true` / `browser: edge` / `profile: "Profile 1"` /
   `since_days: 14` / `max_items: 200` / `kinds: [repo, video]`。
4. 规则 `bili-unintegrated` 改**双源**（`bilibili` + `browser-history`），`why` 点明来源；
   `bili.BAD_OWNERS` 扩了一组站点路径段（`settings`/`login`/`users`/`topics`/…）。
5. `tests/verify_m5b.sh`（17 项）。

**真机落地（2026-10-16 09:33）**
- `collect.timer` 自己就把新源收了进去：`browser-history` 事件 **237** 条（repo 36 + video 201，
  其中 201 = 首次 200 + 后来历史里又多了一条视频 ⇒ 每类上限确实在生效），全部 `privacy=sensitive`。
  手动 `bin/hub-collect --once --source browser-history` ⇒ `emitted=0`（游标幂等）。
- `bin/hub-rules derive`：候选 **48** 条（随后我自己还在改代码，涨到 50），其中 `bili-unintegrated` 出来的仓库条目覆盖了
  `TIGillusion/Qbot`、`tencent-connect/dsh-qqbot`、`deusyu/harness-engineering` 等他真看过的仓库。
- 新源接上后先出了一条假待办（`notebooks/recipes`）：某仓库页标题是
  "A collection of notebooks/recipes showcasing…"，被 `REPO_TOKEN_RE` 当成仓库名。已在 `extract_tools` 修掉，
  那条假待办用 `bin/hub-store todo-set … dropped` 手工标掉（`author=human`，留痕，不删行）。
- 全量回归：15 套 179 项全绿；`bin/hub-install --dry-run` rc=0 且列出 7 个单元（6 定时器 + UI）。

**教训一（真数据才照得出来的 bug）：URL 是仓库页时，标题里的斜杠词不是仓库。**
`extract_tools` 原本把 `title/url/desc` 拼成一段扫 `owner/repo`。bilibili 收藏夹那路没事（URL 是视频页，
工具名只在标题里），但浏览器历史那路 URL 本身就是仓库页，标题里却满是斜杠词 ⇒ 真机立刻多出一条
`notebooks/recipes` 的假待办。修法是**分路**：`GITHUB_RE` 命中 URL 时只认 URL，否则才扫标题/正文
（`tests/verify_m5b.sh` T8 钉住）。**教训**：合成夹具里写不出"标题恰好长得像仓库名"，
这类 bug 只有拿真数据跑一遍才会露头。
- 第二类假货（2026-10-16 12:0x，看门狗自己照出来的）：真机历史里有一条失效的重置链接
  `https://github.com/password_reset/<200 多位 base32>`，`password_reset` 出现在 owner 位置 ⇒ 被当成仓库，
  产出 2 条假待办。修法两条：①`BAD_OWNERS` 补 `password_reset`/`password`/`reset`；
  ②按 GitHub 真实长度上限挡（`MAX_OWNER_LEN = 39`、`MAX_REPO_LEN = 100`）—— 长度这条更通用，
  不依赖枚举站点路径。验收加 T8b（m5b 18/18）；真库那 2 条假待办用 `hub-store todo-set <id> dropped` 标掉（留痕不删行）。


**教训二（三个夹具坑，都是"把夹具写错当成功能错"）**
- `hub-rules --json derive` 的 `items[]` 只有 `{key, rule, title}`，**没有 `why`** ⇒ 断言待办正文要去库里取。
- `store.todos()` 的**第一个位置参数是 `status`**（`todos(10)` = `status=10`）⇒ 必须写 `todos(limit=10)`。
- 采集器失败记录的游标 key 是 `run:<name>`（`cli.run_round` 的 except 分支），不是 `<name>:items`。
  这三条已写进 INTERFACE §8 末尾的「写验收脚本时的三条已知坑」，避免下一期再踩。

---

- M7：`tests/verify_m7.sh` 全过、退出码 0（活跃取 1 帧 + `shot.saved`/`sensitive` + 图落盘 + 事件 `sha256` == 文件哈希；
  间隔内不再取；不活跃（空闲 900s）不取帧不发事件且状态记 `idle`；探针失败 **不取帧** 且记 `run:screenshot`、退出码 0；
  `require_active=false` 探针不参与；`enabled=false` 一张不取；`max_width`/`jpeg_quality` 透传；`prune` 删老图留新图、
  事件元数据不动；`status` 如实报；`grab` 立刻取一帧；空命令退出 1 不写文件；过期清理单元含 `hub-screencast prune`）。
- m2 追加 T15：`Env.idle_s()` 解析真机格式 `(uint64 29866,)` ⇒ 29.866 秒（修掉"位宽被当数字"的 bug）。

## 34. M2f 实施记录（2026-10-16）：通知的**触发面**

**动机（用户 2026-10-16 定调）**：「弹消息是弱提醒，是**例外项目**」⇒ 例外可以开，但"弱"要落在机制上。
复查时发现一个真缺口：门 1–8 回答了"这条能不能发"，**没有任何东西回答"什么时候去评估"** ——
`hub-notify dispatch` 只能手动跑，装上的 `info-hub-notify.service` 只是动作回调（`listen`），
所以就算把 `notify.enabled` 改成 `true`，真机上也**永远不会**弹：触发面根本不存在。

**一度想用 timer**（每 N 分钟 `hub-notify dispatch`）—— 否掉了：条文①写的是
「只在检测到界面活动时评估……绝不按时间表推送」。用 timer 的话，"评估"本身就按时间表发生，
条文①②就只剩门在挡、触发点失守（等于用写法绕开用户定的规矩）。**换等价工具或写法也必须同样被拦住**。

**做法**：把触发点钉在"采集到新界面事件"上 —— `bin/hub-collect --once --notify`，
`run_round` 统计这一轮入库批次里的 `window` 事件数（`summary["window_new"]`），
`cli._notify_after_ui()` 只在 `window_new > 0` 时调一次 `notify.dispatch`。
定时的只是**采集**（M0 既有行为，每 5 分钟），**推送**永远挂在"有新界面事件"上。
`packaging/info-hub-collect.service` 的 `ExecStart` 带上 `--notify`（用户态单元，无窗口）。

**关键判据（都可机械验，`tests/verify_m2f.sh` 9/9）**
- 不带 `--notify` ⇒ 完全不评估（显式 opt-in，采集默认语义不变）。
- 这一轮收到 `window` 事件 ⇒ `sent=1` 且退出码仍 0（**通知出问题不许拖累采集**）。
- 只收到 `fs` 事件 ⇒ `reason=no-new-ui-event`，不弹。
- 库里**旧**的 `window` 事件不算数 —— 否则等于把 timer 换个写法。
- 池子弹完收敛到 0；`notify.enabled=false` ⇒ 触发面照走、门 1 挡住（`reason=disabled`）。
- `--notify` 真装机上：`ExecStart` 带它、`--help` 列得出它。

**真机只读预览（dry-run，不动库）**：临时配置开 `enabled=true` + `backend=command` 指向假通知器 ⇒
`would_send=true`，正文 `「info-hub/tests/verify_m5b.sh」24 小时内改了 3 次（依据 3 条事件）`；
跑完 `notifications` 仍 1 行、`notified_at` 非空仍 0 条、`notify:ui` 游标仍为空。
**连跑两次都判 `idle`** —— 但**这次判定当时是错的**：显示的空闲 642 秒是解析 bug 的产物
（`(uint64 N,)` 里 `64` 被当成高位吃进毫秒数；反推真实空闲只有 2 秒，**人其实在**）。这条在 M7 联调时
照出来并修掉（§35 的 bug 2）；修好后重跑同一条预览，真机空闲 433 秒 ⇒ 这次"人不在"才是真值。

**教训**：「门都实现了」不等于「这条链路能跑」—— 门管的是"能不能发"，
**触发面管的是"什么时候想起来去评估"**。缺后者时，前者的全部验收都是绿的自证。

## 35. M7 实施记录（2026-10-16）：静默截屏（**活跃才取，不活跃完全不反应**）

**用户定调（逐字）**：m01149「应该是监测到键鼠活跃的时候每段时间解牌，键鼠不活跃不应该有反应」→
m01154「我说的是截屏，说错了」→ m01179「不能走这个截屏，有动画和声音」。
两问答复：形态 = **ScreenCast 取帧**（键鼠活跃时取，活跃 = mutter 空闲 < 60 秒；无闪光无快门声；
要一次性点「共享屏幕」同意）；存储 = **缩放图（1280 宽 JPEG）+ 元数据，留 7 天**。

**被否掉的三条路（真机实测，决定了形态）**
- `org.gnome.Shell.Screenshot.Screenshot` ⇒ `busctl --user call` 实测 `Call failed: Access denied`（非授权调用者用不了）。
- `org.freedesktop.portal.Screenshot` ⇒ 调用返回 request handle，但 `gdbus monitor` **抓不到 Response、不产出 PNG**，
  `pyatspi` 也看不到待确认对话框 ⇒ 无 app-id 的调用者走不通；而且这条路正是"闪光动画 + 快门声"。
- 唯一已装的截图 CLI 是 ImageMagick `import`（Wayland 下不可靠）；没有 grim/gnome-screenshot/spectacle/scrot/flameshot。
- `org.freedesktop.portal.ScreenCast`（v5，`AvailableSourceTypes=7`）⇒ **可用**，取帧无声无动画。后端不支持带音频。

**实现**（代码在 `src/infohub/screencast.py` + `src/infohub/collectors/screenshot.py`，契约见 INTERFACE.md §2.8、§4.13）
- `bin/hub-screencast consent|status|grab|prune`；同意令牌存 `state/screencast.json`（0600，原子写）。
- 采集器四道门：**间隔**（`interval_s`，游标 `epoch`）→ **活跃**（复用 `notify.Env.idle_s()`，不另造探针；
  `idle > idle_max_s` ⇒ 不取帧、不推进 `epoch`、状态记 `last_skip=idle`）→ **打字保护**（2026-11-13 加，见下）
  → 取帧落盘
  （`<dir>/<日期>/<HHMMSS>.jpg`，同名加序号）→ 发 `screenshot/shot.saved`（`privacy=sensitive`，`sha256` = 文件内容哈希）。
- 探到不活跃**不取帧**；探针失败**同样不取帧**（fail-closed：探不到"人在"就当不在）。

**2026-11-13 追加：第四条门「不在打字时取」**（用户 m00001「现在的屏幕监测会打断我的输入法，修复一下」）
- 根因：本机候选窗由 GNOME Shell 扩展 `kimpanel@kde.org` 渲染（`GetConnectionUnixProcessID :1.19` = gnome-shell = 6193），
  而活跃门 `idle < idle_max_s` 恰好把取帧对在**正在打字**的时刻。`dbus-monitor` 实测一次取帧全程只有
  ScreenCast / Mutter.ScreenCast 调用（无对话框、无 `_NET_ACTIVE_WINDOW` 变化）⇒ 不是抢焦点；
  是 0.4 秒的合成器/portal/JPEG 编码 + pyatspi 全窗口遍历扰动到了候选窗。
- 改法：空闲 < `idle_min_s`（默认 3 秒）视为「正在敲键」⇒ 先等一次自然停顿（每 ≤0.5 秒重问，最多 `pause_wait_s`=20 秒），
  等到再取；等不到 ⇒ 这轮不取、`last_skip=typing`、不推进 `epoch`。这道门探针失败**放行**（fail-open），
  与活跃门的 fail-closed 相反 —— 探不到空闲时长不等于「在打字」，不能因此停采。
- 留存：`hub-screencast prune` 挂进 `info-hub-prune.timer` 的**第二条 `ExecStart`**（删图、不删事件元数据）。

**真机照出的两个 bug（都在本轮修掉）**
1. **portal 签名不是照着旧文档写的那样**：`gdbus introspect` 实测本机 v5 是
   `CreateSession(a{sv})`（**没有 `parent_window`**）、`SelectSources(o,a{sv})`、`Start(o,s,a{sv})`。
   按旧文档写 `(sa{sv})` ⇒ `GDBus.Error.InvalidArgs: 消息的类型"(sa{sv})"，与预期的类型"(a{sv})"不匹配`。
2. **`Env.idle_s()` 解析错**：真机回的是 `(uint64 29866,)`（**uint64**，毫秒），老实现把"所有数字字符"连起来，
   把位宽 `64` 也吃进去 ⇒ 6429.866 秒 ⇒ **永远判"人不在"**。这条从 M2 起就错（门 3），只是通知一直关着没暴露；
   M7 的活跃门直接依赖它，才在联调时照出来。修法：`IDLE_RE` 只取括号里的整数，回退取最后一个数字串；
   回归 `tests/verify_m2.sh` **T15**（`(uint64 29866,)` ⇒ 29.866s，< 60s ⇒ 门 3 放行）。

**真机跑通后追加的两个 bug（2026-10-16 11:42–11:49）**
3. **缩放把画面拉变形**：管道只写了 `video/x-raw,width=[1,1280]`，videoscale 便**保留源高度**——
   真机流是 **2560x1600**（portal 报的 `size` 是 1920x1200 逻辑尺寸，两者同比例），于是缩出来是
   **1280x1600**（横向压掉一半，字都立起来了）。修法：`target_size(max_width, size)` 按 portal 报的尺寸算等比目标，
   管道里**宽高都钉死** + `videoscale add-borders=true`（宁可留黑边也不拉伸）；实测 1280 ⇒ **1280x800**、640 ⇒ 640x400、
   `max_width` 大于源宽 ⇒ 不放大。回归 **T15**（尺寸口径）+ **T16**（管道串里宽高都在、`width=[1,` 不再出现）。
4. **「单次使用」的令牌在本机后端上不成立**：规范说 `restore_token` 一次一换，实测本机 GNOME portal
   复用时**每次仍回同一个令牌**（连续 4 次取帧，落盘令牌一字未改、始终可用）。实现按"不同才写回"+ `grab` 返回
   `token_from_portal` 对账 ⇒ 真换了也不会漏；无人值守路径**从不弹同意框**（没令牌就失败并记 `run:screenshot`）。

**真机证据（2026-10-16 11:49，`collect.screenshot.enabled=true` 后）**

```
事件 17670 @ 2026-10-16T11:49:21+08:00  source=screenshot kind=shot.saved privacy=sensitive
图 1280x800，421939 字节，~/.dsh/info-hub/screens/2026-10-16/114920.jpg，idle_s=5.195
文件哈希与事件里的 sha256 对得上（同一 `digest_of` 口径）；取帧全程**没有弹任何对话框**（窗口列表只有 gnome-shell + ptyxis）
第一张（11:42:15）是修变形之前的 1280x1600，留在库里当反面证据，7 天后由 prune 收走
```

**活跃门在真机上"两头都成立"（11:55 的定时器轮是免费的对照）**：

```
11:49:21 手动一轮：idle=5.195s（人在）        ⇒ 取帧，落 1280x800
11:55:06 定时的 collect 轮（无人干预）：人在同一时刻空闲 216s ≥ 60s
          ⇒ 采集器 last_ok_at=11:55:07、last_error=None、**一条新事件都没有**（不活跃就完全不取）
```

**验收**：`tests/verify_m7.sh` **24/24、退出码 0**（活跃/间隔/不活跃/**打字中不取 + 轮询到停顿仍取**/探针失败/`require_active=false`/开关/参数透传/
prune 留 7 天/status/grab/失败面/单元 + 帮助面），全程离线、**不碰真 portal、不弹任何对话框**（取帧走 `backend=command` 假命令，
活跃度走假 `gdbus`）。全量回归 17 套全绿。

**教训**：
- 真机格式按**实测**写，不按文档/直觉（`uint64` vs `uint32`、portal v5 少一个参数）。
- "把所有数字连起来"是隐患写法：任何带单位/类型名的输出都会被污染。
- 两个模块共用**同一个探针**（M7 复用通知层的 `Env.idle_s()`）让一次修复同时覆盖两处 —— 少一份实现就少一处会漂移的口径。

---

## 36. M8 实施记录（2026-10-16）：全量浏览历史 + 用途/网址分类 + 一条总结

**动机（用户 m01809 逐字）**：「只留历史记录+一条总结，你的方式可以对所有浏览历史生效，最好都做检查，
可以做简单分类，按照用途和网址」。此前 M5b 只把**仓库页/视频页**折成事件（14 天窗口、每类 200 条），
用户看到的是"挑过的历史"；这次改成**全量**：每条历史留一条记录（时间 + 网址 + 站点 + 用途 + 标题前 120 字），
分类由**用途**与**网址**两个维度机械算出，并给出一条汇总。用户三问答复（同一轮 ask_user_question）：
①每条存**标题前 120 字**；②出卡范围选 **「卡片不推送」**；③留存 **180 天**。

**做法**
- 新模块 `src/infohub/history.py`（纯函数层，不碰库/不看时钟）：`PURPOSES` 16 类 + `DOMAIN_RULES`/`PATH_RULES`
  按"顺序即优先级"匹配，认不出落 `其它`（不猜）；`site()` 处理多段后缀与 IP；`visit_id()` = `visit:<sha1[:16]>`
  （同一网址只算一条）；`summarize()` 给 `{total,by_purpose,top_sites,line}`；`render_block()` 出报表区块。
- `src/infohub/browserhist.py` 增 `history_item(row, title_limit=120)`；`kinds` 增 **`history`**，且它是**兜底类**
  ——命中它的行不再产 `repo`/`video`（同一次访问不会记两遍）。配置默认 `kinds: [history]`、`since_days: 180`、
  `max_items: 20000`、`keep_ids: 40000`、`title_chars: 120`。
- 采集器事件统一 `kind=browser.history`、payload 带 `site`/`purpose`、`privacy="sensitive"`。
- 报表新增区块 `history`（`report.blocks` 与 `report.history_days`）；新 CLI `bin/hub-history`（`summary`/`classify`）。
- **不产卡片、不推送**：分类与总结只进报表与 CLI stdout（用户答"卡片不推送"）。
- 规则侧加**噪声门**：`browser-history` 来源必须先 `bili.GITHUB_RE` 命中 **URL** 才提工具 —— 全量历史里
  视频/小说标题的 `a/b` 不能当仓库（同一个坑在 M5b 用数据照出来过，见 §33）。

**真机证据（2026-10-16 12:25–12:26）**
```
全量入库：History 9073 行 → 首次 2262 条（30 天窗口）+ 二轮 7073 条（180 天窗口）= 事件 9335 条（全部 sensitive）
         采集耗时 0.89s / 单轮；游标 keep_ids 挡住重复（再采 emitted=0）
分类覆盖：9335 条里 其它 4.5%（搜索/网盘/AI/本机等规则修好后原本的 12.4% 降到 4.5%）
         分布：视频/音频 51.9% · 小说/阅读 16.5% · 搜索 4.9% · 账号/邮箱 4.7% · AI 服务 4.6% · 社区/论坛 3.6%
              · 代码仓库 2.9% · 学习/课程 1.7% · 本机服务 1.1% · 购物/采购 1.1% · 技术文档/博客 1.1% · 其余 2.0%
一条总结（最近 1 天）：浏览 383 个页面（去重后）：视频/音频 235、学习/课程 65、招聘/求职 31、AI 服务 27
         主力站点：bilibili.com 233 · hrbust.edu.cn 34 · careersky.cn 31 · deepseek.com 18 · pintia.cn 18
报表：hub-report.md 出现 `hub:auto:begin history` 区块（一句话总结 + 用途表 + 主力站点）
```
**真机照出来的一个问题（已修）**：历史全量后 `browser-history` 单源事件从 259 涨到 9335，而
`rules.bili-unintegrated.scan_limit` 还是 2000（**每源**上限，按 id 倒序取最新 2000 条）⇒ 窗口内的老仓库页被挤出扫描面，
待办从 30 掉到 20。把 `scan_limit` 提到 20000 后恢复（28 条，差额是与 KB 对账后静默的那些）。

**验收**：`tests/verify_m8.sh` **16/16、退出码 0**（分类单元 12/12；条目字段与 120 字截断；全量入库；
同网址只留最近一次；游标幂等；`summarize` 总数/用途/站点与库一致 + 一条总结；窗口收窄；**非仓库页不产假卡**；
报表含 `history` 区块；库不存在 ⇒ 退出码 0 + `run:browser-history` 记错；`enabled=false` 不采；假 `History`
sha256 不变；CLI `classify`/`summary`）。全量回归 **18 套 228 项全绿**（新增 m8 16 项，m5b 因默认 `kinds` 变化
显式钉住 `[repo, video]` 后仍 18/18）。

**教训**
- 上游"变全量"会**冲刷**下游按条数截断的扫描窗口：改口径时要顺着数据流把每处 `limit` 都复核一遍
  （这次是 `scan_limit`），否则表现为"功能变差"而不是"报错"。
- 兜底类要显式定义语义（`history` 命中即不再产 `repo`/`video`），否则同一次访问会在库里记两遍。
- 分类是"描述层"，不是事实断言：认不出就老实地落 `其它`，不猜、不补。

---

## M11 画面语义层（2026-10-24，用户：「走一个简单 ocr，接入那个信息中枢统一管理」）

**场景**：M10 的专注度只能算「在场/切换/空档」，分不清「写代码」和「刷视频」；用户提出 OCR，并要它归到
info-hub 统一管理，不要另起一个工具。

**做法**（派生层，不是采集层）：
- 不新增取帧：只读 M7 已经在存的 JPG（`~/.dsh/info-hub/screens/<day>/<HHMMSS>.jpg`，每 5 分钟一张、只在场时拍）。
- 引擎：RapidOCR（onnxruntime，纯 CPU）。装法 `python3 -m pip install --user rapidocr-onnxruntime`。
- 判定：`vision_keywords.py` 词表，**弱词 1 分 / 强词 2 分**，最高分胜出；不足 `vision.min_hits` 判 `unknown`。
- 落库：`shot_text`（可选原文，默认不存）+ `shot_labels`（类别/分数/命中词/算法版本）；幂等键 `sha256`。
- 挂载：`hub-rules derive`（15 分钟一轮）顺带跑，`vision.enabled` 关掉即退化成 M10；失败只进摘要不阻断。
- 代价控制：`vision.threads=4`——实测环境变量与 `sched_setaffinity` 都压不住 onnxruntime，只有建会话前改
  `SessionOptions.intra_op_num_threads` 有效（否则一张 1280×800 图烧约 50 秒 CPU；改后 1.2 秒/张、约 4 核）。

**真机实测**：176 张图 / 63 MB，1280×800；其中 **169 张的 `app` 字段是 null**（Wayland 下 pyatspi 拿不到焦点），
OCR 正好补上「这是哪个应用/什么页面」。14 张标定结果：`agent` 6、`video` 3、`unknown` 5 —— 覆盖率 64%，
unknown 是诚实弃权（只命中标签栏里的「淘宝」这类弱词）。

**验收**：`tests/verify_m11.sh` **16/16、退出码 0**（八类判定含「只有弱词判 unknown」；只读未读过的画面且幂等；
默认不留原文；keep_raw + 过期清理；引擎缺失降级零写入；图丢失记 missing；derive 不因它崩；重跑逐字节相同；
`classify` 不建库；配置键齐全）。全量 **20 套全 rc=0**。

**三次标定的教训**（写下来省下一轮）：
- v1 词表混进了界面通用词（消息/回复/收藏/关注）⇒ 3 张桌面全判成 `chat`。**界面词语义是噪声，不是信号。**
- 截屏里常驻浏览器**标签栏标题**（淘宝/bilibili 每张都在）⇒ 只有弱词时不能判类别，必须留 `unknown`。
- 本机主界面是 DeepSeek GUI，长得像聊天窗口 ⇒ 单列 `agent` 类，否则「干活」会被算成「聊天」。

---

## M12 画面粗判：工作 / 玩（2026-10-24，用户：「0.6b 给出画面分析，只要归类到玩和工作」）

**要什么**：一张图只回答一件事 —— 在工作还是在玩。类别以后再加（学习/会议…），所以类别表必须是数据。

**怎么做**：`src/infohub/vlm.py`，llama.cpp（`llama-mtmd-cli`，CPU、GGUF）跑 `Qwen3-VL-2B-Instruct-Q4_K_M`；
图**先缩到 512 宽（JPEG q75）再喂**，跑完临时文件删掉；答案按 `vision_keywords.VERDICTS` 同义词表白名单映射成
`work/play/unknown`，表外词一律 unknown。落 `shot_verdicts`，PK `(sha256, version)` —— 换模型可并存对比。
挂 `hub-rules derive`（`vision.vlm.enabled=true`），不新增定时器。

**为什么不是 0.6B**：先试了 `SmolVLM2-500M`（q8，峰值 0.6 G、5 秒/张）—— 把 bilibili 娱乐视频判成「工作」，
中英提示词都错。2B-Q4 峰值 2.8 G、9 秒/张，4 张标定全对。**这条任务瓶颈是判断力，不是速度。**

**踩坑**：不加 `-c 512 -t 2` 时内存尖峰把人整台机器顶到 OOM（用户当场指出「oom了」）；现在把上下文和线程钉死，
单张峰值 2.8 G。竖屏截屏（1280×1600，HiDPI）缩完是 512×640，比横屏多一倍分块、18 秒/张。

**验收**：`tests/verify_m12.sh` **13/13、退出码 0**（用假 runner，不依赖真模型）；全量 **21 套全 rc=0**。

---

## M13 输入节奏：键盘与鼠标的计数（2026-10-24，用户：「鼠标移动轨迹和神态、动作是专注度的标志，去确认一下」→「做完了」）

**查证结论（写在这里，避免以后重复查）**：

| 信号 | 文献证据 | 强度 |
|---|---|---|
| 鼠标轨迹 → 走神 | 272 人实验，5 种分类器，**F1 只比随机高 ≤15%**，且是受控按键任务 | 弱，可当辅助证据 |
| 键盘节奏 → 疲劳 | 按键按下/抬起时序，3 档疲劳识别 **98.11%** | 强（受控打字任务） |
| 表情/神态 → 专注 | 表情与专注等级相关；公开系统**预期 70–85%**，多为概念验证 | 中，隐私成本最高 |
| 小动作 → 走神 | 相关，不是判定 | 中 |

**做了什么**：`src/infohub/inputs.py` + `bin/hub-input` + `info-hub-input.service`（常驻）。
读 `/dev/input/event*` 的 `input_event`，**只累加计数**：按下次数 / 点击次数 / 滚轮格数 / 光标像素距离，
每 60 秒落一条 `input.sample`。`hub-activity day` 多打一行「手上」。
**结构上读不到内容**：payload 只有六个字段，没有 keycode、没有时间戳序列、没有文本；验收脚本专门断言这一点。

**权限坑（踩过）**：`/dev/input` 要 `input` 组；`usermod -aG` 之后**已登录的会话拿不到新组**，`id` 里看不见 ——
但 `/etc/group` 已经是成员了，所以 `sg input -c '...'` 立刻可用。另外 `systemd --user` **不支持**
`SupplementaryGroups=`（实测 `Unknown assignment`），所以服务单元用 `ExecStart=/usr/bin/sg input -c "..."`。

**实测**：25 个可读设备；60 秒窗口真数据 `{"clicks":7,"wheel":32,"dist_px":270,"keys":0,"devices":25}`；
服务内存 13 MB、CPU 86 ms/分钟。**没上摄像头**（表情那条证据最弱、隐私成本最高，等前两条跑出相关性再说）。

**验收**：`tests/verify_m13.sh` **15/15、退出码 0**（合成事件，不碰真设备）；全量 **22 套全 rc=0**。

---

## M14 在座状态 + 校准（2026-10-24，用户：「不是 3 最强吗」→ 决定按数据分顺序，不再按论文数字排）

**争论点**：摄像头那路论文写 70–85%，看着最高；但那个数是 Zenodo 预印本里的**「预期能做到」**，不是实测；
键盘那 98.11% 是实测但只在受控打字任务里；鼠标 +15% F1 是实测但在受控按键任务里。**三个数字不可比。**

**做法**：先做廉价的在座判定（不再用 2B VLM 看摄像头画面）。
- `src/infohub/camera.py`：YuNet（232 KB，OpenCV `FaceDetectorYN`，**12 ms/帧**）→ 三档 `away/present/facing`
  （最大脸 + 脸宽占比阈值 + 鼻尖/眼距偏航）。挂在 `hub-activity sample` 那一分钟，**不新增定时器**。
- `src/infohub/calib.py` + `bin/hub-calib`：`rate 1|2|3` 记自报，`report` 对每条自报取前 60 分钟特征算 AUC。
- `inputs.py` 加 `path_px` / `straightness`（路径长度 ÷ 净位移）——为「鼠标绕路」这一路备好原料。

**硬边界**：一帧只在内存里，判定完立刻丢；**不落盘、不落库、不压缩、不上传**；不做表情/情绪/身份。
payload 里只有 `state/faces/face_ratio/yaw/latency_ms/w/h/version`。

**实测**：真机取帧 640×480，检测 10–12 ms，`facing` 判定稳定（占比 0.22、偏航 -0.10）。

**验收**：`tests/verify_m14.sh` **13/13、退出码 0**（合成人脸框，不碰摄像头）；全量 **23 套全 rc=0**。

**还没做**：鼠标轨迹的「绕路」特征只是原料（`straightness`），要不要进专注度，等 `hub-calib report` 的 AUC 说话。

## M15 手机通知入中枢（最终存储在个人服务器，2026-10-24/26）

用户原话：m02648「是，把qq/微信消息纳入信息中枢统一管理」；m03453「有线开了，然后 本机info-hub也是要服务器为基准」。

链路：手机 `HubNotificationListener` → 本地 `hub.db` → `HubUploader`（20s/200 条/批，走既有 9444 加密 socket）
> ECS `phone-relay.service`（**纯 L4 转发，不落库**）> 家庭机 `hub_server.py`（**最终存储**）> 本采集器 `phone-hub` > 本机 info-hub `events`（**镜像**）。

- 2026-10-26 迁移：ECS 上 10322 行并入家庭机（6499 + 3823 = 10322，QQ 4738）；ECS 旧库归档 `archive/hub.db.20261026`。
- 采集器 `ssh_host` 改为 `root@<bridge-host>`（直连 wg，不再经 wireproxy 代理）。

- 采集器：`src/infohub/collectors/phone_hub.py`，source=`phone-hub`，kind=`notify`，
  游标 `{last_ts, fail_streak, backoff_until_ts}`；只读 ECS，不直接联系手机。
- 远端工具：ECS `/opt/mcp-bridge/ai_mcp_server/hub_dump.py <since_ms> <limit>`，
  SQL 必须是 `WHERE ts > ? ORDER BY ts ASC LIMIT ?`（按最新取批会让游标跳过历史，踩过一次）。
- 配置：`collect.phone-hub.*`（见 INTERFACE.md §3）；`limit` 默认 1000。
- 实测：服务器库 3601 行（QQ 974）；本机回填 4401 条（含真实 QQ 群消息）；`info-hub-collect.timer` 每 5 分钟一轮。
- 剩余验收：真实新消息自然落库复核（合成通知已验证，历史已灌入）。

---

## M16 行为特征层：把事件翻译成「人」的指标（2026-10-26，用户：「读取手机的屏幕使用时间，电脑的活跃窗口，键盘的特征…这个才是最重要的」）

**这一层就是 m00113 的那个「转译层」**：`events` 是原料，`features` 是成品 —— 一小时一行，直接回答
「何时开始干活 / 专注程度 / 多久」。

**已接进来的三路**
| 路 | 实现 | 状态 |
|---|---|---|
| 键盘节奏 | `inputs._rhythm()`：`iki_median_ms / iki_cv_ms / pauses / burst_max / bs_ratio` 五个**聚合值**（code 只在进程内比较，payload 无 keycode） | ✅ 真机在采 |
| 电脑窗口 | `window.snapshot` 的应用集合变化 → `switch_count` | ⚠️ 只有「切换次数」，**没有「哪个窗口在前台」** |
| 手机使用 | 通知流聚类（间隔 ≤5 分钟算连续）→ `phone_notif_min` + 条数 | ⚠️ 下界估计，不是系统级屏幕使用时间 |

**两处拿不到真数据的根因（都试过）**
- 活跃窗口：`org.gnome.Shell.Introspect.GetWindows` 在 GNOME 45+ 对非 shell 调用者返回 `AccessDenied`；
  AT-SPI 的 `STATE_ACTIVE` 在 Wayland 下恒为 0。**唯一干净解法是自己写个 GNOME Shell 扩展暴露 focus DBus**，代价是一次重登录。
- 手机屏幕使用时间：本机无 `adb`，Android 的 `UsageStatsManager` 数据只能从设备侧取
  （adb `dumpsys usagestats`，或手机端脚本上报）。通知推断只是下界。

**实现**：`features.py`（纯函数 + 幂等写表）、`bin/hub-features build|show`、`store.features_replace/features_day`、
`SCHEMA_M16`、`hub-rules derive` 每轮顺带 build 当天（不新增定时器）、`behavior.feat_version=feat-1`。

**验收**：`tests/verify_m16.sh` **26/26、退出码 0**（合成事件：100ms 连打 ⇒ 中位 100/连击 20/停顿 0；
2.5s 间隔 ⇒ 停顿 5；9 键含 1 退格 ⇒ 0.111；一小时 12 采样 9 在场 ⇒ 0.75；10/12 facing ⇒ 0.833；
6 条通知间隔 5 分钟 ⇒ 25 分钟；截图众数 code；空小时 presence 为 NULL 且 coverage 全 0；
重跑 24 行幂等；feat_version 新旧各 24 行）；全量 **25 套全 rc=0**。

**教训**：`store.events()` 返回的 `payload` 已经是 dict（不是 JSON 字符串），`features.py` 第一版又
`json.loads` 了一遍导致所有列静默变 NULL；**静默失败比报错危险** —— 特征层必须断言 coverage 不为全 0。


## M17 手机屏幕使用时间（2026-10-28）

**场景**：问「今天在手机上花了多久、都在干嘛」不能靠通知条数推断（那是下界，听歌/看视频不响铃就漏）；
Android 自己就有答案 —— `dumpsys usagestats` 的日桶里，每个 app 的 `totalTimeVisible` 是系统统计的「被看见时长」。

**做法**：`collectors/phone_usage.py` 走 USB adb 读一次 dump（1.9 MB / 1 万行），只解析**第一个聚合日桶**，
取 `totalTimeVisible` 求分钟，落一条 `phone.usage` 事件（`total_min` + `top_n` 个应用），
`features` 表新增 `phone_day_min`/`phone_day_top` 两列（整天每行同值，没有快照就是 NULL）。

**踩的坑（关键）**：Android 的时长串是 `DateUtils.formatElapsedTime` 语义 —— **两段是 `分:秒`，三段才是 `时:分:秒`**。
第一版按 `H:MM` 解析，`totalTimeVisible="53:31"` 被读成 53 小时，一天合计 222 小时；
用**事件流推算的时长**（`ACTIVITY_RESUMED`→`ACTIVITY_PAUSED`）交叉验证才定下来。

**另一个坑**：`float(cfg or 1800)` 会把合法的 `interval_s: 0` 吃掉变成 1800，测试里第二次采集被静默节流。

**验收**：`tests/verify_m17.sh` 27/27（合成 dump：日桶解析、周/月桶不算进来、两种时长写法、缺 `totalTimeVisible`
不记 0、`top_n` 截断、adb 失败不落半条、节流、features 两列、`show` 首行）；全量 **26 套全 rc=0**。
真机：今天 `1230 分 ≈ 20.5 小时`，最常用 `kajfosz.antimatterdimensions`（883 分）。

## S1–S17 插件化：本体只剩「输入 + 推送」，其余全搬成插件（2026-11-14）

**为什么**：原来规则、报表、控制台、7 个环境/派生模块都长在 `src/infohub/` 里，改任何一处都要动本体；
用户要的是「消息输入」和「消息推送到手机/电脑」当本体，其他都是插件（m00127）。搬完判据是机械的：
**停用/删掉插件 ⇒ 对应命令退出码 1 并点名插件**，本体其余部分照跑。

**本体留下的四道缝**（都是同一形状：清单 → 解析 → 加载，失败三分）：

| 缝 | 文件 | 管什么 |
|---|---|---|
| 插件宿主 | `src/infohub/plugin.py` | 发现 `plugins/**/plugin.yaml`、校验键、按 `entry` 导入、隔离坏插件 |
| 来源注册表 | `src/infohub/sources.py` | `kind=source` 的 10 个采集器 |
| 视图缝 | `src/infohub/views.py` | `kind=view` 的 report/digest/week/board/ui |
| 派生缝 | `src/infohub/derived.py` | `kind=derived` 的 activity/vision/calib/features/input/camera/screencast |

投递台账 `deliveries` 表和三通道（`plugins/channels/{desktop-popup,phone-mcp,dsh-ask}`）是另一条线，见 INTERFACE §11/§12。

**开关只有一处语义**：`plugins.<name>.enabled=false` 才真正卸掉（缺省 true）；缺省是否自动采由 `plugin.yaml:default_enabled` 决定；
`--source` 只看安装开关，不看 `collect.<name>.enabled`。见 INTERFACE §4.4/§10。

**踩过的最贵的一个坑（门面转发）**：先给 7 个搬走的模块在 `src/infohub/<name>.py` 留了「转发门面」，
以为旧 import 全兼容。结果测试的 monkeypatch 打在门面命名空间（`SC._call_request=`、`A.Env=`），
真函数在插件模块的 globals 里跑，补丁不生效 —— `verify_m7` T13 直接把真 portal 打穿
（`AttributeError: 'object' object has no attribute 'get_unique_name'`）。**结论：不兼容门面不做**；
测试改 `tests/derived_mod.py` 按路径加载真模块（`load(name)` / `install()`）。门面遗骸在
`~/hub-tasks/backups/derived-facades-*`，只当反面样本，不入库。

**验收**：`tests/check_derived_plugins.sh` 37/37（7 行 status=ok；每插件停用 ⇒ rc1 + 点名；启用 ⇒ rc0；
目录缺失 ⇒ rc1 + 点名「未安装」；7 个实现文件不在 `src/infohub`）；`tests/check_ui_plugin.sh` 5/5；
全量 **27 套 verify + 7 套 check 全 rc=0**。逐条 commit：S9 `200ff0b` / S11 `fa3e890` / S13 `4a0f486` /
S14 `6e38039` / S15 `cd10b99` / S16 `c295f7f` / S17 `fa88381`+`bc726d7`。

