# info-hub 交付报告

日期：2026-10-16 · 工作区：`$HUB_ROOT/`
（本文只讲「交付了什么 + 凭什么算交付 + 还差什么」；设计与契约在
[PROJECT.md](PROJECT.md) / [INTERFACE.md](INTERFACE.md)，用法在 [README.md](README.md)。）

## 1. 一句话结论

**M0–M6 全部落地并各有机械可判的验收脚本；系统此刻真的在跑**（6 个 systemd 定时器 + 控制台），
并且按用户 2026-10-16 的拍板又推进了一步：**通知的触发面已接好**（M2f，「弹消息是弱提醒，是例外项目」——
但触发点钉死在"收到新界面事件"上，不是定时器）、**浏览器历史已接成真来源**（M5b，真机已收 237 条事件、
产出 48–50 条候选待办 —— 数字随开发期改动浮动）、**归纳层已开成每周一次**（M4d，默认离线本地后端 ⇒ 数据不出本机）。
通知这块要把两件事分开说，别混：
- **通道本身在真机弹过**，用户亲眼看到过（`PROJECT.md` §18 的真后端自检 `(uint32 70,)`，更早的手工探针 `(uint32 69,)`）—— 想随时自证就跑 `bash bin/hub-notify test`（探针，绕开门 1–8，不碰待办）。
- 但**走门的 B 形态闭环（按门评估 → 发 → 记账 → 按按钮回写）从未在真机发生**，`notifications` 台账只有 1 行（那是探针，`kind=probe`）—— 因为 `notify.enabled` 默认关着，等的正是 §3-1 用户要拍板的那件事。
除通知外，其余部分不是"写完了"，是"在跑着"。

## 2. 交付物与机械证据

### 2.1 交付物

| 类别 | 内容 |
|---|---|
| 代码 | `src/infohub/` 5261 行（21 个模块 + 11 个 `entry_*` 入口）+ `src/infohub/collectors/` 537 行（7 个采集器 + 注册表）+ `bin/` 14 个可执行入口（含 `hub-install`）；合计 5798 行 |
| 契约 | [INTERFACE.md](INTERFACE.md) 788 行（§1–§9：CLI / 配置键 / 采集器·规则·通知·报表·控制台·归纳·周报·bilibili·浏览器历史·**静默截屏 §4.13**·**历史分类 §4.14** 契约 / 数据表 §5.1–§5.8 / 失败语义 / 落位 / 验收判据 / 运行面） |
| 设计 | [PROJECT.md](PROJECT.md) 1080 行（§0–§36：场景→用法→嵌入、数据模型、分期路线、规模评估、待定五件套、各期实施记录 + 收尾记录 + **§32 M4d / §33 M5b / §34 M2f / §35 M7 / §36 M8**） |
| 验收 | `tests/` 3257 行：**18 套 228 项** |
| 运维 | `packaging/` 145 行（14 个 systemd user 单元文件）+ `bin/hub-install`（支持 `--dry-run`） |
| 文档 | README 202 行 + INTERFACE 788 行 + PROJECT 1080 行 + DELIVERY 本文件 |
| 依赖 | **零新增**（stdlib + 本机已有 `wl-paste` / `pyatspi` / `gst-launch-1.0` / `pipewire`；浏览器历史用 stdlib `sqlite3`） |

### 2.2 验收：18 套 228 项，全绿

```
$ for f in tests/verify_*.sh; do bash "$f" >/dev/null || echo "FAIL $f"; done; echo ok
m0 14 / m1 11 / m2 16 / m2b 16 / m2c 11 / m2d 11 / m2f 9 / m3 16 / m3b 9 / m3c 9 /
m4a 8 / m4b 9 / m4d 12 / m5 12 / m5b 18 / m7 22 / m8 16 / week 9   → 全 rc=0（2026-10-16 12:28 实测）
```

