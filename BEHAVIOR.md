# info-hub 行为层设计草案（数据 → 行为特征 → 判定口径）

- 日期：2026-10-22
- 状态：**部分实施**（2026-10-22）—— M10 转译层已落地：`activity` 采样 + `episodes` 片段 + 专注度 + `bin/hub-activity`，`tests/verify_m10.sh` 18 项全过；**M11 画面语义层也已落地**（`bin/hub-vision`，把已存截屏读成类别，`tests/verify_m11.sh` 16 项全过）；§10 的五个待定项仍待拍板，L1-b「产出事件」与 actor 归属**还没做**
- 输入：用户 m00001（闭环设计：原始记录→行为数据→信号判定→策略→推送）、m00005（委派：盘点剩余 + 补"数据到行为特征"的分析层）
- 前置文档：`README.md`（现状与用法）、`PROJECT.md`（场景→用法→嵌入、分期、各期记录）、`INTERFACE.md`（契约）、`DELIVERY.md`（交付与未决项）
- 落位：本文只是**设计草案**，尚未进 `INTERFACE.md`；实施时每节逐条落成契约 + `tests/verify_m9.sh` 起的机械验收。

---

## 0. 先跑一下（M10 转译层，已实现）

```bash
bin/hub-activity sample                        # 采一条：现在人在不在（在场/离开）
bin/hub-activity day                           # 今天：几点开始干活 / 在场合计多久 / 平均专注度 / 每段明细
bin/hub-activity episodes --day 2026-10-22     # 重算并落库当天的片段（幂等）
```

口径（`hub.config.yaml` 的 `behavior:` 段）：在场 = 空闲 ≤60s；相邻采样间隔 >300s 即断段；
在场累计 <60s 的段不落库；专注度 = 在场比例 0.5 + 窗口稳定度 0.3 + 无长空档 0.2，加权到 0–100。
**专注度不读屏幕内容**，只由这三样算出，所以它分不清「写代码」和「刷视频」—— 对外必须带上这句。

---

## 0. 三个结论（先看这个）

1. **采集这半边已经做完，而且在跑**：6 个源、76298 条事件、6 个 systemd 定时器、18 套验收脚本**全 rc=0**（本文写作当天 2026-10-22 19:58 实跑，不是引用旧报告）。不需要重做，也不需要换框架。
2. **卡住北极星的不是缺采集，是缺三样东西**：
   - `actor` —— 分不清"你在干活"和"Agent 在替你干活"（dsh-session 里 user 事件只占 2.2%）；
   - `产出事件` —— 分不清"文件改了"和"出活了"（fs 只记 `mtime + size`）；今天 90 条 fs 事件、69 个文件改动、**0 次 commit**，现有数据既说得出"改了"也说不出"出了什么"；
   - `连续活动时钟` —— 分不清"开着"和"动着"（window 只在界面集合变化时落库，间隔中位数 **2394 秒**）。
   这三样不补，往上加多少分析层都只是在算噪声。
3. **分析层的正确定位是"可重算的测量"，不是"判定"**：特征 = `f(events)`，必须能在任意时刻**全量重放**得出同一结果；成/败的判定单独一层，且必须带**反事实**（§7）。

---

## 1. 先定死三条口径（上一轮讨论落成的规则，可机械判）

| 编号 | 规则 | 机械判据 |
|---|---|---|
| **R1** | **北极星 ≠ 梯度** | 产出只进「验收」报告；策略学习用的必须是**相对基线的增量**，不是产出的绝对值 |
| **R2** | **不许用时长冒充产出** | 任何产出判定里出现"持续 N 分钟"即为违规（它是动作，不是产出） |
| **R3** | **没有对照组不许写"有效"** | 报告里出现"推送有效"必须同时给出 no-push 对照窗口的产出，缺一即判无效 |

**为什么 R1 是硬规则**（一句话）：Agent 只控制"推不推"，产出在三四跳之后（推 → 你启动 → 你干活 → 产出），中间混着任务类型、你当天的状态、外部打断。拿产出**绝对值**当梯度，策略一定收敛到"在你本来就要干活的时候推"——白嫖归因，指标漂亮，价值为零。

> 这条规则不是洁癖：它决定了 §7 的判定层必须能生成反事实，而不是只统计"推了之后有没有动静"。

---

## 2. 现状盘点（真机数字，2026-10-22 19:45 实测）

### 2.1 已经在跑的

