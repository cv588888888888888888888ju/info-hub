# info-hub（信息中枢）

把本机**正在发生的事**收进一个库里，加工成**待办清单**与**报表**，需要时用系统通知轻提一句，并给一个
**能手改、每次改动都留痕**的本地控制台。

- 契约（键名/接口/失败语义/数据表）：[INTERFACE.md](INTERFACE.md)
- 设计（场景→用法→嵌入、分期路线、规模评估、各期实施记录）：[PROJECT.md](PROJECT.md)
- 发布说明与已知限制：[RELEASE.md](RELEASE.md)
- 行为层草案（数据→行为特征→判定口径，**未实施**，见其 §10 待拍板五项）：[BEHAVIOR.md](BEHAVIOR.md)

> 一句话边界：**它只收集、只呈现、只在被允许的时机说话。** 不主动提醒、不替用户决定、不自动改知识库。

## 架构：本体只剩「输入 + 推送」（S1–S17，2026-11-14 定型）

**本体**只做两件事：把消息**收进来**（`events` 表 + spool）、把消息**推出去**（`deliveries` 台账 + 通道）。其余全是插件，元件都在 `plugins/`：

| 种类 `kind` | 目录 | 职责 | 现状 |
|---|---|---|---|
| `source` | `plugins/sources/*` | 一轮采集，产出事件 | 10 个：fs / clipboard / window / dsh-session / browser-history / bilibili / screenshot / phone-hub / phone-usage / study |
| `processor` | `plugins/processors/*` | 事件 → 待办 | rules |
| `view` | `plugins/views/*` | 报表 / 摘要 / 周报 / 看板 / 控制台 | report / digest / week / board / ui |
| `derived` | `plugins/derived/*` | 派生指标（活跃度、画面、校准、行为特征…） | activity / vision / calib / features / input / camera / screencast |
| `channel` | `plugins/channels/*` | 投递通道（电脑弹窗 / 手机 MCP / DSH 提问） | desktop-popup / phone-mcp / dsh-ask |

**四道缝**（同一形状：清单 → 解析 → 加载，失败三分；本体不认识任何具体实现）：

| 缝 | 文件 |
|---|---|
| 插件宿主（发现 `plugin.yaml`、校验、导入、隔离坏插件） | `src/infohub/plugin.py` |
| 来源注册表（`kind=source`） | `src/infohub/sources.py` |
| 视图缝（`kind=view`） | `src/infohub/views.py` |
| 派生缝（`kind=derived`） | `src/infohub/derived.py` |

**接口归一化**：所有来源共用一个签名与一个事件 schema ——

```python
def collect(config, store) -> list[dict]
# 事件 = {ts, source, kind, payload, fingerprint, privacy}（src/infohub/events.py 的 make_event）
```

一轮里**多个采集器同时跑**：每个来源把事件写进 spool（JSONL + `flock`，并发写安全），本轮结束统一入库（`INSERT OR IGNORE` 按 `fingerprint` 去重）。单个插件抛异常只把自己标成 `ok:false`，其它照跑、退出码仍是 0（INTERFACE §6）。

**接一个外部采集器**（不用改本体）：把目录挂进 `plugins.paths`，写两个文件即可 ——

```yaml
plugins:
  paths: ["/path/to/my-plugins"]
```
```yaml
# /path/to/my-plugins/weather/plugin.yaml
name: weather
kind: source
api: 1
entry: collector.py:collect
default_enabled: true
config_key: collect.weather
```
```python
# /path/to/my-plugins/weather/collector.py
def collect(config, store):
    from infohub.events import make_event
    return [make_event("weather", "daily", {"t": 21})]
```

`bin/hub-collect --source weather,fs --json` 会把两边的事件一起入库（实测：外部根下两个来源 + 一个故意抛异常的来源，一次运行 `ingested=3`、异常来源 `ok:false`、退出码 0）。

**开关两处**：`plugins.<name>.enabled=false` = 卸掉；`collect.<name>.enabled` = 这轮跑不跑（缺省取清单的 `default_enabled`，`--source` 点名时不看）。