每套自带 `mktemp -d` 临时库 + 临时配置（不碰真库、不弹真窗），末尾 `[ "$FAIL" -eq 0 ] || exit 1`。
"过"的定义是**脚本退出码 0**，不是"我看了一眼觉得对"。

### 2.3 真机运行证据（不是测试环境）

```
$ systemctl --user list-timers 'info-hub-*'   → 6 timers listed，NEXT 全非空
$ bash bin/hub-store stats
库 16818176 字节
事件: 27639  首=2026-07-25T10:54:37+08:00  末=2026-10-16T12:30:08+08:00
  browser-history 9335 / dsh-session 18008 / fs 274 / clipboard 8 / screenshot 7 / window 7
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8799/   → 200
$ systemctl --user is-active info-hub-ui.service                  → active
$ bash bin/hub-rules --json derive | jq '.items|length'           → 50 条候选（随开发期改动浮动）
window 快照抓到真实应用标题: clash-verge / ptyxis / gnome-system-monitor / gnome-control-center
```

**M8 全量浏览历史（2026-10-16 12:25）**

```
$ bash bin/hub-collect --once --source browser-history --json
emitted=7073（180 天窗口；30 天窗口那轮已收 2262 条）→ browser-history 事件共 9335 条，全部 privacy=sensitive
采集耗时 0.89s/轮；再采 emitted=0（游标幂等）

$ bash bin/hub-history summary --days 1
最近 1 天浏览 383 个页面（去重后）：视频/音频 235、学习/课程 65、招聘/求职 31、AI 服务 27
主力站点：bilibili.com 233 · hrbust.edu.cn 34 · careersky.cn 31 · deepseek.com 18 · pintia.cn 18
分类覆盖：9335 条里「其它」只占 4.5%（16 类用途；规则顺序 = host → 站点 → 路径）

$ bash bin/hub-report generate → reports/hub-report.md 出现 `hub:auto:begin history` 区块
$ bash bin/hub-rules --json derive | jq '.items|length'  → 52 条候选（28 bili + 20 fs + 4 session）
```

**新增两项在真机上的闭环（2026-10-16 09:33）**

```
$ bash bin/hub-collect --once --source browser-history        # 定时器 09:15 已自动收过一轮
[browser-history] emitted=0 / nothing new                     ← 游标幂等
（库内 237 条：browser.repo 36 + browser.video 201，全部 privacy=sensitive）

$ bash bin/hub-digest run --window-days 7
digest #2 条目 6 丢弃无依据 0 耗时 48ms（后端 …/bin/hub-digest-local --window-days）
  · 待办：「info-hub/hub.config.yaml」24 小时内改了 7 次  evidence=[7752, 11065, 11285]
  · 待办：工具「tencent-connect/dsh-qqbot」在知识库里查不到  evidence=[15387]
$ bash bin/hub-digest run --window-days 7                     # 第二次
digest skip reason=reused（同一份输入已经归纳过，不重复花 token）

$ bash bin/hub-week run   → reports/hub-week-2026-W42.md  归纳 2 条/11 项，每条带 evidence 指针
$ bash bin/hub-report generate → reports/hub-report.md 的 digest 区块带 evidence 指针
```

**隔夜复查（2026-10-16 08:45）** —— 这组说明 M6 成不成立：

```
事件: 14093（10-15 21:30） → 14451，末=2026-10-16T08:41:40   ← 采集器整夜没停
info-hub-collect.timer  LAST=08:41:40（2 分钟前）   info-hub-derive.timer  LAST=08:41:29
info-hub-report.timer   LAST=07:01:05  ← **报表定时器真的自己跑了**，hub-report.md 1748→3279 字节
```

结论：**不是"装上了"，是"过了一夜还在跑、报表自己重写过一遍"。** 而且夜里几乎没有新事件
（23:26 之后只有 1 条）—— 它只记录真的发生的事，不自造活动。同一份报表里如实写着采集器异常
（`clipboard`：`wl-paste 退出 1: This seat has no keyboard`）—— 夜里的真实环境（无输入席位），
采集器按 INTERFACE §6 的失败语义 fail-soft、不中断整轮。

