# hubreplier — 让 info-hub 的投递在手机通知栏里直接回复 / 点选项

一个极小的 helper APK（不依赖 gradle / androidx，纯 framework API）。它替 info-hub 发一条
**带 RemoteInput 输入框的通知**；用户在通知栏里回复或点选项后，结果写到 App 私有目录，
PC 侧用 `adb shell run-as` 读回。之所以另起一个 APK：已有的 `com.jxev.ai` v0.2.2 六个 dex 里
`KEY_TEXT_REPLY` / `deliver_show` 各 0 次，结构上做不出可回复通知；本 APK **不动** `com.jxev.ai`。

- 包名 `com.chenty.hubreplier`，version 0.1.0，minSdk 26，targetSdk 36，`android:debuggable="true"`（run-as 才能读文件）
- 构建产物 `hubreplier-0.1.0-debug.apk`（约 25 KB）
- 已实测机型：Xiaomi <model> / HyperOS V816 / Android 16 (SDK 36)，`wm size 1080x2400`

## §1 目录与构建（不用 gradle）

```
build.sh            # aapt2 compile/link → javac → d8 → zip -j classes.dex → zipalign → apksigner
install.sh          # adb install -r -g（自动点 MIUI「继续安装」）→ 解 stopped → 授监听权限
test.sh             # 一条命令回归：投递 → 断言通知结构 → 三种答复落盘 → 截图存证
AndroidManifest.xml
res/drawable/ic_stat_reply.xml, res/values/strings.xml
src/com/chenty/hubreplier/
  BuildConfig.java     # token / channel / action / key 常量（唯一出处）
  HubActivity.java     # 正式入口（am start）——MIUI 会丢 shell 广播，见 §6
  ShowReceiver.java    # 发通知（广播入口，换 ROM 兜底）
  ResponseReceiver.java# 答复入口：RemoteInput 结果 / 选项按钮 / TEST_REPLY
  DismissReceiver.java # 撤消息（幂等）
  ReplyStore.java      # 落盘（内部权威 + 外部镜像）
  Notifications.java   # channel / 通知构造 / 已回复刷新
  HubClient.java       # 回执 POST 127.0.0.1:8799（失败静默，只当兜底）
  KeepaliveListener.java, MainActivity.java
```

依赖：JDK 21、`$ANDROID_SDK/platforms/android-36/android.jar`、
build-tools 35（`$BUILD_TOOLS/{aapt2,d8,zipalign,apksigner}`）。
脚本里全部可用环境变量覆盖：`ANDROID_JAR` / `BUILD_TOOLS` / `ADB` / `HUB_DEVICE` / `HUB_EVIDENCE`。

```bash
./build.sh && ./install.sh     # 出包 + 装机
./test.sh                      # 9 项断言（默认）
HUB_TAP=1 ./test.sh            # 额外做一次「通知栏真点选项按钮」
```

> javac 用 `-source 8 -target 8 -bootclasspath android.jar`：JDK 21 不允许把 `-bootclasspath`
> 和 `-source 17` 一起用（报“目标 17 不允许选项 --boot-class-path”）。工程不用 Java 9+ 语法，d8 负责 desugar。

## §2 入口与 extras 契约

三个 action 都是 exported，**除 Activity 入口外都要 token**；token 不对直接丢弃并打日志。

| 入口 | 方式 | action / op | 必填 | 可选 |
|---|---|---|---|---|
| 投递 | `am start -n com.chenty.hubreplier/.HubActivity` | `--es op show` | `payload_b64`（或 `payload`，或逐字段 `id/title/body`） | `mode`、`options` |
| 答复（文本） | 同上 / 广播 | `--es op reply` | `--es id`、`--es text` | — |
| 答复（选项，脚本用） | 同上 / 广播 | `--es op action` | `--es id`、`--es option_key` | — |
| 撤消息 | 同上 / 广播 | `--es op dismiss` | `--es id` | — |
| 通知栏点「回复」 | 系统填 RemoteInput | `com.chenty.hubreplier.RESPONSE` | 系统写入 clipData | — |
| 通知栏点选项 | 系统发 PendingIntent | `com.chenty.hubreplier.RESPONSE` | `option_index`/`option_key`/`option_text` | — |
| 仅测试 | 广播或 am start | `com.chenty.hubreplier.TEST_REPLY` | `id` | `text`（默认 `TEST_REPLY`） |