> 已核对（2026-11-18）：`src/infohub/collectors/` 不存在，本体源码里没有任何具体采集器；`bili.py` / `browserhist.py` / `atspi.py` / `screencast.py` 只是取数后端，被插件 `import`。


## 现在跑着什么（2026-10-16 09:41 实测）

```
$ systemctl --user list-timers 'info-hub-*'
Fri 09:45 info-hub-derive.timer    Fri 09:45 info-hub-collect.timer
Fri 23:50 info-hub-report.timer    Sun 23:30 info-hub-digest.timer
Sun 23:40 info-hub-prune.timer     Sun 23:55 info-hub-week.timer
6 timers listed.

$ bash bin/hub-store stats
库: ~/.dsh/info-hub/hub.db  (16818176 字节)
事件: 27468  首=2026-10-02T23:34:29+08:00  末=2026-10-16T12:25:08+08:00
  browser-history 9335 / dsh-session 17845 / fs 267 / clipboard 8 / window 7 / screenshot 6
```

控制台 `http://127.0.0.1:8799/`（只绑 loopback）HTTP 200，`info-hub-ui.service` active。
隔夜复查（2026-10-16 08:45）：报表明明是自己跑过一轮的（`info-hub-report.timer` LAST=07:01:05）⇒
不是"装上了"，是"过了一夜还在跑"。

## 现在有哪些监测（全清单）

**采集器**（`hub-collect`，每 5 分钟一轮；每个源的失败只记 `last_error`，不影响整轮退出码）

| 源 | 看什么 | 关键口径 | 开关 |
|---|---|---|---|
| `dsh-session` | `~/.dsh/sessions` 里会话的事件流（最多 60 个会话 × 400 条） | 游标 = 行号 + mtime；`head` 前 200 字 | 开 |
| `fs` | 工作区文件改动 | 根 = `default-workspace`，最多 50 个文件；`.git/node_modules/*.log/*.db/*.zstd` 等不看 | 开 |
| `browser-history` | **Edge 的 `History`**（`Profile 1`）的**全部**浏览记录（M8 起） | **只读副本**读完即删；窗口 180 天（跟留存对齐）；每条只留**时间 + 网址 + 站点 + 用途 + 标题前 120 字**；同一网址只留最近访问；`privacy=sensitive`；用途分 16 类（认不出落「其它」） | 开（M8） |
| `clipboard` | 剪贴板文本（最多 2000 字） | `wl-paste -n`；连续失败 3 次冷却 900 s | 开 |
| `window` | 前台窗口标题（最多 40 帧 × 120 字） | 只在界面集合**真的变了**才落库（所以稀疏是设计） | 开 |
| `screenshot` | **屏上画面**（活跃时才取） | ScreenCast 取帧（**无闪光、无快门声**）：已获授权（2026-10-16 11:41 点过一次「共享屏幕」，之后不再弹）；键鼠活跃（mutter 空闲 < 60 秒）才取、不活跃完全不反应；最短间隔 300 秒；等比缩到 1280 宽 JPEG（16:10 ⇒ 1280x800）；`privacy=sensitive`；**留 7 天**（图删、元数据留） | **开**（已跑通，见下） |
| `bilibili` | bilibili 收藏导出通路（M5 老路） | 要显式接来源（命令或手动导出 JSON） | **关** —— 实际来源是上面的 `browser-history`，这条不再用 |

**规则**（`hub-rules derive`，每 15 分钟一轮；产出清单 + 每条都带证据链）

| 规则 | 什么时候出待办 | 阈值 |
|---|---|---|
| `fs-repeat` | 同一个文件 24 小时内被反复改动 | ≥ 3 次 |
| `session-stale` | 一个会话 48 小时没动静 | `stale_hours: 48` |
| `clipboard-domain-repeat` | 最近 20 条剪贴板里同一域名出现 ≥ 3 次 | `min_count: 3` |
| `bili-unintegrated` | 浏览器历史/bilibili 里提到的仓库在 KB（`~/knowledge_base`）查不到 | 窗口 14 天；KB 判定缓存 30 天；M8 起历史来源**先按 URL 判门**（非仓库页的标题不当仓库） |