保留期 180 天、`prune` 定时器已在跑；数据只在 `~/.dsh/info-hub/`。

**M7 真机状态（2026-10-16 11:27 起）**

```
$ bash bin/hub-screencast --json consent      # 用户点了一次「共享」
{restore_token: "", node_id: 82, size: [1920, 1200], at: 2026-10-16T11:27:42+08:00}
```

同意本身成功了（portal 给了流：`node_id=82`、屏 1920×1200），但**当时没请求持久化**（`SelectSources` 少一个
`persist_mode`），所以 portal 没发 `restore_token` —— 按规范 `restore_token` 是 **Start** 的结果、且要 `persist_mode` 才会给
（已改：`persist_mode=2` + 从 `Start` 结果读、每次取帧后轮换，单次使用）。
**2026-10-16 11:41 用户点了「共享」**（`persist_mode=2` 补上后一次成功）⇒ `state/screencast.json` 落到持久令牌，
`collect.screenshot.enabled` 随即改成 true。**真机取帧证据（11:49）**：

```
事件 17670 @ 2026-10-16T11:49:21+08:00  source=screenshot kind=shot.saved privacy=sensitive
图 1280x800 / 421939 字节 / ~/.dsh/info-hub/screens/2026-10-16/114920.jpg / idle_s=5.195
哈希与事件 sha256 同口径一致；取帧全程**没有任何对话框弹出**（窗口列表只有 gnome-shell + ptyxis）
```

采集器**不会**弹同意框（没令牌直接失败并记 `run:screenshot`，回归 T14 钉住这条）。
活跃门的两个真机对照：11:49:21 手动一轮时 idle=5.195s（人在）⇒ 取帧；11:55:06 的**定时器轮**
（无人干预）当时空闲 216s ≥ 60s ⇒ `last_ok_at` 更新、`last_error=None`、**一条新事件都没落**。

**M2f 只读预览（真库、dry-run、不动一行）**：临时配置开 `enabled=true` + `backend=command` 指向假通知器 ⇒
`would_send=true`，正文 `「info-hub/tests/verify_m5b.sh」24 小时内改了 3 次（依据 3 条事件）`；
跑完 `notifications` 仍 1 行、`notified_at` 非空仍 0 条。
**当时"判 idle"是解析 bug 的产物**（真机 `(uint64 N,)` 的 `64` 被当高位吃进毫秒数，把 2 秒空闲读成 642 秒）；
修好后重跑同一条：`detail="空闲 433s ≥ 60s"`、`reason=idle`、`sent=0` ⇒ 这次"人不在就不弹"是真值。

### 2.4 逐期落在哪

| 期 | 内容 | 验收 | 记录 |
|---|---|---|---|
| M0 | 地基：库 + 事件总线 + 3 采集器 | `verify_m0.sh` 14/14 | §16 |
| M1 | 规则 → 待办 → 看板卡 | `verify_m1.sh` 11/11 | §17 |
| M2 / M2b / M2c / M2d / **M2e** / **M2f** | 通知五道门 → 按钮 → 忽略此类 → 打开/稍后 → **通道自检** → **触发面** | 15 + 16 + 11 + 11（M2e 并进 `verify_m2.sh`）+ **9**（`verify_m2f.sh`） | §18 / §24 / §26 / §27 / **§31** / **§34** |
| M3 / M3b / **M3c** | 报表与控制台 → 清单手改留痕 → **证据链 + 门 8** | 16 + 9 + **9** | §19 / §28 / **§29** |
| M4a / M4b / M4c / **M4d** | 有效性统计与自动降频 → LLM 归纳 → 周报 → **每周归纳（本地后端）** | 8 + 9 + 9 + **12** | §20 / §21 / §22 / **§32** |
| M5 / **M5b** | bilibili 收藏 → KB 对账 / **浏览器历史当来源（真机已跑）** | 12/12（合成源）+ **18/18** | §23 / **§33** |
| **M8** | **全量浏览历史 + 用途/网址分类 + 一条总结**（16 类用途、报表区块、`bin/hub-history`；卡片不推送） | **16/16** | **§36** |
| **M7** | **静默截屏**（ScreenCast 取帧：活跃才取、**打字中不取（等停顿）**、缩放图 + `sensitive` 元数据、留 7 天） | **24/24** | **§35** |
| M6 | systemd 真跑 + 安装器 | 人工：6 timers NEXT 非空 + 退出码 0 + **隔夜复查**（见 §2.3） | §25 |