投递 payload（JSON，**推荐 base64**：adb shell 双层引号会吃掉 JSON 里的双引号，实测会报
`JSONException: Unterminated object`）：

```json
{"id":"d-20261114174625-ed7f88","mode":"question","title":"确认一下","body":"这单要不要发?",
 "options":[{"key":"done","label":"做完了"},{"key":"later","label":"稍后"}],
 "layout":"default","token":"<服务端生成的 token>"}
```

- `id`：只允许 `[A-Za-z0-9._-]`，≤96 字符（会当文件名）。`options` 最多取前 3 个；元素可以是
  `{"key","label"}` 或裸字符串（key=label）。`mode` 只用于通知副标题 `hub:<mode>`。
- `token` 不再写死在 APK 里：必须等于设置页填的那个（服务端首次启动生成到 `$HUB_ROOT/phone_api.token`）。
  广播入口必须自己带。
- 通知 id = `id.hashCode() & 0x7fffffff`，**同一个 id 再投递会原地替换**那条通知。

示例（注意 `payload_b64` 单引号包住）：

```bash
B64=$(printf '%s' '{"id":"i-1","title":"t","body":"b","options":[{"key":"done","label":"做完了"}]}' | base64 -w0)
adb -s <serial> shell am start -n com.chenty.hubreplier/.HubActivity --es op show --es payload_b64 $B64
```

## §2.5 主页常驻通知与「不依赖 VPN」的地址择优（2026-11-17）

HomeActivity 装 info-hub 的移动页；HomeSync 前台服务每 120s 拉一次待处理数，画一条不可划掉的常驻通知
（channel `hub_home_ongoing`、NOTIF_ID 42001，点它进 HomeActivity）。

两处都走 **Endpoints.pick()**（Endpoints.java）：按 `url` → `urls` 的顺序探测候选地址
（带 token 的 GET `api/messages?limit=1`，连接/读各 2.5s），第一个 200 的胜出并缓存 60s；
轮询失败自动作废重探。所以手机在 Wi-Fi 用内网地址、在外用 IPv6 / Cloudflare、实在不通用本机中转，
**都不用改配置、不用重启 App**。

- 备选里可以放：内网 `http://<lan-ip>:8800/`、IPv6 `http://[2408:…::1]:8800/`、
  Cloudflare `https://xxx.trycloudflare.com/`、WG `http://<hub-host>:8800/`。
- **本机中转是最后一档**（LocalRelay.java，`relay`≠0 时）：候选全不通才起 `127.0.0.1:8801`，
  用 SOCKS5 无认证连 `127.0.0.1:10800`（手机 MCP com.jxev.ai 的用户态隧道口，把 内网网段 经公网 TCP/443 送到 ECS 再进 WG 网段），
  目标是 `relay_url`（缺省 `url`）。SOCKS 不通退回直连。不改 com.jxev.ai 任何东西。

| prefs 键（hub_home） | 缺省 | 语义 |
|---|---|---|
| url | 空 | 主地址（协议 + 端口写全）；探测第一顺位 |
| urls | 空 | 备选地址，逗号或换行分隔 |
| token | 空 | 每个请求带 X-Hub-Token / ?token= |
| relay | 1 | `0` = 不用本机中转（纯内网/纯 Cloudflare 场景） |
| relay_url | 空 | 中转目标，缺省用 url |
| socks_port | 10800 | 手机 MCP 的 SOCKS5 口；com.jxev.ai 换端口时要跟着改 |
| relay_port | 8801 | 本机中转监听口（只绑 127.0.0.1） |

写入方式（am start 会持久化）：`--es url … --es token … --es urls "https://x.trycloudflare.com/" --es relay_url "http://<hub-host>:8800/"`。

