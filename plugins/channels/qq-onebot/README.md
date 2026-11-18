# qq-onebot 通道插件

把一条投递发到 QQ（OneBot v11 HTTP，后端 = 家庭机上的 SnowLuma 注入真实 QQ 客户端），
并把对方回来的消息结算成「回复」。契约：INTERFACE.md §11.7d（配置键、失败语义、验收）。

## 1. 入口

- 清单：\`plugin.yaml\`（\`kind: channel\`、\`entry: channel.py:send\`、\`default_enabled: false\`）
- 装机闸：\`hub.config.yaml\` 里 \`plugins.qq-onebot.enabled: true\`（缺省关）
- 单函数：\`send(request)\`，op ∈ \`dispatch\` / \`retry\` / \`dismiss\`
  - request 键：\`config\`、\`store\`、\`op\`、\`target={type,id}\`、\`title\`、\`body\`、\`options\`、\`kind\`、\`todo_id\`、\`delivery_id\`

## 2. 命令（用户/agent 面）

\`\`\`bash
bin/hub-qq-send --to 123456789 --title "标题" --body "正文"
bin/hub-qq-send --to 阿甲 --title "按好友名发"          # 名字先解析成 uin
bin/hub-qq-send --to 998877 --group --title "群消息"
bin/hub-qq-send --to 123456 --title "选一个" --options '[{"key":"a","label":"能"},{"key":"b","label":"不能"}]'
bin/hub-qq-send --pending                # 重推 channel=qq 的 pending 行
bin/hub-qq-send --dismiss <投递 id>       # OneBot delete_msg 撤回
bin/hub-qq-replies [--json] [--dry-run]  # 把 QQ 回来的消息结算成回复
\`\`\`

退出码：\`0\` 成功；\`1\` 远端拒绝/不可达（**行留 pending + last_error**，不是丢消息）；\`2\` 入参错（库不变）。

## 3. 配置（\`qq_onebot.*\`）

| 键 | 缺省 | 说明 |
|---|---|---|
| \`ssh_host\` | \`root@<bridge-host>\` | 置空 = 本机执行同一条命令（测试注入） |
| \`api\` | \`http://127.0.0.1:3000\` | OneBot **HTTP** 适配器（后端机视角） |
| \`token_file\` | \`/opt/qq-relay/onebot.token\` | HTTP 适配器的 access token（0600；**ws 口是另一个 token**） |
| \`spool_path\` | \`/opt/qq-relay/queue/events.jsonl\` | 事件 spool（与采集器共用） |
| \`timeout_s\` | 25 | 单次远端调用超时 |

## 4. 语义

- 发送：先建 \`channel=qq\` 的投递行，再 \`send_msg\`；成功写 \`remote_ref={"type","id","message_id"}\`。
- 失败：行留 \`pending\`、\`attempts=1\`、\`last_error\`；\`--pending\` 可重推。
- 回复：\`hub-qq-replies\` 读同一个 spool（游标 state key \`qq-replies\`），把对方私聊/群消息对到
  「最近一条同目标、未回复」的行；有 options 时 \`1..n\` 或 label/key 相等 ⇒ 记选项标签，否则原样记文本。
- 不动待办状态：回复只写 \`status=replied\` + \`reply\`（与 §11.4 一致）。

## 5. 验收

\`tests/check_qq_channel.sh\`（35 项，本机 stub OneBot，不需要真 ssh / 真 QQ）：

\`\`\`bash
cd info-hub && ./tests/check_qq_channel.sh     # 35 通过 / 0 失败 / ALL PASS
\`\`\`

## 6. 排障

| 现象 | 先看 |
|---|---|
| \`HTTP 401\` | \`token_file\` 里是不是 **HTTP** 口的 token（ws 口 token 不一样） |
| \`Connection refused\` | 后端 SnowLuma 还活着吗、OneBot HTTP 适配器端口还是 3000 吗 |
| \`Unknown action\` | 后端 OneBot 实现是否支持该 action（\`send_msg\`/\`delete_msg\`/\`get_friend_list\`） |
| 回复结算不到 | 事件 spool 在涨吗（\`wc -l /opt/qq-relay/queue/events.jsonl\`）、\`onebot-tap.service\` 是否 active |