**接手后补的两批**：
- M3c（上一任代码写完但验收 4/8 未过、文档未写）：根因是验收夹具 `tests/verify_m3c.sh` 仍在用
  `m2_seed.py` 的**手写假证据 id**，且对已解析字段二次 `json.loads`。改用 `tests/m3c_seed.py`
  （造真事件、让规则引擎自己产待办）+ 去掉重复解析 ⇒ 9/9；文档补 `INTERFACE §5.8 / §4.6 门 8 / §2.2 / §8`
  与 `PROJECT §29 / §9 / §15`。
- **M2f 触发面**：`hub-collect --notify` —— 只在**这一轮真收进新的 `window` 事件**时评估通知
  （一度想用 timer，否掉了：那会让「评估」按时间表发生，条文①就只剩门在挡）；
- M2e + M4d + M5b（按用户 2026-10-16 的拍板）：通知通道自检 `hub-notify test`（更正"通知从没弹过"的错述）；
  每周归纳换成离线本地后端（无人值守可靠）；浏览器历史接成真来源（真机 237 条事件、48 条候选）。

## 3. 用户 2026-10-16 拍板的四项（已落实）

用户原话（m00480）：「不用加，本来就是事件触发的，2直接我看我账号的历史记录，归纳层每周，代表不谢看板，
还有我需要知道现在有哪些监测」⇒ 逐条对应：

| 决定 | 落实 | 真机证据 |
|---|---|---|
| **② 用账号的历史记录取数** | 新采集源 `browser-history`（M5b）：只读 Edge `History` 的**副本**，取仓库页 + 视频页 | 真机 237 条事件（repo 36 / video 201）、规则候选 48 条；`verify_m5b.sh` 17/17 |
| **③ 归纳层每周一次** | M4d：`info-hub-digest.timer` 周日 23:30；后端换成**离线本地确定性**命令 | `digest #2 条目 6 / reused`；周报 `W42` 记 `归纳 2 条/11 项`；`verify_m4d.sh` 12/12 |
| **④ 不写看板** | `board.enabled` 保持 `false`（本来就没开） | `hub-rules derive` 不碰看板 |
| **⑤ require_ui_event 不加东西** | 保持 `true`（"本来就是事件触发的"） | 配置未动 |

**一件必须如实说的事**：他点的"bilibili 历史"那条路**本身产不出东西**。实测把最近 14 天去重后的
**1072 条真实 bilibili 标题**喂给 `bili.extract_tools` ⇒ 提出 **0 个工具**（标题里没有仓库链接）。
真信号在**同一份历史里的仓库页**：github/gitlab URL 242 条 ⇒ 去重 `owner/repo` **73** 个（30 天 68、14 天 40）。
所以"账号历史"是按**浏览器历史（不只 bilibili）**接的 —— 这不是扩大范围，是把你要的那份历史里真正有信息的那半接上。
`collect.bilibili.enabled` 仍是 `false`（要真接收藏夹 API 才需要它，属于另一件事）。

## 4. 仍待用户拍板的（唯一一项）：`notify.enabled`

### 4-1 `notify.enabled`：通知到底开不开（**唯一真正阻塞 B 形态的一项**）