**定时器**（`bin/hub-install` 装；通告见上表）

| 什么时候 | 干什么 |
|---|---|
| 每 5 分钟 | 采一轮；**若这轮真收进了新的界面事件，顺手评估一次通知**（触发面，M2f） |
| 每 15 分钟 | 规则跑一遍、刷清单 |
| 每天 23:50 | 出报表（`reports/hub-report.md`，人的文字与机器区块分离） |
| **周日 23:30** | **本周归纳**（`bin/hub-digest run --window-days 7`，默认本地后端） |
| 周日 23:40 | 按保留期清理老事件 **+ 删 7 天前的截屏图**（元数据不动） |
| 周日 23:55 | 出周报（引用当天 23:30 那份归纳） |
| 常驻 | 控制台 `127.0.0.1:8799` |

**没在跑的**：通知（`notify.enabled=false`，见下 —— **触发面已经接好了**，只差这一个开关）、看板写卡（`board.enabled=false`）、
浏览历史的**推送**（M8：分类与总结只进报表，按你的答复「卡片不推送」）。

**浏览历史（M8，2026-10-16 改口径）**：从"只挑仓库页/视频页"改成**全量**——每条历史留一条记录
（时间 + 网址 + 站点 + **用途** + 标题前 120 字，`privacy=sensitive`），并用一句总结汇总。真机已入库 **9335 条**：

```bash
bash bin/hub-history summary --days 1     # 一句话总结 + 主力站点
bash bin/hub-history summary --days 7 --json
bash bin/hub-history classify <url>       # 单条分类（调试）
```

真机最近 1 天：浏览 383 个页面（去重后）—— 视频/音频 235、学习/课程 65、招聘/求职 31、AI 服务 27；
主力站点 bilibili.com 233、hrbust.edu.cn 34、careersky.cn 31。用途分 16 类（本机服务/代码仓库/AI 服务/技术文档/博客/
社区论坛/视频音频/小说阅读/学习课程/游戏/招聘求职/购物采购/网盘文件/资讯新闻/账号邮箱/搜索），认不出落「其它」
（真机 4.5%）。报表里对应「浏览历史」区块（`hub:auto:begin history`），保留 180 天（跟事件留存一致）。

**截屏（M7）**：**已开、已跑通**（`tests/verify_m7.sh` 24/24）。
2026-11-13 起加「打字保护」：空闲 < `idle_min_s`（3 秒）视为正在敲键 ⇒ 等一次停顿再取，等不到这轮不取（`last_skip=typing`）。
真机证据 2026-10-16 11:49 —— 事件 17670、图 **1280x800**、421939 字节、`idle_s=5.195`、取帧全程**没有任何对话框弹出**。
授权：2026-10-16 11:41 点过一次「共享屏幕」（`persist_mode=2`），拿到持久令牌后**不再弹**；
采集器**永远不会弹同意框**（没令牌就记失败）。命令：

```bash
bash bin/hub-screencast consent   # 点一次「共享」；成功后令牌存 state/screencast.json
bash bin/hub-screencast status    # 看现状
bash bin/hub-screencast grab      # 立刻取一帧（自证：无声、无闪光）
```

`collect.screenshot.enabled` 现为 **true**；不活跃时**一张都不会取**（活跃门在采集器里）。取帧走 ScreenCast 视频流（不是截图），
所以没有闪光动画、没有快门声；只取整屏、不取窗口列表内容。

> 「弱提醒」落在机制上：**定时的只是采集，推送永远挂在"收到新的界面事件"上**。
> 也就是说，你人不在（空闲 ≥ 60 秒）或没切换界面时，它不会为了"到点了"而弹 —— 契约见 [INTERFACE.md §4.6](INTERFACE.md) 的「触发面」。

## 快速开始（解包即用）