| 层 | 现状 | 证据 |
|---|---|---|
| 采集 | 6 个源：`dsh-session` / `fs` / `browser-history` / `clipboard` / `window` / `screenshot` | `bash bin/hub-store stats` |
| 加工 | 4 条无状态计数规则 → `todos`；每条带 `source_event_ids` 证据链 | `src/infohub/rules.py:199/214` |
| 出口 | 报表（5 个区块）、周报、每周归纳（离线本地后端）、本地控制台 `127.0.0.1:8799` | `hub.config.yaml` `report.blocks` |
| 通知 | 通道 + 门 1–8 + 动作回写 + 类别抑制全部实现；**总开关 `notify.enabled=false`** | `hub.config.yaml:108` |
| 运维 | 6 个 systemd user 定时器，NEXT 全非空 | `systemctl --user list-timers 'info-hub-*'` |
| 验收 | 18 套脚本，**2026-10-22 19:58 实跑全部 rc=0**（`for f in tests/verify_*.sh`）；"过"= 退出码 0 | 本次实跑 |

**库里现在有什么**（`~/.dsh/info-hub/hub.db`，44.3 MB）：

```
事件: 76298  首=2026-07-25T10:54:37+08:00  末=2026-10-22T19:45:13+08:00
  dsh-session     64532   （其中 user/message 仅 1402 条 = 2.2%）
  browser-history 10771 + browser.video 226
  fs                727
  screenshot        168
  window             44
  clipboard          56
```

**这说明的事**：库里"本机在发生什么"的记录**九成以上是 Agent 自己产生的**（`tool/call` 9738、`tool/result` 9756、`step/start` 8691、`assistant/message` 8623、`job/*` 6994…）。这一条直接决定了 §5 的 P0-1。

### 2.2 三个已经能用的东西（不要重造）

1. **证据链**（M3c）：`todos.source_event_ids` → `hub-store todo <id>` 逐条回查原始事件，`missing` 如实暴露。**这一条要原样继承到特征层**（§6.6）。
2. **事件 append-only + 唯一去重键**：`UNIQUE(source, fingerprint, ts)`。这是"特征可全量重放"的地基，白送的能力。
3. **通知的门 1–8**：开关 / 勿扰 / 空闲 / 界面事件 / 节流 / 类别抑制 / 稍后 / 证据。策略层将来要发任何东西，都必须走这套门，**不许另开通道**。

---

## 3. 场景（先场景；每条挂真实锚点）

| 编号 | 场景 | 真实锚点 | 错了的代价 |
|---|---|---|---|
| **S1** | 想知道"今天到底有没有出活" | 2026-10-22：`宣传片/野生开源软件-观点视频/` 下 69 个文件有改动、0 次 commit。**现在的报表只能说"改了 69 个文件"，说不出"出了一个视频工程"** | 高。北极星本身不可测，整套闭环没有终点 |
| **S2** | 想知道"这次推送值不值" | `notify.enabled=false` 已 6 天，53 条 open 待办全部未提示；一旦打开，没有任何"推了之后行为怎么变"的记录 | 高。缺它，策略层只能靠感觉调 |
| **S3** | 想知道"这段是不是被打断了" | `window` 事件 6 天只有 44 条，间隔 p50 2394 秒、最长 87306 秒（24.3 小时）——**开着一个窗口和真的在干，在这份数据里长得一样** | 中高。打断源找不到，就没法解释"启动了没产出" |
| **S4** | 想知道"这周是不是在同一个方向" | 周报现在只有待办/通知/归纳，没有"你这周的时间花在哪类事情上" | 中。方向漂移发现得太晚 |
| **S5** | 想知道"是不是在硬撑"（健康） | 现有数据能看出深夜还有 `dsh-session` 活动，但分不清是人还是 Agent | 中高。这条错了会把人往过劳推 |
| **S6** | 想知道"哪些活其实可以让 Agent 干" | 库里 Agent 侧事件占九成，但没有任何"替你干了多少"的统计 | 低。是优化项，不是地基 |

---

## 4. 用法（四问；每条实现都必须能回答）

### 4.1 谁触发

- **采集（L0/L1）**：systemd 定时器自动，**不需要人**。这是"无感"的定义。
- **计算（L2 特征）**：定时器增量算当天窗口 + 每日结算一次。
- **看（L3 报告）**：只有人打开报表/周报/控制台才看；**永不主动推送**（AGENTS.md §一）。

### 4.2 何时触发

| 产物 | 触发时机 | 为什么是这个时候 |
|---|---|---|
| `activity_samples` | 每 60 秒一条（`info-hub-activity.timer`） | 60 秒是"人还在不在"的分辨率上限；再密就是浪费 |
| `output_events` | 每 60 秒，与采样同轮 | 产出是稀疏事件，靠轮询发现即可 |
| `episodes` | 每 5 分钟扫一遍近 2 小时 | 片段要等"尾巴"确定才能封闭；2 小时窗口足够 |
| `behavior_features`（日内） | 每小时算一次当天窗口，覆盖写 | 报表要能随时看到"到现在为止" |
| `behavior_features`（日结） | 每天 23:45 | 与现有 `info-hub-report.timer`（23:50）错开 5 分钟 |
| 判定 / 反事实 | 每周日 23:20，在现有 `digest`（23:30）之前 | 周报要引用它；且必须"先算完再写" |