- **待定的是什么**：PROJECT.md §13 那六条例外条文 —— ①只在有新的界面事件时评估，勿扰/空闲不发、**绝不按时间表推送**；
  ②频率上限；③**每条必须附依据、无依据不发**；④动作只做字面事（打开/稍后/忽略此类）；⑤措辞不催促；
  ⑥忽略率 >50% 且样本 ≥5 自动降频。**AGENTS.md §一禁止主动提醒**，要开通知就得同时写一条例外条文进
  `AGENTS.md`，而那属于权威层 ⇒ 按 §六**必须用户逐字过目**。
- **触发面已经就位（M2f）**：`hub-collect --once --notify` 只在**这一轮真收进新的 `window` 事件**时评估一次，
  且 `notify.enabled=false` 时触发面照走、门 1 挡住（采集不受影响）。`tests/verify_m2f.sh` 9/9。
  ⇒ 开关一开就真能跑，**不需要再补任何代码**。
- **为什么重要**：门 1–8 已全部落码并有验收（含门 8「无依据不发」），但 `notify.enabled=false` ⇒
  **走门那条闭环在真机上一次都没跑过，台账里只有探针那一行**。目前"门 1–8 能工作"只有 stub 证据。（**通道**本身在真机弹过，
  见第 1 节；`hub-notify test` 是随时可复现的自证命令。）
- **选项**：① 逐字确认后把例外条文落进 `AGENTS.md`，同时开 `notify.enabled`；② 逐字确认但**先只开 dry-run**
  （`backend=stdout`，只进日志不弹窗）观察若干天；③ 继续关着，只保留 A（控制台 + 报表）。
- **各自权衡**：① 真闭环，但一旦条文没写死，通知就可能变成"催促"（正是 §一要防的）；
  ② 零打扰、能攒真机数据来校准节流参数，代价是闭环仍差最后一步；
  ③ 最安全，但 M2 系列五期代码等于闲置。
- **我的推荐**：①，且**条文逐字定稿前不动**；若用户嫌一步到位风险大，② 是最好的中间态，
  `backend` 已经是配置键，切换不改一行代码。

> 另两项**未表态**（我按"维持现状"处理，随时可改）：通知类型白名单（现在没有，`fs-repeat` 这类
> 开发期churn 也会候选）、`retention_days` 仍 180 天（PROJECT §12-5 当初推荐过"30 天 + UI 一键清空"，
> 但 UI 目前没有清空路由，只有 CLI `hub-store prune`）。

## 5. 已知局限（如实列，不粉饰）

1. **走门的 B 形态闭环无真机证据**（见 §4-1）：`notifications` 表只有 1 行，且那是探针（`kind=probe`）。
   **注意别把这条说成"通知从没弹过"** —— 通道真机弹过（探针 id 69/70，用户亲眼看到），
   但那是绕过门的探针，不算闭环。
2. **`bilibili` 采集器仍无真来源**：M5 的真机产出目前完全来自 `browser-history`（M5b，已跑真数据）；
   `collect.bilibili.enabled=false`，要接收藏夹 API 才会用上（用户已选历史记录那条路，这条不再是阻塞项）。
3. **53 条 open 待办全部未提示**：这是 `notify.enabled=false` 的直接后果，不是故障。
   构成：`bili-unintegrated` 30 / `fs-repeat` 19 / `session-stale` 4。其中 `fs-repeat` 那 19 条是
   **我开发这个项目本身的 churn**（例如「`info-hub/INTERFACE.md` 24 小时内改了 4 次」）⇒ 开通知前值得先想清楚
   要不要按类别设白名单（见 §4 末尾"未表态"那条）。
4. **window 采集很稀疏**（全库 5 条，约每 2–3 小时一条）：因为它只在**界面集合真的变了**时落库 ——
   这是设计（省噪音），但副作用是 `notify.require_ui_event=true` 这条门会让通知平均要等很久才评估一次。
   **开通知前建议先实测一轮节奏**（用 `backend=stdout` 观察）；用户 2026-10-16 已明确"不用加，本来就是事件触发的"
   ⇒ 这一项**按用户决定不动**，只作为开通知时的观察项。
