# QQ 来源插件 · 独立验收报告

> 2026-11-18 · 验收者：qq-verify（独立于实现者 task-5，只按判据复跑，不看实现者自述）
> 被测：`info-hub/plugins/sources/qq/{plugin.yaml,collector.py}` · `info-hub/tests/check_qq_source.sh`
> 纪律：全程只读被测文件；边界/去重用工件全部落在 `/tmp/qqverify-*` 临时根，不碰真库、不改实现。

## 0 结论

| 判据 | 结果 |
|---|---|
| 1 主脚本连跑两次 | **PASS**（两次均 41 PASS / 0 FAIL / rc=0；第三次复跑同样绿） |
| 2a remote_db 不存在 | **PASS** |
| 2b 表缺列 | **FAIL**（rc/ok/fail_streak 达标；**错误信息不点明原因**） |
| 2c ssh_host 不存在 | **PASS** |
| 3 去重与游标（删 collector_state 后重跑） | **PASS**（ingested=0、skipped_duplicate=3） |
| 4 脱敏（<bridge-host> / token） | **PASS**（仅 2 处内网地址，0 token；见 §4 点名） |
| 5 只读性（真库 + 远端库） | **PASS**（脚本对真库零写入；窗口内本地 mtime 变化已定位到常驻 timer，见 §5） |

**总判定：FAIL（单点阻塞）。** 采集、去重、只读、脱敏全部通过；失败语义的「可读原因」不达标 —— 见 §7-1。

## 1 主验收脚本连跑两次

```
cd info-hub && bash tests/check_qq_source.sh
```

| 轮次 | 退出码 | PASS | FAIL | 末行 |
|---|---|---|---|---|
| RUN1（18:47 起） | 0 | 41 | 0 | `SUMMARY {"checks":41,"failed":0}` |
| RUN2 | 0 | 41 | 0 | `SUMMARY {"checks":41,"failed":0}` |
| RUN3（§5 受控用） | 0 | 41 | 0 | `SUMMARY {"checks":41,"failed":0}` |

覆盖：T0 默认关 / T1 首轮 3 条 + 契约字段 / T2 id 游标 / T3 游标回退 + 改正文 / T4 丢游标指纹兜底 / T5 缺列 / T6 不可达 / T7 超时 / T8 恢复。
判定 **PASS**（脚本自身可复跑且隔离：每次 `HUB_ROOT`=`/tmp/infohub-qq-*`，跑完即删）。

## 2 边界（自造工件，不改被测文件）

公共前置：复制真 `hub.config.yaml`，只覆写 `plugins.qq.enabled=true`、`collect.qq.enabled=true` 与 `collect.qq.{ssh_host,remote_db,timeout_s}`；`ssh_host` 置空 ⇒ 同一远端命令在本机跑。
调用：`HUB_ROOT=<temp> HUB_CONFIG=<temp>.yaml info-hub/bin/hub-collect --source qq --json --quiet`

### 2.1 remote_db 指向不存在的路径

期望：rc=0；`sources.qq.ok=false`；`collector_state.cursor.fail_streak=1`；错误可读。

实际（remote_db=`/tmp/qqverify-nEJ2JL/does-not-exist.db`）：

```
RC=0
sources.qq.ok=false
STATE={"fail_streak": 1, "last_id": null, "seen_keys": 0, "last_error": "读取 QQ 中继库失败（rc=1）: ... sqlite3.OperationalError: unable to open database file"}
```

判定 **PASS**（原因点明：cannot open database）。

### 2.2 remote_db 指向缺列的表（自建临时 sqlite）

期望：rc=0；ok=false；fail_streak=1；**错误信息点明失败原因**。
工件三个变体：a) `n` 缺 `subtext`（其余列齐、有 1 行）；b) `n` 缺 `key`；c) remote_db 是存在但非 sqlite 的文件。

实际：三个变体**全部** rc=0 / ok=false / fail_streak=1 ✅，但错误串一模一样地只截到远端 traceback 的头部：

```
读取 QQ 中继库失败（rc=1）: Traceback (most recent call last):   File "<string>", line 8, in <module>
    rows = [dict(zip(cols, r)) for r in con.execute(sql, ("com.tencent.mobileqq", after, limit))]
```

真正原因在 traceback 末行，被截掉了（同一条命令手动跑，stderr 末行是 `sqlite3.OperationalError: no such column: subtext`）。
定位：`collector.py:100` `err = proc.stderr.decode(...)[:200]` —— 取**前** 200 字符；远端程序是长 traceback，异常名落在尾部。
判定 **FAIL**（判据「错误信息点明失败原因」；rc/ok/fail_streak 三项仍满足）。

### 2.3 ssh_host 指向不存在的主机

期望：rc=0；ok=false；错误非空。

实际（ssh_host=`root@qqverify-不存在的主机.invalid`）：