### 4.3 延迟预算（场景的属性，不是工具的属性）

| 场景 | 可接受延迟 | 超了会怎样 |
|---|---|---|
| S1 "今天有没有出活"（日终看） | ≤ 15 分钟 | 无所谓，人不会盯 |
| S2 "推了值不值"（周看） | ≤ 1 周 | 无所谓，但**样本要够**：一周 <2 个对照窗就该判"样本不足" |
| S3 "被打断"（事后回溯） | ≤ 1 小时 | 超了就只能事后补，不能实时解释 |
| S5 "硬撑"（日看） | ≤ 1 天 | 超了这条就没有干预价值，只剩记录价值 |

**结论**：这套东西**没有实时性要求**。凡是"必须秒级"的设计都是过度工程 —— 记这一条是为了挡住后续"加个常驻 daemon 做流处理"的冲动。

### 4.4 失败语义（必须可机械断言）

| 情形 | 行为 | 判据 |
|---|---|---|
| `mutter` 空闲取不到 | 该轮 `sample.idle_s=NULL`、`src='missing'`；`active_minutes` 记 `measured=0` | **不许填 0** |
| `pyatspi` 挂了（拿不到活动窗口） | 仍记 sample（idle 在）；`app/title=NULL`；片段切分退化到"只按 idle" | sample 数不下降 |
| 产出源全缺（无 git、fs 无改动） | 当天 `output_events` 为空 **且** `measured=0`；报表写「未测量」 | **不写「无产出」** |
| 特征定义版本升级 | 旧行保留、新版本另存；报表只读最新版；`rebuild` 可全量重放 | 两次 rebuild 字节相同 |
| 事件被 `prune` 清掉 | 特征行 evidence id 标 `missing`，与 M3c 同一条铁律 | `missing` 计数 > 0 就出现在报表 |
| 采样/特征进程崩 | 退出码非 0 + journal；下轮从 events 重算补齐（纯函数 ⇒ 天然自愈） | 崩溃后无空洞 |
| 一次推送后没有任何行为 | 记 `eval='no_behavior'`，**不参与比率** | 不把"没测到"算成"没用" |

> 最后一条最容易写错：把"没反应"和"没测到"混在一起，比率就是假的。这跟 §4.4 第三行是同一条纪律的两次出现。

---

## 5. 缺口清单（还剩什么，按"挡住北极星"排序）

### P0 —— 不补，北极星就是不可测的

**P0-1 `actor` 不可区分**

- 现象：`dsh-session` 64532 条里 `user/message` 只有 1402 条（2.2%），其余是 `tool/*`、`step/*`、`assistant/*`、`job/*`、`llm/*`；`fs.change` 完全不知道文件是谁写的。
- 后果：任何"活跃分钟"都会因为 Agent 在跑而**恒满**；任何"产出"都可能记在"你"头上而实际是委派。S5（硬撑）会被误判成勤奋，S1（出活）会被 Agent 的产出虚高。
- 最小修法：`events` 加 `actor TEXT`（列级迁移，不重建表）；采集时按 `kind` 打标（`user/message`→`user`，`tool/*`/`step/*`/`assistant/*`/`job/*`→`agent`，其余 `unknown`）；`fs.change` 用"±5 秒内是否有指向同一路径的 Agent 工具调用"判，判不出就 `unknown` 并统计 `actor_unknown_ratio`。
- 工作量：小（1 个迁移 + 3 个采集器改 10 行 + 1 条验收）。

**P0-2 产出信号不存在**

- 现象：`fs` 的 payload 只有 `{root, path, mtime, size}`（`src/infohub/collectors/fs.py:53`），没有内容哈希、没有 diff、没有 git。
- 后果：R2 写了"不许用时长冒充产出"，但现在**也没有别的可选**——系统里根本没有产出事件。
- 最小修法：新建 `output_events`（§6.3），三个来源：
  1. **`git_commit`**：对 3 个真实仓库（`default-workspace/`、`freellmapi/`、`_recon/dsh-meme/`）跑 `git log --since`，取 commit + diffstat；
  2. **`fs_net_growth`**：同一路径相邻两次 `fs.change` 的 `size` 差 > 阈值（门槛进 config），且路径不在忽略表；
  3. **`task_done`**：看板卡片状态翻到 done（复用 `board.py` 已有通路，`board.enabled` 仍默认关）。
- 工作量：中（1 个新入口 + 1 张表 + 忽略表 + 3 条验收）。

**P0-3 没有连续活动时钟**