依赖只有 Python 3.11+ 与 PyYAML（不是 pip 包，是真的没别的依赖）：

```bash
# 从 release 的 info-hub-<版本>.tar.gz 解包，或直接 git clone
cd info-hub-*
python3 -m pip install --user pyyaml        # 唯一外部依赖

cp hub.config.example.yaml hub.config.yaml  # 然后按需改 root/监听地址
# 库不用手动建：首次跑任何 bin/hub-* 都会按配置里的 root 自动建
bin/hub-collect --once                      # 收一轮
bin/hub-rules derive                        # 出待办
bin/hub-report                              # 出报表（$HUB_ROOT/reports/）
bin/hub-ui                                  # 控制台 http://127.0.0.1:8799/
```

手机通道（可选）：`bin/hub-phone-api` 起手机主页接口，**首次启动自动生成 token** 到
`$HUB_ROOT/phone_api.token`（0600）；手机装 release 里的 `hubreplier-<版本>.apk`，
在 App 设置页填「服务器地址 + token」两栏即可（不用 adb，App 不自动配置任何东西）。

插件可以脱离本体单独替换：release 里另有 `info-hub-collectors-<版本>.tar.gz`（采集器）
与 `info-hub-channels-<版本>.tar.gz`（投递通道），解到 `plugins/sources/`、`plugins/channels/` 下即生效。

## 装（一次性）

```bash
cd <工作区>/info-hub
bash bin/hub-install --dry-run     # 先看它要干什么
bash bin/hub-install               # 6 个定时器 + 控制台（不含通知）
bash bin/hub-install --with-notify # 连通知一起装（**通知默认关，见下**）
```

装完：`systemctl --user list-timers 'info-hub-*'` 应见 6 个 NEXT 非空；
`journalctl --user -u info-hub-collect -n 20` 每轮 `Starting` 且退出 0。

**不需要装也能手动跑**：`bin/hub-collect --once` → `bin/hub-rules derive` → `bin/hub-report` 各跑一遍即可。

## 日常用

| 想干什么 | 命令 |
|---|---|
| 立刻收一轮 | `bin/hub-collect --once`（只收某个源：`--source browser-history`） |
| 事件里有什么 | `bash bin/hub-store events --limit 20` / `stats` |
| 规则跑一遍出待办 | `bin/hub-rules derive`（`--json` 给机器看） |
| 这条待办凭什么 | `bash bin/hub-store todo <id>` ← **证据链**（见下） |
| 改待办状态 | 控制台点按钮，或 `bash bin/hub-store todo-set <id> done` |
| 谁改过什么 | 控制台改动记录 / `bash bin/hub-store todo-revisions` |
| 出报表 | `bin/hub-report`（→ `$HUB_ROOT/reports/hub-report.md`） |
| 归纳（本周发生了什么） | `bin/hub-digest run --window-days 7`（同一份输入第二次跑会 `reused`，不重复算） |
| 周报 | `bin/hub-week`（→ `reports/hub-week-<年>-W<周>.md`） |
| 控制台 | `bin/hub-ui`（→ <http://127.0.0.1:8799/>） |
| 今天几点开始干活 / 多久 / 多专注 | `bin/hub-activity day` |
| 画面到底是哪一类（代码/文档/聊天/视频/agent…） | `bin/hub-vision run` → `bin/hub-vision labels` / `stats`（派生层，只读已存截屏） |
| 手机屏幕今天用了多久 / 最常用哪个 app | `bin/hub-collect --source phone-usage --once` → `bin/hub-features show`（第一行就是；需插 USB 线） |
| 这段是在干活还是在玩（粗判） | `bin/hub-vision vlm` → `bin/hub-vision act`（本地 2B 小模型，9 秒/张） |
| 手上在动没有（键盘鼠标计数） | `bin/hub-input sample` / `stats`（每 60 秒一条，只数不读内容） |
| 人在不在座、脸朝不朝屏幕 | 跟活动采样一起（`bin/hub-activity sample`），三档：不在座 / 在座 / 看屏幕 |
| 记一次「刚才专注/一般/走神」 | `bin/hub-calib rate 1`（1 专注 / 2 一般 / 3 走神） |
| 三路特征谁真有判别力 | `bin/hub-calib report --days 2`（AUC，≥0.65 才算有） |
| **一小时的「人」的指标（转译层成品）** | `bin/hub-features build` / `show`（在场/看屏/切换/按键/节奏/停顿/手机/类别/专注） |
| 体检 | `bash bin/hub-store doctor` / `bin/hub-* --selftest` |