```
RC=0
sources.qq.ok=false
STATE={"fail_streak": 1, "last_error": "读取 QQ 中继库失败（rc=255）: ssh: Could not resolve hostname qqverify-...invalid: Name or service not known"}
```

判定 **PASS**（原因点明：DNS 解析失败）。

## 3 去重与游标

工件：临时 sqlite `n` 表 4 行 = 3 条 QQ（id 1/2/4，key k1/k2/k3）+ 1 条非 QQ（id 3，com.tencent.mm）；故意不建 `UNIQUE(key)`。
流程：跑一轮 → 删 `collector_state WHERE key='qq'`（rowcount=1）→ 再跑一轮。

| 轮次 | rc | emitted | ingested | skipped_duplicate | events(source=qq) |
|---|---|---|---|---|---|
| RUN1 | 0 | 3 | 3 | 0 | 3 |
| RUN2（已删游标） | 0 | 3 | **0** | **3** | **3**（未增） |

RUN2 游标被重建为 `last_id=4, seen_keys=3, fail_streak=0`。
判定 **PASS**（未重复入库；游标丢失时由库级事件指纹兜底）。

## 4 脱敏检查

```
grep -rn "10\.0\.0\.9\|token" info-hub/plugins/sources/qq/ info-hub/tests/check_qq_source.sh
```

命中 2 处，均在被测采集器里：

- `info-hub/plugins/sources/qq/collector.py:3` — docstring：数据源 = `<bridge-host>` 的 `/opt/qq-relay/qq_ingest.db`
- `info-hub/plugins/sources/qq/collector.py:73` — 默认值 `ssh_host="root@<bridge-host>"`

`token` 0 命中；`tests/check_qq_source.sh` 0 命中。
**点名**：仓库里出现的真实地址是 **内网 WireGuard <bridge-host>（私有网段，公网不可达）**，不是公网 IP；与 `INTERFACE.md:313` 的契约默认值、`hub.config.yaml:108` 的 phone-hub 写法一致 ⇒ 判为可接受的文档化默认值。
副作用：grep 报 `__pycache__/collector.cpython-314.pyc` 二进制命中；已确认 `.gitignore:32` 忽略该目录，不入库。
判定 **PASS**。

## 5 只读性（测试前后 mtime / sha256）

### 5.1 本地真库 `~/.dsh/info-hub/hub.db`

| 时刻 | size | mtime | sha256 |
|---|---|---|---|
| 18:47:17（基线） | 259,883,008 | 18:46:03.106 | `8d161d78…6d7906` |
| 18:51:24（复跑后） | 260,284,416 | 18:50:42.977 | `c4301958…c8d7b3` |
| 18:52:40（受控复跑后） | 260,284,416 | 18:50:42.977 | `c4301958…c8d7b3`（不变） |

**变化发生了，但不是验收脚本写的**，证据三条：

1. `journalctl --user -u info-hub-collect.service`：18:50:19 起跑、18:50:43 结束，`ingested=669 duplicate=0 backlog=669`；`info-hub-collect.timer` 为 5 分钟常驻（18:50:19 上一轮、18:55:07 下一轮）。变化时刻与该轮吻合。
2. 受控实验：第三次跑 `check_qq_source.sh` 前/后 3 秒内取样，sha256 与 mtime **逐字节一致**（18:52:40，仍未变）。
3. 真库里 `source='qq'` 事件 0 条；验收 fixture 的 `key IN ('k1','k2','k3')` 命中 **0**（无泄漏）。

### 5.2 远端 `/opt/qq-relay/qq_ingest.db`

| 时刻 | size | mtime | sha256 |
|---|---|---|---|
| 18:47:17（基线，<bridge-host>） | 28,672 | 2026-10-10 22:06:39 | `a9bf50c9…668817` |
| 18:51:24（复跑后，<bridge-host>） | 28,672 | 2026-10-10 22:06:39 | `a9bf50c9…668817` |
| 18:53:32（默认配置 e2e 后再取） | 28,672 | 2026-10-10 22:06:39 | `a9bf50c9…668817` |

<wg-server-ip> 上的同名文件 sha256 与上面相同（也是 28,672 B）；两机读数一致。
判定 **PASS**（远端库零写入；默认配置 e2e 一轮走通：rc=0、ok=true、emitted=6、last_id=27、fail_streak=0）。

## 6 仓库状态（辅助）

`git status --porcelain`：

```
?? info-hub/plugins/sources/qq/
?? info-hub/tests/check_qq_source.sh
```

被测交付物尚未被 git 跟踪；`info-hub/docs/qq-source-verify.md`（本文件）为本次新建。

## 7 发现的问题（按严重度，均未自行修改被测文件）