- 现象：`window` 只在"界面集合的 hash 变了"才落库（`src/infohub/collectors/window.py:53`），44 条 / 6.2 天，间隔 p50 2394 秒，最长 24.3 小时。
- 后果：切不出"专注段"，S3（被打断）无法回答；`episodes` 层没有输入。
- 最小修法：新建 `activity_samples`（§6.2），每 60 秒一条 `{ts, idle_s, app, title, app_hash}`。空闲走 `mutter`（`notify.py` 和 `screenshot.py` 已经在用同一个口子，复用即可）；活动窗口走 `pyatspi` 的 `STATE_ACTIVE`（**这条待实测**，拿不到就退化，见 §4.4 第二行）。
- 工作量：小（1 个采集器 + 1 个定时器 + 1 条验收）。

### P1 —— 补不上，分析层没有立足点

**P1-1 没有"一段工作"这个实体**

- 现象：`rules.py` 全是"对事件窗口计数"（`fs-repeat` 24h 内 ≥3 次、`session-stale` 48h 没动静…），没有任何"一段时间"的对象，更谈不上它有没有产出。
- 后果：m00001 里"启动无产出"这个失败类，**现在无从表达**——没有"启动"这个对象。
- 修法：`episodes`（§6.4）。

**P1-2 没有特征表（现在只有 4 条计数规则）**

- 现象：全库 8 张表（`events` / `collector_state` / `todos` / `todo_revisions` / `report_revisions` / `notifications` / `digests` / `kind_suppressions`），没有一张是"测量值"。
- 修法：`behavior_features`（§6.5）。

**P1-3 特征不可重算、不可版本化 —— 这是分析层的生死线**

- 现象：现在没有任何"定义版本"的概念；`hub.config.yaml` 里的阈值改了就是改了，历史无法对齐。
- 后果：特征定义**一定会改三次以上**（你在 m00001 里已经改过一次口径）。不能重算 ⇒ 每改一次，历史全部作废，三周实验直接归零。
- 修法：`def_version` 列 + `rebuild` 子命令（§6.6 第 1 条）。**这条不做，§6 整层不用写。**

### P2 —— 有数据之后才谈得上

**P2-1 没有判定层**：现在只有 `notify.effectiveness`（M4a：`acted`/`ignored` 比率 + 自动降频，`src/infohub/notify.py:422`），那是"通知有没有被点"，不是"行为成没成"。
**P2-2 没有反事实基础设施**：没有"不推时段"的概念，也没有基线。R3 因此无法满足。
**P2-3 推送台账没有回链**：`notifications` 表有 `todo_id`/`action`/`dbus_id`，但没有"推了之后 N 分钟内的产出"。且总开关关着。
**P3-1 奖励层完全不在这套系统里**（家庭群日报）。建议**不要**塞进来：本系统的边界是"数据不出本机"（README 隐私节），家庭群需要出网，两件事分开做。

---

## 6. 分析层设计（L1 活动与片段 → L2 特征）

### 6.1 分层

```
L0 events              ← 已有，append-only，不动
   ↓ (60s 轮询)                     ↓ (60s 轮询)
L1-a activity_samples        L1-b output_events
   ↓ (5min)
L1-c episodes
   ↓ (1h 增量 / 23:45 日结)
L2 behavior_features   ← 纯测量，可全量重算，带证据
   ↓ (周日 23:20)
L3 判定与反事实  ← 成/败、Δ产出、2×2
   ↓
报表 / 周报（只呈现，不推送）
```

**铁律**：L1/L2 **只产出测量**，不产出"你应该做什么"；任何"建议/提醒"只能由现有 `rules → todos → 通知门 1–8` 这条路走。这一条是为了不让行为层悄悄变成一个新推送通道（它和 README 的一句话边界直接冲突）。

### 6.2 L1-a `activity_samples`（新采集器 `activity`）

```sql
CREATE TABLE activity_samples (
  id       INTEGER PRIMARY KEY,
  ts       TEXT NOT NULL,        -- 采样时刻，ISO+08:00
  idle_s   REAL,                 -- mutter IdleMonitor 秒数；取不到 = NULL
  app      TEXT,                 -- 活动窗口应用名；取不到 = NULL
  title    TEXT,                 -- 前 120 字；取不到 = NULL
  app_hash TEXT,                 -- 窗口集合指纹（复用 window.py 的 digest 算法）
  src      TEXT,                 -- mutter+pyatspi | mutter | missing
  privacy  TEXT DEFAULT 'sensitive'
);
CREATE INDEX activity_ts ON activity_samples(ts DESC);
```

- 参数（**全部进 `hub.config.yaml`，唯一数值源**）：`interval_s=60`、`title_chars=120`、`retention_days=90`。
- **不做的事**：不截屏、不取窗口内容、不读键盘。活动窗口标题已经是本项目允许的最深一层（与 `window` 采集器同级）。