排障锚点（logcat -s HubReplier）：`Endpoints picked <base>`（选中）、`probe <base> 不通：…`（候选失败原因）、
`Endpoints: 没有可用地址 […]`（全挂，页面会试直连第一个）、`relay 127.0.0.1:8801 -> … via socks …`（中转起好）、
`HomeSync pending=N total=M`（拉到了）。

### §2.5.1 三条接入路线（同一 App，只换 prefs）

| 场景 | 配法 | 现状 |
|---|---|---|
| 手机与 PC 同一 Wi-Fi | url = `http://<PC 局域网 IP>:8800/`；relay=0 | 服务端已监听 0.0.0.0；PC 侧 `curl <lan-ip>:8800` = 200（2026-11-17）。**手机需在同一 Wi-Fi 才会选中它** |
| 手机用流量 / 不在家 | urls = Cloudflare 隧道地址（`cloudflared tunnel --url http://127.0.0.1:8800`） | **已实测**：手机 5G 无 VPN，页面与 /api/messages 都 200 |
| 家里有 IPv6 公网 | urls 加 `http://[<家庭出口机 IPv6>]:8443/`（出口机转发到 WG <hub-host>:8800） | **已实测**（2026-11-17 21:58）：手机 5G 选中该候选，`/api/messages` 200、页面 200、通知同步 OK；比 Cloudflare 快（探测 ~0.3s vs ~1.5s） |
| 有 com.jxev.ai 且不想开隧道 | relay_url = `http://<hub-host>:8800/`（relay 保持 1） | 已实测（2026-11-17 之前的主路径） |

### §2.5.2 设置页（2026-11-17：全部配置都在手机上填，不用 adb）

入口两条：① 消息页右上「⚙ 设置」（页面发 `hub://settings`，HomeActivity 的 WebViewClient 拦下）；
② 桌面图标（MainActivity：prefs 里没地址就直接进设置页，填过则进消息页）。

字段与动作（SettingsActivity，程序化 UI，无 layout XML）：服务器地址（主）/ 备选地址（每行一个，逗号也行）/
token / 中转目标；「保存」写 prefs 并作废地址缓存，「测试连接」逐候选探 api/messages（直连全不通时自动试本机中转，命中会标「经本机中转」——手机不接 VPN 时这是常态）
（显示 可用 Nms / 401 token 不对 / 不通）。**没有密码相关字段** —— 门禁只有 token 一道
（用户 2026-11-17 22:56「有了 token 本身也不需要密码，可以删掉」）。

消息页顶部的原生状态条：正常时隐藏（页面自己的 ⚙ 够用）；页面 401 / 连不上时露出红底红字原因
（如「token 不对 —— 点右上「⚙ 设置」改正」）+ 「⚙ 设置」按钮——**设置入口不依赖网页 JS**，token 填错也进得去。

**装完手填（用户 2026-11-17 22:51 澄清「不用 adb，可以安装应用之后手填配置」）**：App 不自动填、不写回 prefs、
不改服务端状态；缺什么就在设置页填什么（四个字段 + 保存）。要填的值只有两个：地址（内网 / IPv6 / Cloudflare 隧道 /
中转任一）与 token（服务端 `hub.config.yaml` 的 `phone_api.token`）。备选地址可留空。

## §2.6 通知栏：每条待处理一条可回复通知（2026-11-18 定型）

- **谁发**：`HomeSync` 前台服务自己拉——每 120 s `GET <base>api/messages?limit=80&archived=0`，不经服务端推送、不经 adb。
  `<base>` = 设置页候选里由 `Endpoints.pick` 挑出的一个（直连优先；全不通才走 `LocalRelay` 的 SOCKS 中转）。
- **哪些行**：`status ∈ {pending, claimed}`。每条一条通知（notification id 由 delivery id 派生；同 id 覆盖不堆叠，
  `shownIds` 保证同一行在一轮生命周期里只提醒一次）。