1. **[中 · 判据不满足] 缺列 / 非 sqlite 的错误信息被截断，看不出原因。**
   根因 `info-hub/plugins/sources/qq/collector.py:100`：`proc.stderr…[:200]` 取 stderr 前 200 字符，而远端 `python3 -c` 失败时是长 traceback，`OperationalError: no such column: …` 在末行。建议改取末行（或 `[-200:]`、或剥掉 `Traceback` 头），一次改一行。
2. **[中 · 环境] 上游数据源已停更 39 天。** `qq_ingest.db` 25 行、`max_id=27`、`max_ts=1791641102911`（=2026-10-10 22:05）；`qq-ingest.service` 自 2026-10-26 起 active，但 `/opt/qq-relay/keepalive.log` 每 15 分钟 `心跳失败 … rc=1 ret=-2 errmsg=prepare failed`（`idle=317h`），`ingest.log` 停在 2026-10-26 12:10。插件本身能读（§5.2 e2e 读到 6 条 QQ），但装机后不会有新消息 —— 与 `docs/qq-source-recon.md` §0 的「手机侧上传器已不存在」一致。
3. **[中 · 交付] 被测文件未纳入 git**（§6）。验收通过后需 `git add`。
4. **[低 · 去重边界] 丢游标 + 上游改正文 ⇒ 会重复入库。** 附加探针：初次 3 条；删 `collector_state` 并改 k1 正文后再跑 ⇒ `emitted=3, ingested=1, skipped_duplicate=2`，events 3→4。即 `collector.py:8` docstring 的「同一 key 的重放…即使正文被上游改写」只在 `seen_keys` 仍在时成立；游标整行丢失后只剩 `events` 指纹去重。另 `seen_keys_max` 默认 500（`collector.py:78,140`），突发 >500 新 key 会挤掉旧 key，同一残留风险。
5. **[低 · 脚本强度] 主脚本对 T5 只断言固定前缀。** `tests/check_qq_source.sh:199` 仅检查 `"读取 QQ 中继库失败"`，所以 41/41 绿 ≠ §2.2 判据满足。建议补断言 `no such column`（或至少断言错误里含列名）。
6. **[低 · 数据卫生] 远端真实库混有测试行**（`key=upload-test-1/2`）；默认配置 e2e 的 6 条里有 2 条是它们。建议清库或让采集器跳过 `upload-test-*`（若确认是遗留工件）。
7. **[信息] ssh 认证走用户默认密钥/agent。** `collector.py:87` 只传 `BatchMode/StrictHostKeyChecking/ConnectTimeout`，没有 `ssh_key`/`ssh_proxy` 配置项（phone-hub 有）；本机默认身份下 `root@<bridge-host>` 可用（§5.2 已验证），但换机/换用户需自己在 `~/.ssh/config` 配 `IdentityFile`。`INTERFACE.md:313` 未写明这一点。

## 8 复现路径

- 临时根保留：`/tmp/qqverify-nEJ2JL`（边界+去重探针）、`/tmp/qqverify2-Jwdnvf`（缺列三变体）、`/tmp/qq-e2e-7t7grA`（默认配置 e2e，含临时 hub.db 与配置副本）
- 主脚本日志：`/tmp/qq_run_{1,2,3}.log`
- 驱动脚本：`/tmp/qqverify_harness.sh`、`/tmp/qqverify_b2.sh`（均在仓库外）


---

## 9 Lead 处置（2026-11-18 18:59，收到本报告后）

- **[已修 · 单点阻塞 §7-1]** `plugins/sources/qq/collector.py` 的 stderr 取法由「前 200 字符」改为「去掉空行后末 3 行拼接、再取末 200 字符」（注释写明原因：traceback 头无信息、真异常在末行）。
- **[已修 · 低 §7-5]** `tests/check_qq_source.sh` 的 T5 增加断言 `no such column`（现在缺列失败能被脚本自己抓住，不再靠人工读报告）。
- 复跑：`bash tests/check_qq_source.sh` ⇒ `SUMMARY {"checks":42,"failed":0}`、退出码 0（比原 41 项多出的 1 项即上面那条断言）。
- 改前备份：`~/backups/qq-verify-fix-20261118-185850/{collector.py,check_qq_source.sh}`。
- INTERFACE.md 同步：§8 QQ 验收改 42 项并写明「错误信息取自 traceback 末行」；§10 开关优先级按实现校正（`default_enabled: false` 的来源必须显式 `plugins.<name>.enabled: true` 才会被注册）。
- **仍开放（未处置，按原报告保留）**：§7-2 上游已停更 39 天（B 路径需用户确认账号与风控）；§7-3 交付物未入库（`git status` 仍 `?? plugins/sources/qq/`、`?? tests/check_qq_source.sh`、`?? docs/`）；§7-4 去重边界（丢游标 + 上游改正文 / `seen_keys_max` 溢出）；§7-6 远端测试行 `upload-test-1/2`；§7-7 ssh 认证默认走用户密钥。