### 6.3 L1-b `output_events`（产出事件，**定义必须版本化**）

```sql
CREATE TABLE output_events (
  id           INTEGER PRIMARY KEY,
  ts           TEXT NOT NULL,
  kind         TEXT NOT NULL,    -- git_commit | fs_net_growth | task_done
  actor        TEXT NOT NULL,    -- user | agent | unknown
  target       TEXT NOT NULL,    -- 仓库路径 / 文件相对路径 / 任务 id
  magnitude    REAL,             -- diff 行数 / 净增字节 / 1
  def_version  TEXT NOT NULL,    -- 例 'out-1'（定义一改就换版本号）
  evidence     TEXT NOT NULL,    -- JSON 数组：事件 id（M3c 同一条铁律）
  UNIQUE(kind, target, evidence)
);
```

**`actor` 是两根轴，别混**（这是 P0-1 的落地）：

| 轴 | 取值 | 判法 |
|---|---|---|
| 谁在操作机器 | `user` / `agent` / `unknown` | 按事件 `kind` 打标 + 并发工具调用关联 |
| 谁出了这份活 | `亲手` / `委派` | `git_commit` 看 author + 当时是否有 Agent 在跑；`fs_net_growth` 继承上游 `actor` |

**语义**：`亲手` 的产出计入"你出活"（北极星）；`委派` 的单独算"系统替你干了多少"（P3/S6），**不混进同一个数**。混了就等于奖励"让 Agent 干活"，那跟"你出活"是两回事。

**不采集的东西（明确写死，防止后来越加越宽）**：不做 OCR、不读屏幕内容、不抓 IDE 内部编辑事件、不统计按键数。产出必须来自**外部可验证的状态变化**（git 仓库、文件大小、任务系统），不从"屏幕上有字"推。

### 6.4 L1-c `episodes`（一段工作）

```sql
CREATE TABLE episodes (
  id            INTEGER PRIMARY KEY,
  start_ts      TEXT NOT NULL,
  end_ts        TEXT NOT NULL,
  active_s      INTEGER NOT NULL,   -- 片段内 idle < 阈值 的采样数 × 60
  gap_max_s     REAL,                -- 片段内最大空洞（衡量"零碎"）
  app           TEXT,                -- 占比最高的应用
  project       TEXT,                -- 由 output_events/fs 路径反推的项目名
  switch_count  INTEGER,             -- app_hash 变化次数
  output_count  INTEGER,             -- 落进 [start_ts, end_ts+300s) 的产出事件数
  output_owned  INTEGER,             -- 其中 actor=亲手 的条数
  def_version   TEXT NOT NULL
);
```

**切分规则（可机械判、可重放）**：

1. 相邻 `activity_samples` 间隔 ≤ `gap_s`（默认 300 秒）归同一片段；
2. 片段内 `idle_s < idle_s_max`（默认 60 秒）的采样才算 `active`；
3. `active_s < min_active_s`（默认 120 秒）的片段**不落库**（过滤"点一下就走"）；
4. 产出事件归到它时间上所属的片段，**允许 ±300 秒宽限**（写完文件才切窗口是常态）。

四个参数全部在 `hub.config.yaml`。改参数 = 重算，不删历史（因为 `events` 在）。

### 6.5 L2 `behavior_features`（本次要补的那一层）

```sql
CREATE TABLE behavior_features (
  id          INTEGER PRIMARY KEY,
  window      TEXT NOT NULL,     -- day:2026-10-22 | week:2026-W43 | episode:<id>
  name        TEXT NOT NULL,
  value       REAL,
  unit        TEXT,              -- min | count | s | ratio | bytes
  measured    INTEGER NOT NULL,  -- 1=有值（哪怕 0）；0=未测
  reason      TEXT,              -- 未测原因：no_idle_sensor | no_output_source | no_samples
  def_version TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  evidence    TEXT,              -- JSON 数组：支撑这个值的 event id（有上限，取代表样本）
  UNIQUE(window, name, def_version)
);
```

**特征目录（第一版 7 组 21 个，先少后多）**