- **长什么样**：标题 + BigText 正文 + 「回复」RemoteInput 动作 + 最多 3 个选项按钮；channel `hub_reply`（`IMPORTANCE_HIGH`）。
  subtext 只在 `mode` 非空且非字面 `"null"` 时显示 `hub:<mode>`——org.json 的 `optString` 会把 JSON null 变成字符串 `"null"`，必须挡。
- **常驻那条**：`NOTIF_ID=42001`，不可划掉，「info-hub · N 条待处理，点开看全部」，点开进主页；
  拉失败时文案变「连不上 info-hub（<base>）」。
- **点之后**：选项按钮 ⇒ 本地 jsonl `action=<key>`、回传 `reply=<label>`；输入框 ⇒ 回传 `reply=<text>`。`ResponseReceiver`（显式广播、`exported=false`）
  先写本地 jsonl（§3），再 POST `<base>api/deliver/reply`（头 `X-Hub-Token`）。成功 ⇒ 通知刷成
  「已回复：<答案>（已回传 info-hub）」并 autoCancel；失败 ⇒「（已落盘，PC 侧可读）」+ `Endpoints.invalidate()` 下轮重选地址。
- **行消失就收回**：下一轮拉取发现该 id 不在 pending/claimed 集合 ⇒ `Notifications.dismiss`。
- **通知权限（Android 13+）**：没授 `POST_NOTIFICATIONS` 时通知全静默（`dumpsys notification` 里 `AppSettings importance=NONE`，
  一条都不显示）。主页顶部琥珀横幅「通知权限没开：收不到可回复通知 [开启]」→ 点它走 `requestPermissions`；授权后立刻 `HomeSync.start`。
- **PendingIntent 必须 `FLAG_MUTABLE`**（Android 12+ RemoteInput 的硬要求），否则系统填不进结果。
- **选项按钮回什么**：回传 info-hub 的文本取 **label**（「收到」），key（`yes`）只留在本地 jsonl 的 `action` 字段；
  没有 label 才退回 key。这样台账里 `reply=等会儿` 是人话，程序侧要 key 仍可从手机文件取。
- **服务启动清场**：`onStartCommand` 里 `NotificationManager.cancelAll()` 清掉上个进程遗留的「已回复：…」卡片，常驻那条随后立即重建。
- **真机判据（2026-11-18 09:47–09:56 已实测）**：通知栏展开一条 ⇒ 点「收到」⇒ 该行 `status=replied`、`reply=收到`；
  logcat `committed id=… src=button` 与 `postReply … => 200`。

## §2.7 强提醒：可回复全屏页 StrongActivity（2026-11-18 定型）

`kind=strong` 的投递行由 HomeSync 轮询（`INTERVAL_MS=30000`，30 s 一轮）发现后直接拉起全屏页，
不经过通知栏，也不调用 com.jxev.ai 的 `reminder_show` —— 两条路同时走会在手机上并存两个弹窗
（用户 2026-11-18 实测后明确只要 info-hub 这一个）。

- **拉起**：`StrongActivity.intentFor(ctx, id, title, body, optionsJson)`；`setShowWhenLocked(true)` + `setTurnScreenOn(true)`（API<27 走窗口 flag），锁屏也是整屏。同一 id 只拉一次（`strongShown` 集合）；行离开 pending/claimed 或归档后从集合移除，可再次推送。
- **内容**：标题（26sp）+ 正文 + 每个选项一个按钮 + 输入框 + 「回复」按钮；点选项＝用该选项 label 直接 reply，输入文字＝自由文本 reply。
- **回写**：`HubClient.postReply(ctx, id, text)` → `POST <base>api/deliver/reply`（头 X-Hub-Token，2xx 算成功，失败 `Endpoints.invalidate()`），台账 `status=replied`、`reply=<原文>`，info-hub 页面与 `/api/messages` 立刻可见。
- **失败**：地址全不通时 Toast「回复失败（会保留在主页）」，行保持 pending，下一轮还会再拉。
- **与 §2.6 的关系**：普通 `pending/claimed` 行照旧发通知栏可回复通知；只有 `kind=strong` 额外拉全屏页（两条都发，通知栏那条便于事后回看）。
- **验收**：真机 <serial> 实测 —— 选项点击 ⇒ 台账 `reply=稍后`；打字 ⇒ `reply=，1111号`（用户手输）；`dumpsys window` 只有 `com.chenty.hubreplier/.StrongActivity`，`ReminderActivity` 窗口数 0。
## §3 落盘 schema（Lead 侧按字段解析，别改名）