全局参数写在子命令**之前**：`bin/hub-rules --config X --json derive`。
完整 CLI 表见 [INTERFACE.md §2](INTERFACE.md)。

### 证据链：每条待办都能摊开

待办的 `source_event_ids` 不是自述，是**能逐条回查 `events` 表的事实**：

```
$ bash bin/hub-store todo t-2026-10-15-808713
t-2026-10-15-808713  [open]  「info-hub/src/infohub/store.py」24 小时内改了 4 次
  为什么: 同一文件反复改动，像是没收尾的活  置信度: 0.6
  依据 8 条事件：
    #7748  2026-10-15T16:37:33  fs/fs.change  {"path":"info-hub/src/infohub/store.py",…}
    #11061 2026-10-15T18:19:23  fs/fs.change  {"path":"info-hub/src/infohub/store.py",…}
    …
  改动记录 0 条：

$ bash bin/hub-store todo t-nope; echo "退出码=$?"      # 没有这条待办: t-nope / 退出码=1
```

- 三个出口共用一份视图：控制台「依据」按钮（`GET /api/todo/detail?id=`）、CLI `hub-store todo <id>`、通知正文的「依据 N 条事件」。
- 查不到的 id 会**如实标 `missing`**（`prune` 清过老事件就必然出现）—— 不静默丢、不假装有依据。
- 摊不开的待办**不许变成通知**：这是通知的门 8「无依据不发」。契约见 [INTERFACE.md §5.8](INTERFACE.md)。

## 默认关着的一项（**等用户拍板**）+ 两项的现状

| 键 | 现在 | 为什么 | 卡在哪 |
|---|---|---|---|
| `notify.enabled` | `false` | 全局铁律「不做主动提醒」的**例外条文**得先经用户逐字确认 | [PROJECT.md §13](PROJECT.md) 六条草案待确认 |
| `collect.bilibili.enabled` | `false` | 要显式接来源（命令或手动导出） | 用户已定按**账号历史**取数 ⇒ 实际走的是 `browser-history`（已开） |
| `digest.enabled` | **`true`（每周）** | 已按用户要求开成每周一次，后端 = **本地确定性后端**（离线、零 token、同输入同输出） | 想换回 LLM：`digest.command` 改成 `bin/hub-digest-dsh --session <id>` |

另有 `board.enabled` 默认 `false`（用户已定：**不写看板**）。**改任何一项都不改代码**，只动 `hub.config.yaml`
（唯一数值源；键语义见 [INTERFACE.md §3](INTERFACE.md)）。

**通知关着 ≠ 通知坏的**：通道本身在真机弹过（用户亲眼看到过探针 id 69/70），走门那条闭环只是还没在真机跑过。
想随时自证通道通不通（**不依赖上面的开关**、不碰任何待办、台账另记 `kind='probe'`）：

```bash
bash bin/hub-notify test            # 真弹一条探针：「自检：通知通道正常（这条不是待办提示，不需要处理）」
bash bin/hub-notify test --dry-run  # 只回报会发什么：不弹、不记账
```

契约见 [INTERFACE.md §4.6](INTERFACE.md) 的「自检探针」小节。

## 投递台：确认 / 回复 / 推手机 / DSH 双弹

投递不再各端各记一本账：每次投递在 `deliveries` 表落一行（`channel` = `computer` / `phone` / `dsh`），
确认与回复都回写到同一行。`ack` 只表示「看见了」（不改待办状态），`reply` 原文留档。契约见 [INTERFACE.md §11](INTERFACE.md)。