| 组 | 特征 | 定义 | 依赖 | 缺测条件 |
|---|---|---|---|---|
| 在场 | `active_minutes` | 当天 `idle_s<60` 的采样数（×1 分钟） | activity | 无 idle 源 |
| | `away_minutes` | 当天 `idle_s≥300` 的采样数 | activity | 无 idle 源 |
| | `first_active_ts` / `last_active_ts` | 当天第一次/最后一次 `active` | activity | 无 active |
| 片段 | `episode_count` | 落库片段数 | episodes | 无片段 |
| | `median_episode_min` / `longest_episode_min` | 中位/最长片段时长 | episodes | 无片段 |
| | `switch_per_hour` | 片段内 `switch_count` / active 小时 | episodes | 无片段 |
| 产出 | `output_user` | `actor=亲手` 的产出事件数 | output | 无产出源 |
| | `output_agent` | `actor=委派` 的产出事件数 | output | 无产出源 |
| | `output_magnitude_user` | 亲手产出的 `magnitude` 合计 | output | 无产出源 |
| | `output_per_active_hour` | `output_magnitude_user` / active 小时 | output+activity | 任一缺 |
| 启动 | `push_count` | 当天真弹的通知数（`notifications.shown=1`） | notifications | 通知关着 ⇒ 0 但 `measured=1`（"确实没推"） |
| | `first_output_latency_s` | 推送后到第一个亲手产出的秒数 | notify+output | 推送后无产出 |
| | `start_without_push` | 有产出但之前 2 小时无推送的片段数 | output+notify | — |
| 恢复 | `recovery_s` | 离开 ≥5 分钟后回到"同一项目"的间隔中位数 | activity+output | 采样不足 |
| | `abandoned_episodes` | 有启动无产出的片段数（**"启动无产出"这个失败类的量化形态**） | episodes | 无片段 |
| 健康 | `episodes_over_90min` | 连续工作 >90 分钟不休息的片段数 | episodes | 无片段 |
| | `late_night_minutes` | 23:00–03:00 的 `active_minutes` | activity | 无 idle 源 |
| 质量 | `unmeasured_ratio` | 当天 `measured=0` 的特征占比 | 自身 | — |
| | `actor_unknown_ratio` | `actor=unknown` 的产出事件占比 | output | — |
| | `coverage_ratio` | 当天实际采样数 / 应有采样数（86400/60） | activity | — |

**每个特征必须能回答三个问题**：① 它的值从哪几条事件来（`evidence`）；② 它什么时候是"未测"（`measured=0` + `reason`）；③ 定义改了怎么对齐（`def_version`）。

### 6.6 三条硬约束（漏一条后面全是坑）

1. **纯函数 + 可全量重放**
   `bin/hub-features rebuild --from 2026-10-01 [--def-version out-2] [--diff]`：
   删掉该区间该版本的行 → 从 `events` 重算 → 写回。判据：**同一输入跑两次结果逐字节相同**。
   这条靠 L0 的 append-only 才成立，是白送的能力，不用就浪费了。
2. **每个值带证据**
   继承 M3c 的铁律：“看起来有依据”不算数，id 必须真在库里；被 `prune` 清掉就如实标 `missing`。特征层不许出现"这个数是我算的，但说不出从哪来"。
3. **缺测必须显式，禁止用 0 填充**
   `measured=0` 和 `value=0` 是两回事：前者是"没测到"，后者是"测到了，就是 0"。**用 0 填缺测，等于把"传感器坏了"读成"你没干活"** —— 这是这类系统最常见、也最难发现的错。

### 6.7 明确不要做的事

- 不在 L2 调 LLM。特征必须是确定性的、可复算的；要语义归纳走 `digest` 那条已有的路（且默认还是离线本地后端）。
- 不把 `episodes` 叫"专注"。它只知道"键鼠在动、窗口没换"，不知道你在专注还是在刷视频——**命名不许提前赋予判断**。
- 不把特征直接当结论。`abandoned_episodes=3` 是测量；"今天状态不好"是判定，属于 L3 且必须带反事实。

---

## 7. 判定口径（把 2×2 落成规则）

### 7.1 四格与对策

| | 有产出（亲手） | 无产出 |
|---|---|---|
| **启动了** | 有效推送 → 保持；一周只改一个变量 | **启动无产出** → 查任务难度 / 打断源 / 反馈缺失（**不要**降门槛） |
| **没启动** | **推力多余** → 该少推，不是失败 | 时机或门槛问题 → 优先调推送 |

第四格（没启动有产出）是 m00001 里漏掉的那格，也是最值钱的一格：**它是"推送无用"的证据**。缺了它，系统只会往"多推"的方向漂。

### 7.2 反事实基线（R1/R3 的落地）

- 基线只用**无推送窗口**算：同 `weekday × 2 小时时段 × 任务类型`，取近 4 周的中位数 `output_per_active_hour`。
- 一次推送的效果 = 推后 120 分钟内的 `output_magnitude_user` ÷ 该时段的基线。
- 输出只有三档：**高于基线 / 与基线无差异 / 低于基线**。样本 < 5 次推送 ⇒ 直接写「样本不足」，**不许出结论**（R3）。

### 7.3 对照组怎么排

- 每周随机抽 **≥2 个半天**（4 小时）标为 `no_push`，写进 `push_schedule` 表，**随机种子落库**（可复现、可审计）。**第一版按 §10-3 的建议先做 1 个半天/周，跑四周没漏事再加到 2**——先验证"漏得起"，再谈统计功效。
- 对照窗口里通知门加一条"该时段不评估"（相当于门 0），**不改门 1–8 的任何一条**。
- 代价要认：那半天里真有急事它也不会说。所以取半天为单位、且可随时手动取消（取消要留痕，否则对照被污染）。