> **2026-11-18 起主从关系变了**：本人答复的**主通道**是直接 POST `<base>api/deliver/reply`（见 §2.6），
> 本节的 jsonl 降为**本地账 / 网络不通时的兜底**（`commit()` 先写盘再 POST）。旧路径
> `bin/hub-phone-replies --apply` + `adb pull` 仍可用，但不再是必需环节。

`files/replies/all.jsonl` 每行一个对象，**只 append 不重写**：

```json
{"id":"i-1","kind":"action","text":null,"action":"done","src":"button","at":"2026-11-14T17:56:10+08:00"}
{"id":"i-2","kind":"reply","text":"手机答","action":null,"src":"remoteinput","at":"2026-11-14T18:02:06+08:00"}
```

| 字段 | 取值 |
|---|---|
| `kind` | `reply`（自由文本）/ `action`（选项） |
| `text` | kind=reply 时的原文；kind=action 时为 `null` |
| `action` | kind=action 时的选项 key；kind=reply 时为 `null` |
| `src` | `remoteinput`（通知栏输入框）/ `button`（通知栏选项按钮）/ `test`（TEST_REPLY 或 op=reply、op=action 脚本入口） |
| `at` | ISO8601 带时区，如 `2026-11-14T17:56:10+08:00` |

其它文件：

- `files/replies/<id>.json`：该 id 的**最后一条**答复（同字段，缩进 2）。
- `files/meta/<id>.json`：投递时记下的 title/body/mode/options/shown_at，答复时用来把通知刷成「已回复」。
- 外部镜像：`/sdcard/Android/data/com.chenty.hubreplier/files/replies/`（同样两个文件；部分 ROM 不可写，失败不影响主流程）。

读回（debuggable=true 才可用）：

```bash
adb -s <serial> shell run-as com.chenty.hubreplier cat files/replies/all.jsonl
adb -s <serial> exec-out run-as com.chenty.hubreplier cat files/replies/all.jsonl > replies.jsonl
```

## §4 通知长什么样

- channel `hub_reply`，`IMPORTANCE_HIGH`，`category=msg`，小图标 `ic_stat_reply`，`setOnlyAlertOnce(true)`。
- actions：`[0] 回复`（RemoteInput，key `reply_text`，label「回复」）+ 最多 3 个选项按钮。
  MIUI 实测 `getMaxActions()` 为 4，第 4 个（回复 + 3 选项）会进溢出菜单，所以**建议只给 2 个选项**。
- 每条待处理一条通知（id 由 delivery id 派生）；另有 `NOTIF_ID=42001` 的常驻计数通知（§2.6）。
- 副标题（subtext）只在 `mode` 非空且非字面 `"null"` 时显示 `hub:<mode>`——真机曾出现 `hub:null`，
  因为 org.json 的 `optString` 把 JSON null 返回成字符串 `"null"`。
- 答复后同一条通知刷成「已回复：<答案>（已回传 info-hub）」/「（已落盘，PC 侧可读）」，自动消失，不再带按钮。
- 通知权限没开（Android 13+ `POST_NOTIFICATIONS`）时一条都不显示，主页顶部有琥珀横幅可一键开启（§2.6）。
## §5 脚本行为与失败处理