5. **一次真数据才照出来的假待办**：浏览器历史刚接上时出了一条 `notebooks/recipes`（某仓库页标题里的斜杠词
   被当成仓库名）。已在提取层修掉（URL 是仓库页时只认 URL）+ 补验收；那条假待办用
   `hub-store todo-set … dropped` 标掉（留痕，不删行）。**教训**：合成夹具写不出这种标题，真数据必须跑一遍。
6. **控制台无鉴权**：只绑 `127.0.0.1` + 校验 Host/Origin，任何人只要能访问本机 loopback 就能改待办。
   本机单用户场景可接受，多用户/共享机不可。
7. **规模评估（PROJECT §10：16–25 人天）未被真正验证**：实际是连续几天多轮推进，没有工时记账 ⇒
   这个区间只能当"量级参考"，不能当"实际成本"。
8. **截屏需要一次人工同意**（M7，已完成）：`ScreenCast` 会话先弹一次「共享屏幕」（2026-10-16 11:41 用户点过），
   之后靠 `restore_token` 免重复授权。**实测更正**：规范说令牌单次使用，但本机 GNOME portal 复用时每次回**同一个**令牌
   （连续 4 次取帧后落盘令牌未变、一直可用）；实现按"不同才写回"+ `grab` 返回 `token_from_portal` 对账，两种行为都不怕。
   采集器**永远不会弹同意框**（没令牌就如实失败，记 `run:screenshot`）。
10. **第一张真机截图是变形的那张**（1280x1600，横向被压）：管道只限宽、videoscale 保留了源高度。
   已修（宽高都钉 + `add-borders`），那张图留在库里当反面证据，7 天后由 prune 收走（§35 bug 3）。
9. **通知门 3 的空闲判定曾长期算错**（M2 起就错，M7 联调才照出来）：真机 `(uint64 29866,)` 里 `64` 被当成高位
   吃进毫秒数 ⇒ 空闲被放大成 6429.866 秒 ⇒ 永远判"人不在"。已修（`IDLE_RE`）+ 回归 `verify_m2.sh` T15。
   **连带更正**：2026-10-16 那条 M2f 只读预览当时报的"空闲 642 秒"也是这个 bug 的产物（反推真实只有 2 秒，
   人其实在）；修好后重跑，真机空闲 433 秒 ⇒ 这次"人不在"才是真值。

11. **M8 全量历史把库撑大了**：180 天窗口 + 每条一条事件 ⇒ 9335 条 / 约 3 MB（库总共 16.8 MB）。
    留存 180 天与 `retention_days` 对齐，`prune` 到点会收；但 `since_days` 再放大就要先看盘。
    **标题只留前 120 字、网址原样**：这是分类与检索够用的最小面，不是"全文抓取"。
12. **M8 的"一条总结"是逐条的流水（时间·站点·用途·标题）**，不是语义摘要：**没有 LLM 参与**，
    所以它不会告诉你"你今天在为哪件事查资料"。要语义归纳得走归纳层（M4b/M4d，那层默认本地后端同样不调 LLM；
    接 LLM 后端的口子留着：`digest.command` 指 `bin/hub-digest-dsh --session <id>`）。
13. **分类认不出就落「其它」**（现在 4.5%）：规则表是手写枚举，新站点/新用途出现时不会自动学会；
    这是"描述层不猜"的代价，也是它的可靠性来源。
14. **同一网址只记最近一次访问**（`visit_id = sha1(url)`）：总访问次数保留在 `visits` 字段里，
    但"几点几分又看了一次"不单独成条 ⇒ 想做访问频次趋势要另加口径。

## 6. 下一步（按优先级）