### 7.4 目标写成约束优化，不写成最大化

```
max  E[Δ产出 / 次推送]
s.t. 每日打断次数 ≤ notify.max_per_day（现 6，建议收紧到 2）
     每周 no_push 对照时段 ≥ 2 个半天
```

**理由**：分母是你的工作时间，Agent 能影响它 ⇒ 纯最大化"单位时间产出"会学到"推更多、推更长"。约束写进配置，不写进 prompt（prompt 约束会被绕过，配置不会）。

---

## 8. 嵌入（缝在哪；每条必须对应上面某个场景的某条失败语义）

| 期 | 缝 | 改什么 | 对应 |
|---|---|---|---|
| M9 | `store.py` 列级迁移 | `events.actor`；新表 `activity_samples` / `output_events` | P0-1/P0-2/P0-3；S5 的"人 vs Agent" |
| M9 | 新采集器 `collectors/activity.py`、`collectors/gitout.py` | 注册进 `collectors/__init__.py` 注册表；失败只记 `collector_state`，不影响整轮 | §4.4 第 1–2 行 |
| M9 | 新定时器 `info-hub-activity.timer`（60s） | `packaging/` 加 2 个单元文件 | — |
| M10 | 新模块 `episodes.py` + `bin/hub-episodes` | 只读 `activity_samples` + `output_events` | P1-1；S3 |
| M11 | 新模块 `features.py` + `bin/hub-features` + `info-hub-features.timer` | 日内 1h 增量 + 23:45 日结 + `rebuild` | P1-2/P1-3；S1/S4 |
| M11 | `hub.config.yaml` 新增 `behavior:` 段 | 所有阈值唯一数值源 | §4.3 |
| M12 | 新模块 `verdict.py` + `bin/hub-verdict` | 2×2 标签 + 基线 + 样本不足判定 | P2-1/P2-2；S2 |
| M12 | `notifications` 加两列（`eval_id` / `feature_window`） | **不动门 1–8**，只加外键 | P2-3 |
| M13 | `push_schedule` 表 + 门 0 | 随机 no_push 时段 | R3；§7.3 |
| M13 | 报表加 `behavior` 区块 | 先报产出、再用启动率解释（m00001 的结论） | S1 |

**明确不改的**：`events` 的 append-only 与去重键、M3c 证据链、通知门 1–8、`rules.py` 的 4 条规则、控制台的 Host/Origin 校验。新东西**全部走新表新入口**，不往老结构里塞。

---

## 9. 分期（每期独立可验收、可停；判据必须是脚本退出码 0）

| 期 | 内容 | 机械验收判据 |
|---|---|---|
| **M9 采集补洞** | `actor` 列 + `activity_samples` + `output_events`（git/fs 两源） | 真机跑 24h：`activity_samples` ≥ 1300 条；`dsh-session` 的 `actor=unknown` 占比 < 5%；`output_events` 当天 ≥ 1 条且**今天那批 `宣传片/**/*.tsx` 被标成 `agent`**；合成夹具 `tests/verify_m9.sh` 覆盖"日得文件不算产出"（`dsh-skill-gate/plugin/logs/*.jsonl` 必须被忽略表挡住） |
| **M10 片段层** | `episodes` + 切分 + `bin/hub-episodes` | 合成夹具切出**确定**段数（边界 3 组：间隔 299/300/301 秒）；真机 7 天 episode 中位时长落在 [5,120] 分钟（不合理就调参数、不改代码）；四个参数改动只动 `hub.config.yaml` |
| **M11 特征层** | `behavior_features` + 21 个特征 + `rebuild` | 同一窗口 rebuild 两次**逐字节相同**；改 `def_version` 后旧行不动、新行另存；每个特征 `evidence` 可逐条回查且 `missing=0`；缺测样本 `measured=0` 且不出现在"值"的统计里 |
| **M12 判定层** | 2×2 + 基线 + `bin/hub-verdict` | 合成 4 格数据 → 4 种标签精确命中；无对照数据 ⇒ 输出「样本不足」而不是"有效"；`notifications.eval_id` 能回链到具体特征窗口 |
| **M13 策略层** | `push_schedule` + 门 0 + 报表 `behavior` 区块 | 随机种子落库，同一 seed 重跑得到同一批 `no_push` 时段；一周后能出 `Δ产出/次推送` 的区间；打断预算超限时**硬拒绝**（不是软提示） |

**总的验收**（跑三周后）：产出事件数（亲手）趋势上升 + 启动率能解释原因 + 特征判定抽查认可 ≥2/3。**产出不升，启动率再高也不算成立。**

