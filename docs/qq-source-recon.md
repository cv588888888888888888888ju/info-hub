# QQ 消息采集：现状与三条路径（只读调研）

> 2026-11-18 · 范围：本机 Fedora / 家庭机 <bridge-host> / ECS <wg-server-ip> / 手机 jxev-<model>
> 方法：ssh + systemd/ss/ps、远端 sqlite 只读查询、手机 MCP bridge(<bridge-host>:9500) 的 adb_shell。全程只读，未改任何生产文件。

## 0 结论
1. **A（手机通知流）服务端还在跑，手机侧上传器已不存在**：库最后一条 10-10 22:05，之后 5 周 0 条上传（§1）。
2. QQ 通知能拿到「会话名(+未读数) / 发送人前缀 / 最后一条正文 / 通知 key / ts」；拿不到群私聊标记与全量历史（§2）。
3. 两机 + 手机均**没有** NapCat/Lagrange/协议机器人（§3）；唯一在线客户端是本机桌面 QQ。
4. 本机 linuxqq 3.2.34 正在运行，消息库 nt_msg.db 40MB，但**加密**（`SQLite header 3`，sqlite 打不开）。
5. 推荐：**B（NapCat + 独立小号）作为正式路径**；A2（MCP 桥接拉通知）只作 1 天验证；C 需逆向取密钥，暂不推荐（§4）。

## 1 手机通知 → <bridge-host>：链路与当前状态
- 接收端在跑：`systemctl list-units --type=service | grep -Ei "qq|wx"` → `qq-ingest.service loaded active running`；`ss -lntp | grep 8123` → `<bridge-host>:8123 python3 pid=423298`。
- 契约（qq_ingest.py 文件头）：手机 App POST `http://<host>:8123/ingest`，头 `X-Relay-Token`，
  体 `{"device":"…","items":[{ts,pkg,app,title,text,subtext,key}]}`；落 `n(id,ts,pkg,app,title,text,subtext,key,received_at)`，`UNIQUE(key)` 幂等。
- token 来源：服务端 `/opt/qq-relay/ingest.json` 的 `token`（600；空则拒绝启动）；手机侧配置文件写同一值，不一致时 App 日志 `QqRelay HTTP 401`（`qq-wechat-relay/README.md:78,137`）。
- 实测上传已停：`cat /opt/qq-relay/ingest.log`（<wg-server-ip>）最后一条是
  `[2026-10-10 22:06:39] 收到上传 device='<model>' 新增=1 去重=7`，其后只有 10-25/10-26 两行“监听”，无上传。
- 库现状：`select count(*),min(ts),max(ts) from n` → `(25, 1791631099000, 1791641102911)`（10-10 19:18→22:05）；`pkg='com.tencent.mobileqq'` 6 条。
- ECS 侧已停：`ss -lntp | grep -E '8123|9444'` → 只剩 `<wg-server-ip>:9444 socat`（转发 <bridge-host>:9444 的 MCP 手机通道，非通知）；`systemctl status qq-relay` → `failed … since 2026-10-25`。
- 手机侧：`ls -la /sdcard/lifestate/` → 只有 `relay.json(10-10 23:10)`、`schedule.json`、`app.log`、`notify.db(10-24)`；**没有** `qqrelay.json` / `qq_queue.jsonl`（上传开关与队列）。
- `cat /sdcard/lifestate/relay.json`：`enabled:true`、`send_seconds:1800`、内嵌 `ilink.base_url/token`（已隐去）——手机端自己做摘要并发微信，**不是**上传服务端。
- 结论（证据指向）：10-10 22:06 后手机不再上传；同日 23:10 写入的 relay.json 内嵌 iLink 凭据、1800s 周期 ⇒ 摘要改由手机端直发微信。A 路径是**空管道**（接收端空转，5.5 周 0 新行）。

## 2 QQ 通知里到底能拿到什么（6 条样本，正文已做匿名化）
只读查询 `qq_ingest.db`（脚本见 §6）；会话名/发送人/正文在公开版里换成示例值，结构逐字保留：
| ts(10-10) | title | text |
|---|---|---|
| 19:18 | 示例测试群 | 甲: @某人 这是形态2上传测试 |
| 19:18 | 示例测试群 | 乙: 示例闲聊内容 |
| 19:36 | （私聊会话） | 示例消息 |
| 20:02 | 示例群A | 某人: 示例消息 |
| 20:02 | （null） | （null）← 聚合通知 `g:Aggregate_AlertingSection` |
| 22:05 | 示例群A(7条新消息) | 某人: 示例消息 |

