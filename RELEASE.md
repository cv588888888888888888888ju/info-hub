# 发布说明

## v0.1.0 — 2026-11-18

第一个可公开的版本：本体只剩「输入 + 推送」，采集器 / 加工 / 视图 / 通道全部是插件。

### 这个版本里有什么

| 资产 | 内容 | 怎么用 |
|---|---|---|
| `info-hub-0.1.0.tar.gz` | 本体（`src/`、`bin/`、`plugins/` 全量、文档、测试、systemd 单元） | 解包 → `pip install pyyaml` → `cp hub.config.example.yaml hub.config.yaml` → `bin/hub-store init` |
| `info-hub-collectors-0.1.0.tar.gz` | 10 个采集器（fs / clipboard / window / dsh-session / browser-history / bilibili / screenshot / phone-hub / phone-usage / study） | 解到 `plugins/sources/` 下 |
| `info-hub-channels-0.1.0.tar.gz` | 3 个投递通道（desktop-popup / phone-mcp / dsh-ask） | 解到 `plugins/channels/` 下 |
| `hubreplier-0.1.0.apk` | Android 端：消息页 + 可回复通知 + 常驻状态 | `adb install -r` 或直接点开安装；App 内设置页填「服务器地址 + token」 |

源码与资产分开：仓库里只有源码，二进制只在 Releases。

### 安全默认值

- 手机接口 token **不写死**：不填配置就首次启动自动生成 `hub-<32hex>` 到 `$HUB_ROOT/phone_api.token`（0600）；
- APK 里没有任何写死的地址或 token，装完由人在设置页填；App 不改服务端状态；
- 示例配置默认只听 `127.0.0.1`；要公网/局域网访问得自己显式改 `phone_api.hosts` + `exposure`，并接受「只有 token 一道门」这个事实。

### 已知限制（都是设计取舍，不是 bug）

- 手机侧自带的 adb 通道只支持 `shell`，**不支持 `push` / `pull`**（设备端 shell 协议收尾包对不上，往上一版试过五种实现都不成）；
- `dsh-ask` 通道需要 DSH 侧同时装插件才会弹问答卡片；没有在线 agent 时它只留台账，不阻塞采集；
- 桌面弹窗通道依赖 `gdbus` / 通知服务，headless 环境下静默跳过；通知默认关闭；
- APK 是 debug 签名，供 sideload；上架需要自己换签名。

### 从旧版本迁移

- token：以前写死在 `hub.config.yaml` / APK 里，现在两边都留空即可，值落在 `$HUB_ROOT/phone_api.token`；
- 采集器：以前在 `src/infohub/collectors/`，现在在 `plugins/sources/`（`git mv` 保留历史）；
- 旧的内置 MCP 中转（`/opt/mcp-bridge`）已退役，`phone-hub` 采集器不再依赖它。