---

## 10. 待定五件套（每项都要你拍一下）

### 10-1 产出事件的第一版定义（`out-1` 取什么）

- **待定**：`fs_net_growth` 的"净增多少算产出"。
- **为什么重要**：这是北极星的度量单位，定宽了会被刷，定窄了会漏。
- **选项**：① 净增 > 500 字节（宽）；② 净增 > 2 KB 且路径不在忽略表（中）；③ 只认 `git_commit` + `task_done`，fs 不计（严）。
- **权衡**：① 会把日志、生成物算进去（今天 `dsh-skill-gate/*.jsonl` 就是活例子）；③ 会把今天这种"写了几十个文件、一次没提交"的日子全判成没产出。
- **推荐**：**②**，并把忽略表做成独立可编辑文件（`hub.config.yaml` 的 `behavior.output.ignore_globs`），第一版就把 `*.jsonl` / `*.lock` / `package-lock.json` / `dist/` 挡掉。

### 10-2 `actor` 判到多细

- **待定**：`fs.change` 的归属判不出来时怎么办。
- **为什么重要**：判太粗，"委派产出"会算进"你出活"，北极星虚高；判太细，要读工具调用内容，隐私面扩大。
- **推荐**：只做"±5 秒窗口 + 同路径"的关联，判不出就 `unknown`，**并在报表里把 `actor_unknown_ratio` 摊出来**。宁可标未知，不猜。

### 10-3 对照组会不会误伤

- **待定**：每周 2 个半天不推，能不能接受。
- **为什么重要**：没有对照就没有 R3，也就永远证明不了"这套系统有用"；但对照期间真漏掉急事，代价是你自己付。
- **推荐**：先做 **1 个半天/周**，跑四周看漏事的实际次数；没有漏事再加到 2 个。手动取消要留痕（否则对照被污染，等于白做）。

### 10-4 特征与采样保留多久

- **待定**：`activity_samples` 90 天够不够；`behavior_features` 是否永久。
- **为什么重要**：采样每分钟 1 条 = 每天 1440 条，一年 52 万条；但它含窗口标题（`privacy=sensitive`）。
- **推荐**：`activity_samples` 90 天（够跑三周实验 + 回看），`episodes` 一年，`behavior_features` 永久（聚合值不含敏感文本，且它是"历史能不能对齐"的唯一凭据）。

### 10-5 家庭群奖励要不要进这套系统

- **待定**：日报/周报发群这件事，是本系统的一个出口，还是另一件事。
- **为什么重要**：本系统的边界是"数据不出本机"；发群必然出网。
- **推荐**：**不进**。本系统只产出报表文件，发不发、发什么由外部做。锚定产出这件事在报表里能表达（先报产出、再解释），不需要把出网能力塞进采集系统。

---

## 11. 反模式清单（写在这里，防止三个月后自己犯）

| 反模式 | 为什么必须禁 |
|---|---|
| 用 0 填缺测 | 把"传感器坏了"读成"你没干活"，且**事后无法分辨** |
| 把特征当结论 | `abandoned_episodes=3` 是测量，"状态不好"是判定，中间隔着反事实 |
| 在 L2 调 LLM | 特征必须可复算；LLM 一进来，"同输入同输出"就没了 |
| 用 commit 当唯一产出 | 2026-10-22：0 次 commit，69 个文件改动。只认 commit 会把这天判成空白 |
| 把日志/生成物当产出 | 同一天：`dsh-skill-gate/plugin/logs/*.jsonl` 就在改动列表里 |
| 用"持续 N 分钟"当产出 | R2。时长是动作，不是产出，这是 m00001 已经指出过的坑 |
| 一次改多个变量 | 一周只动一个，否则三周后说不清是谁起的作用 |
| 另开推送通道 | 行为层只测量；说话必须走现有门 1–8 |
| 加常驻流处理 | §4.3：这套东西没有实时性要求，分钟级轮询足够 |

---

## 附：本文与 m00001 的对应关系

| m00001 的设计 | 本文落成 |
|---|---|
| 三层指标（过程/结果/质量） | §6.5 特征目录分 7 组；"过程"=在场+片段，"结果"=产出，"质量"=质量组 |
| 产出事件定义写进数据契约 | §6.3 `output_events` + `def_version` |
| 失败新增"启动无产出" | §6.5 `abandoned_episodes` + §7.1 第二格 |
| 策略目标改为"最大化单位时间产出" | §7.4 改成**约束优化**（纯最大化会学到压榨） |
| 周报先报产出、再用启动率解释 | §8 M13 报表 `behavior` 区块 |
| 退出专注问一句"有产出吗" | 不进 m00001 原文的自动链路：自报只能当**检测器校准标签**，不做 ground truth，每天上限 1 次（§4.4 最后一行同一条纪律） |