1. **等用户**：§4-1 的六条条文逐字确认（这是唯一卡住"B 形态"的事）。
2. 用户确认后：落 `AGENTS.md` 例外条文 → 开 `notify.enabled` → 用 `backend=stdout` 观察一轮节奏 → 再切真通知。
3. 其余未表态项（通知类型白名单、留存期）按用户意思随时改，每项都不用改代码。
4. 新增源/规则若发现假待办，走 `hub-store todo-set <id> dropped`（留痕）而不是删行。
5. 截屏：**已开**（令牌在手、`enabled=true`、真机取帧跑通）。剩下可选的只有调参：
   `interval_s`（现 300 秒）/ `idle_max_s`（现 60 秒）/ `idle_min_s`（现 3 秒，打字保护门槛）/ `pause_wait_s`（现 20 秒，等停顿上限）/ `max_width`（现 1280）/ `keep_days`（现 7），都在 `hub.config.yaml` 一处。
6. 浏览历史：**已全量**（180 天、16 类用途、报表区块 `history`）。可选调参：
   `collect.browser-history.since_days`（现 180）/ `max_items`（现 20000）/ `title_chars`（现 120）、
   `report.history_days`（现 1，决定报表回看几天）、`history.py` 的 `DOMAIN_RULES`/`PATH_RULES`（加站点就加一行）。
## 7. 插件化收口记录（2026-11-14，S1–S18）

**这一轮交付的是什么**：本体收缩成「消息输入 + 投递台账」，其余搬成插件；投递第一次可以**确认（ack）/ 回复（reply）**，
并接了 DSH 的「问用户问题」（双弹、先答者生效）。契约：INTERFACE §10（插件）、§11（台账与三通道）、§12（DSH 双弹）、§13（layout）。

**机械证据（本地可复跑）**：

| 判据 | 命令 | 结果 |
|---|---|---|
| A16 全量回归 | `for f in tests/verify_*.sh tests/check_*.sh; do bash $f; done` | **34 套全 rc=0**（27 verify + 7 check） |
| A14 派生层插件化 | `bash tests/check_derived_plugins.sh` | **37/37** |
| A15 控制台插件化 | `bash tests/check_ui_plugin.sh` | **5/5** |
| A9b 无待办不弹 | `bash tests/check_desktop_popup.sh` | **15/15** |
| A17 layout 透传 | `bash tests/check_phone_mcp.sh` | **19/19** |
| A20 双弹两支 | `node plugins/channels/dsh-ask/selftest.mjs` | **12/12** |
| S14–S17 残留 | `grep -rn "from .activity\|from .vision\|from .calib\|from .features\|from .input\|from .camera\|from .screencast" src/` | 无输出 |

**commit 链**（每条都是「代码 + 契约 + 测试」一起落）：`200ff0b` S9 手机通道 · `fa3e890` S11 DSH 双弹 ·
`4a0f486` S13 口径外置 · `6e38039` S14 规则 · `cd10b99` S15 四个视图 · `c295f7f` S16 控制台 ·
`fa88381`+`bc726d7` S17 派生层（含目录缺失用例）。

**还差什么（都不是代码问题，等外部条件）**：
1. **A13 / A20 真机双弹**：需要把 `plugins/channels/dsh-ask` 装进 DSH profile（`cordis.patch.yml`，见 §12.7）并**重启 dsh web**。
   我不重启 —— 重启会杀掉正在跑这件事的会话，所以这一步归用户。
2. **A18 手机端渲染**：手机 App（`com.jxev.ai`）尚未实现 `deliver_show`，也没连 9444。
   真机探针返回 `{"ok":false,"status":"no_device"}`；此时通道按 A18 回落旧提醒卡片（仅标题+正文）并记 `degraded`。
3. ~~**wxstack 的弹窗直推**（m00699 那条）~~ —— **2026-11-18 已处置（用户认定微信推送渠道退役）**：`wx-outlet` 机械命中急迫词只回写微信自留地，不再弹电脑/手机；agent 决策推送不变。范围、理由与恢复方法见 [`docs/wechat-channel-retired.md`](docs/wechat-channel-retired.md)，代码备份 `/root/_backup/wx-outlet.py.bak-20261118-1848`。