`build.sh`：清理 `build/` 用 `find ... -delete`（本机 `rm` 是移入备份区的 wrapper，`rm -rf` 会留备份垃圾）；
首次运行自动 `keytool` 生成 `hubreplier-debug.keystore`（storepass/keypass 都是 `android`，alias `hubreplier`），
签名 v1+v2，末尾打印证书 DN、`aapt dump badging`、APK 大小。
**签名密钥不入库**：仓库里没有 `.keystore`（发布时已剔除），换机后第一次构建会自己生成一把新的 —— 旧 APK 就得卸了重装
（包名相同但签名不同，`adb install -r` 会报 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`）。

`install.sh`：MIUI 的「USB安装提示」默认 6 秒后自动**拒绝**（`INSTALL_FAILED_USER_RESTRICTED`），
脚本在 install 期间轮询 `dumpsys window`，出现 `AdbInstallActivity` 就用 uiautomator 解析「继续安装」并点它
（解析不到时退回本机实测坐标 `311 2232`，1080x2400）。装完必须 `am start .MainActivity` **一次** 解掉
`stopped=true`——Android 3.1+ 新装包广播一律被丢；**绝不能随后 `force-stop`**，那会把 stopped 置回 true。

`test.sh`（默认 7 步 / 9 条断言，全过 exit 0）：

1. 设备在线、包已安装；
2. `am start op=show` 投递一条带 2 个选项的通知，校验 logcat `SHOW ok id=...`；
3. `dumpsys notification --noredact` 断言：该 `hub_id` 的记录 `channel=hub_reply`、`importance=4(HIGH)`、
   `category=msg`、actions 里含「回复」（RemoteInput）+ 选项；
4. `op=reply` → 断言 `all.jsonl` 新行 `kind=reply src=test`；
5. `op=action` → 断言 `kind=action action=done src=test`；
6. 展开通知栏截图 + 断言通知已被刷成 `hub:answered`；
7. `HUB_TAP=1` 时额外用 uiautomator 定位「做完了」按钮并 `input tap`，断言真实按钮落盘 `src=button`。

证据默认写到 `~/hub-tasks/runs/core-deliver-plugins/evidence/hubreplier/`
（`dumpsys-<id>.txt`、`test-<id>-shade.png`、`test-<id>-tap.png`）。

## §6 已知限制

1. **shell 广播在本机被 MIUI Greezer 丢弃**：`logcat` 实证 `Greezer Denial ... need cached broadcast` /
   `process is not permitted to auto start`。所以正式投递走 `am start`（Activity 不受此限）。
   已试且无效：`am set-standby-bucket active`、`cmd deviceidle whitelist`、`settings put system
   power_broadcast_white_list`、常驻 NotificationListenerService。换 ROM 时广播入口可直接用。
2. **token 是明文 constants**，只防误触，不防恶意应用（同机任意 app 知道 action+token 即可投递）。
   需要更强隔离时应改成每装一次随机 token 并配 `android:permission` 自定义签名级权限。
3. 答复只写文件 + 尽力 POST 回执（`http://127.0.0.1:8799/api/deliver/reply`，靠 `adb reverse`；
   投递 id 不存在时后端返回 404，App 侧只记日志，视为正常兜底）。**没有反向长连接**，PC 侧靠轮询文件或 POST。
4. 通知同一时刻只保留「最近投递」的一批（同 id 替换、DISMISS 可撤）；不做队列/重放，回复只保留最后一条 + 全量 jsonl。
5. 真点通知栏按钮已脚本化（`HUB_TAP=1 ./test.sh`，10/10 通过）：MIUI 会把同一应用的多条通知收进分组，
   脚本的做法是「先点分组展开箭头 → 再点本行箭头 → 再点选项按钮」，每次点击前都重新 uiautomator dump 定位，
   最多 5 轮；分组/遮挡导致定位不到时只 SKIP 不判失败。真点「回复」输入框那一步仍只能人点（`input text` 只支持 ASCII）。
   真机已实测：点「回复」→ 内联输入框 → `adb shell input text "reply-from-real-tap"` → 点发送 →
   `all.jsonl` 落 `src=remoteinput` 行（见证据目录 `reply-input-ui.png` / `option-done-tapped.png`）。

## §7 卸载

```bash
adb -s <serial> uninstall com.chenty.hubreplier
```

只删本包；`com.jxev.ai` 与任何其它应用不受影响。已投递内容不会被保留（`allowBackup=false`）。