- 字段统计：`app` 全是 `QQ`；`subtext` 25 行全 null；`max(length(title)),max(length(text))` = `(204,97)`，QQ 样本 text ≤ 25。
- 群聊 = title 是群名(+未读数)，text 是 `发送人: 正文`；私聊样本（19:36）text 无前缀 ⇒ **没有群/私聊字段**，只能按 `": "` 前缀启发式猜。
- 实时复核（手机 `dumpsys notification --noredact`）：`android.title=示例群B-群公告(8条新消息)`、`android.text=某人: …`、`tickerText=群名(8条新消息): 某人: [图片] …`、`key=0|com.tencent.mobileqq|513|null|10323`；**无 android.bigText**。
- 固有损失：同会话多条只留「最后一条 + 未读数」；图片/文件折叠成 `[图片]`；同 key 的内容更新被 `UNIQUE(key)` 丢掉；手机离线/通知被清后的消息不可补。

## 3 是否已有可收发的 QQ 机器人/客户端
- <bridge-host>：`which napcat lagrange qq ntqq` → 空；`ls /opt` 无 napcat/Lagrange；`ps -eo cmd | grep -Ei 'qq|napcat|lagrange'` → 只有 qq_ingest.py / wx_gateway.py；`docker,podman` 未安装；node v22.23.2、Xvfb、xdotool 已装。
- 本机：`command -v napcat lagrange onebot` → 空；`rpm -qa | grep -i qq` → `linuxqq-3.2.34_53644-1.x86_64`；`ps` → `/opt/QQ/qq` 在跑（Electron，user-data-dir=~/.config/QQ）。
- 手机：`pm list packages | grep -iE 'qq'` → 只有 `com.tencent.mobileqq`，无机器人 App。
- 结论：**没有**任何协议端/机器人；唯一在线客户端是本机桌面 QQ。

## 4 三条路径对比
| 维度 | A 手机通知流 | B 协议机器人（NapCat/Lagrange） | C 桌面客户端采集 |
|---|---|---|---|
| 现成度 | 接收端在跑，上传端没了 | 未安装 | 已装且已登录 |
| 能拿到 | 会话名+未读数、最后一条正文、发送人前缀、ts、key | 全量群/私聊消息、历史、图片/文件元数据、可发送 | 本机全量历史（解密后）、发送需另做 |
| 缺什么 | 上传器、全量历史、群/私聊标记、离线消息 | 账号+扫码登录、协议风控 | DB 密钥、版本适配 |
| 风险 | 低（只读通知） | 高（非官方客户端：封号/掉线） | 中（密钥要读进程内存；只读文件风险低） |
| 成本 | 中（要写手机侧上传器）；A2 桥接拉取=低但丢消息 | 中（<bridge-host> 有 node22+Xvfb，无 docker） | 中高（逆向解密） |

- B 的前置约束：NapCat 以“PC 端”登录，与本机在用的 linuxqq 可能互踢（**未实测**；E：一次登录测试即可判定）——用独立小号可规避。
- C 的证据：`head -c 16 nt_msg.db | xxd` → `SQLite header 3`；`python3` 以 `mode=ro` 打开 → `DatabaseError: file is not a database`；页 2 随机；`nt_data/` 下无明文 key 文件。
- 推荐：**B（独立小号）**，唯一能对齐“类似微信”收发语义的路径；上线前用 A2 做 1 天冒烟（零手机改动、只验证桥接与字段解析）；C 仅在“拒绝小号且接受逆向”时再评估。

## 5 A 路径输入契约草案：`plugins/sources/qq`
形状照 phone-hub（`plugins/sources/phone-hub/collector.py`）：`collect(config, store) -> list[event]`，失败抛 `CollectorError`。