**已知局限（这一轮新引入的）**：
- 控制台投递面板（`/api/deliver/*` 左栏）只列 `pending`，没有轮询，要刷新页面才看到新投递；JS 只做了 DOM 结构级测试，没跑无头浏览器。
- `hub-plugin list` 里「停用」表现为 `status=disabled` 的行，不是「消失」；想让它消失得删目录。
- dsh-ask 插件用 node 写、与 Python 本体两套依赖；它挂了不影响本体（DSH 只丢双弹，卡片照弹）。


## 8. 手机可回复通知（helper 后端 + DSH 真会话链路，2026-11-14）

用户要求（m01766）："dsh复制本体调试，结果给我，手机推送也自己调试"。结论：**两条链路都已真机跑通**；
唯一没验的是「浏览器问题卡片与手机谁先答」这一支，因为它必须由人在浏览器里点（执行者不能替用户点，也不重启真 dsh web）。

| 判据 | 命令 | 结果 |
|---|---|---|
| helper 投递（通知栏可回复） | `bash ~/hub-tasks/runs/core-deliver-plugins/evidence/s19_phone_helper_e2e.sh` | **6/6**（真机 <serial>：建行 → SHOW ok → hub_reply 通知 → 回执落盘 → 台账 replied） |
| handler 传输 | `bash tests/check_phone_helper.sh` | **12/12**（`am start -n .HubActivity --es op show --es payload_b64`） |
| 回执拉取 | `bash tests/check_phone_replies.sh` | **16/16**（run-as 读 all.jsonl + 行号游标 + 旧键兼容） |
| 选项按钮真机 | 手机通知栏点「做完了」 | all.jsonl：`{"id":"i-opt-tap-1","kind":"action","action":"done","src":"button"}`（apk-helper 真机点击） |
| DSH 真 agent 链路 | `DSH_HOME=~/dsh-debug/home dsh headless "调用 ask_user_question …"` | 日志 `[dsh-ask] request 1 agent= yes` → `phone pushed d-20261114175814-d90c96` → 手机回「手机答」→ 模型输出 **"你选了手机答。"** |

关键事实（排错用）：
- **HyperOS 丢 adb 广播**：`adb shell am broadcast` 被 Greezer 丢弃（logcat `process is not permitted to auto start`），
  投递改 `am start`；`helper_transport=broadcast` 只作换 ROM 兜底。
- **helper token 只在 payload 里认**：`ShowReceiver` 在 payload 非空时只从 JSON 取 token ⇒ `phone_remind.helper_token`
  必须等于 helper 设置页里的 token（服务端首次启动生成到 `$HUB_ROOT/phone_api.token`），否则 `SHOW rejected: bad token`。
- **payload 键是 helper 契约的 `id/body`**（不是 bridge 的 `delivery_id/text`），通道里做显式映射。
- **DSH 侧要真 agent 才会弹**：`dsh-api-remotes` 对无 agent 的瀑布请求直接 `next()` 跳过，agent-less 探针永远看不到卡片；
  `dsh headless` 默认不载 `@deepseek-ai/dsh-tool-ask-user`，需在 profile patch 里 insert 该插件，模型才有 `ask_user_question`。
- **回执双通道**：helper 直接 POST `/api/deliver/reply`（真机上居然通了，走的是可达的 hub 地址）+
  `bin/hub-phone-replies` 落盘拉取；后者是兜底、也是断网时的唯一路。
- 本机 `adb` 不在 PATH ⇒ `hub.config.yaml` 的 `phone_remind.adb` 要写绝对路径
  `adb`，否则拉取器报 `adb 起不来`。

**还差（等用户/外部）**：
1. 浏览器问题卡片那一支：需要人在 `http://127.0.0.1:3091` 的会话里看一次「卡片 + 手机同时出现、先答的关掉另一侧」。
2. helper APK 的选项按钮有一行是 `src=test`（apk-helper 的测试入口），真点的 `src=button` 已有一条；两者都在 all.jsonl。
3. 手机通知的正文长度/图标是 helper 侧固定值，本体的 `layout` 只透传不渲染（App 不支持时降级，见 §11.7b）。

