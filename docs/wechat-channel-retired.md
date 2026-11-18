# 微信推送渠道：已退役（2026-11-18）

> 用户原话（m08039）：「微信可以写明已经退役，微信渠道基本用不着了」。

## 退役范围
- **不再由 wx-outlet 向电脑/手机推「微信紧急提醒」**。理由：机械命中「急迫词」就弹 zenity 模态框 + 手机全屏，
  实测噪音（例：`急死我了` 命中 `急` ⇒ 弹窗「微信紧急提醒（命中规则：急迫词）」）。
- 机械紧急判定仍照旧记日志、仍把命中文本回写到微信自留地（`send_wechat`），但 `deliver()` 的通道从
  `["wechat", "popup", "phone"]` 收窄为 `["wechat"]`（/opt/wxhub/wx-outlet.py 的 `push_item`，备份 `/root/_backup/wx-outlet.py.bak-20261118-1848`）。
- 云端 agent 决策出来的推送（`run_agent_batch` → `deliver(channels, …)`）保持原样，不受本次收窄影响。
- 微信消息**采集与回复**（wx-gateway / wxhub / wx-chat-poller）仍在跑，退役的只是「微信 → 电脑/手机」这条推送渠道。

## 为什么不再用
- 电脑腿与 info-hub 的强推送（`bin/hub-strong-push`，INTERFACE.md §11.7c）重合，且 zenity 模态框更吵；
- 手机腿与 helper 的可回复全屏页（StrongActivity）重合，且那条能直接回台账；
- 微信这边的机械判定误报率高（关键词命中聊天感叹），真正的紧急交给 info-hub 的投递台账与 agent 判断。

## 现状取证
- `qq-recon` 2026-11-18 报告：手机 `relay.json` 已改为「自己摘要直发微信」，`qq-ingest` 自 2026-10-10 起零上传；
- `systemctl is-active wx-outlet.service` = active（进程在，但机械推送已收窄）；
- 若确要恢复旧行为：把 `push_item` 里的 `deliver(["wechat"], …)` 改回 `deliver(["wechat", "popup", "phone"], …)` 并 `systemctl restart wx-outlet`。

## 相关
- info-hub 强推送契约：INTERFACE.md §11.7c（`bin/hub-strong-push`）
- QQ 采集调研：`info-hub/docs/qq-source-recon.md`