```yaml
# plugin.yaml
name: qq
kind: source
api: 1
entry: collector.py:collect
default_enabled: false      # 上传链路未修，默认关
config_key: collect.qq
```
```yaml
collect:
  qq:
    enabled: false
    ssh_host: root@<bridge-host>          # 必须是 <bridge-host>：DB 在这；<wg-server-ip> 只有 10-10 的旧副本
    ssh_key: ~/.ssh/id_ed25519
    ssh_proxy: ''
    pkg: com.tencent.mobileqq
    limit: 200
    timeout_s: 30
    head_chars: 160
    backoff_after: 3
    backoff_s: 600
```

- 字段映射：`ts`(ms)→`E.iso(ts)`、`pkg/app/title/text/subtext/key`→payload 同名字段（title/text 截 `head_chars`），另带 `received_at`（观测上传延迟）；造事件前过 `redact.apply(payload, config)`（与 phone-hub 一致）。
- `kind`：`notify`（与 phone-hub 同 kind，下游按 `source` 区分；不另造 `im.message` 以免动派生层）。
- 去重键：`payload.key`（服务端 `UNIQUE(key)` 已挡重复；`fingerprint = sha256(source|kind|payload)`）。
- 游标：`{"last_id":int,"last_ts":int,"fail_streak":int,"backoff_until_ts":float}`；查询用 `WHERE pkg=? AND id>? ORDER BY id ASC LIMIT ?`。
  **用 id 不用 ts**：ts 来自手机时钟，可能回拨/批量同毫秒/乱序回填；id 是服务器自增，天然单调且不会漏。last_ts 只作观测字段。
- 远程读取：照 phone-hub 的 ssh 纪律（`BatchMode=yes`、`StrictHostKeyChecking=no`、`ConnectTimeout=10`、`-i <ssh_key>`），
  但**把 python 脚本从 stdin 喂过去**（`ssh … "python3 - <since_id> <limit>"`），不在 <bridge-host> 落脚本、也不用 `python3 -c` 一行（引号地狱）。
  理由：<bridge-host> `which sqlite3` → 未找到；DB 在本机不算可达（无 NFS/共享）；实测一次往返 **0.81s**、输出 JSON、rc=0。
- 不可用行为：**抛 `CollectorError`**（INTERFACE.md §4：失败由调用方兜住）；框架把 `ok=false/error` 落到 `collector_state`——现有 source 不返回 `{"ok":false}`，契约以此为准。
  连续失败 ≥ `backoff_after` 次 → 写 `backoff_until_ts=now+backoff_s`，冷却期内直接返回 `[]`（照 phone-hub `collector.py:60-68`）。
- 已知缺陷（要写进插件 README）：只能采到「每会话最后一条」；图片/文件只有占位符；同一 key 的更新会被 `UNIQUE(key)` 吞掉。

**B / C 的契约要点（同一插件名 `qq`，换实现不改 event 形状）**：
- B：NapCat OneBot 11 HTTP/WS → `message_id` 去重、`time` 作游标，`kind=im.message`，payload 用标准字段 `message_type(group|private)/group_id/user_id/sender/raw_message/time`；发送走独立通道（info-hub 只读）。
- C：读 `nt_msg.db`（解密后）→ 群/私聊两张消息表按 NTQQ 已知结构映射 `msg_id/chat_id/sender_id/ts/text`，游标用行自增序，`kind=im.message`；列名与密钥都要等解密后实测（当前阻塞）。

## 6 复现命令
```bash
ssh -i ~/.ssh/id_ed25519 root@<bridge-host> 'systemctl is-active qq-ingest; ss -lntp | grep 8123'
ssh -i ~/.ssh/id_ed25519 root@<bridge-host> 'python3 - <<PY
import sqlite3;c=sqlite3.connect("/opt/qq-relay/qq_ingest.db")
print(c.execute("select count(*),min(ts),max(ts) from n").fetchone())
print(c.execute("select count(*) from n where pkg=?", ("com.tencent.mobileqq",)).fetchone())
PY'
ssh -i ~/.ssh/id_ed25519 root@<wg-server-ip> 'cat /opt/qq-relay/ingest.log | tail -3; ss -lntp | grep -E "8123|9444"'
# 手机（经 MCP bridge；本机可达，initialize 返回 200）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://<bridge-host>:9500/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
```