```bash
bash bin/hub-deliver add --channel computer --title "…" --body "…"   # 建一条待确认投递（只建行，不弹）
bash bin/hub-deliver list --json                                    # 看台账与未读
bash bin/hub-deliver ack <id>                                       # 确认（幂等，不改待办）
bash bin/hub-deliver reply <id> --text "收到，明天做"                 # 回复（空文本 exit 1）
```

三个通道都是插件（`plugins/channels/`），停用只需改配置，本体照跑：

| 通道 | 插件 | 做什么 |
|---|---|---|
| `computer` | `desktop-popup` | gdbus 通知 + 按钮结算；**没有真实待办（`todo_id` 非 open/doing）不弹** |
| `phone` | `phone-mcp` | 调手机 MCP 的 `hub_deliver`/`hub_dismiss`；layout v1 整份 JSON 透传，失败退避重推 |
| `dsh` | `dsh-ask` | hook DSH 的 `ask_user_question`：DSH 卡片与手机同时弹，先答者生效、另一侧收回 |

控制台左栏有投递面板（确认 / 回复），手机端界面由 `layout` JSON 自定义（[INTERFACE.md §13](INTERFACE.md)）。
「重要/紧急」判定口径只在行为层（`BEHAVIOR.md` + `plugins/processors/rules/`），改它即改口径，不动代码。

## 验收（每期一套，机械可判）

```bash
for f in tests/verify_*.sh tests/check_*.sh; do echo "== $f"; bash "$f" || break; done
```

27 套 verify（m0 14 / m1 11 / m2 16 / m2b 16 / m2c 11 / m2d 11 / m2f 9 / m3 16 / m3b 9 / m3c 9 / m4a 8 / m4b 9 / m4d 12 /
m5 12 / m5b 17 / m7 24 / m8 13 / m10 18 / m11 16 / m12 13 / m13 15 / m14 13 / m15 51 / m16 26 / m17 27 / m18 13 / week 9）
+ 7 套 check（deliver_http 48 / desktop_popup 15 / ui_deliver 40 / agent_md 9 / phone_mcp 19 / ui_plugin 5 / derived_plugins 37）。
2026-11-14 全量 34 套 rc=0。
每套自己 `mktemp -d` 建临时库与临时配置（不碰真库、不弹真窗），末尾 `[ "$FAIL" -eq 0 ] || exit 1`。

## 落位

| 东西 | 在哪 |
|---|---|
| 代码 / 契约 / 验收 | 本目录（git 仓库里的 `info-hub/`） |
| 插件（采集器 / 加工 / 视图 / 派生 / 通道） | `plugins/**`（各自带 `plugin.yaml`；契约 [INTERFACE.md §10](INTERFACE.md)） |
| 库 / 报表 / 状态 / 日志 | `$HUB_ROOT`，默认 `~/.dsh/info-hub/` |

覆盖口径：`HUB_CONFIG` 指配置文件、`HUB_ROOT` 指数据目录（[INTERFACE.md §7](INTERFACE.md)）。

## 隐私

只收「本机在发生什么」，且**入库前脱敏**（`privacy.py`）：剪贴板正文截断、窗口标题限长、
不碰密码类输入；报表、归纳、通知都过同一道脱敏。浏览器历史单列为 `privacy=sensitive`，
且**只取 14 天窗口内的仓库页/视频页**（不整库搬走）。
当前配置下**数据不出本机**：归纳后端是本地命令 `bin/hub-digest-local`（离线、零 token）；
将来若把 `digest.command` 换成 LLM 那条（`bin/hub-digest-dsh`），送出去的只是那份**已脱敏**的提示词。
保留期 `retention_days`，过期由 `info-hub-prune.timer` 清。
**截屏单列**：图只在本机 `~/.dsh/info-hub/screens/`（1280 宽 JPEG），元数据一条 `privacy=sensitive` 事件；
7 天后图被 `hub-screencast prune` 删掉、只留元数据；不上传任何地方。
