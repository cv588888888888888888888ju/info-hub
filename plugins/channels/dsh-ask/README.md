# dsh-ask —— DSH 双弹通道插件（S11）

DSH 调 `ask_user_question` 时，**DSH 卡片与手机卡片同时弹出**；哪边先答哪边生效，另一边撤销。
本插件是 DSH 侧插件（ESM + 瀑布监听），不是 info-hub 的 python 通道；契约全文见
`info-hub/INTERFACE.md` **§12**（键表 / 返回 / 失败语义 / 缺口 / 装载方式），本文只给操作入口。

## 文件

| 文件 | 作用 |
|---|---|
| `index.mjs` | DSH 入口 `apply(ctx, config)`；监听 `user-questions/request`，双弹 + 先到先得 |
| `ledger.py` | 独立进程直连 info-hub 库：`add/get/expire/ack/reply`（补 §11.5 缺口，见 §12.6） |
| `plugin.yaml` | info-hub 插件清单（`hub-plugin list` 用；`runtime: node` 只校验入口存在） |
| `package.json` | ESM 包 + `dsh.bundle.patch` 指向 `cordis.patch.yml` |
| `cordis.patch.yml` | 可直接抄进 `~/.dsh/cordis.patch.yml` 的装载块（绝对路径） |
| `selftest.mjs` | A12/A20 的 12 项替身自测 |

## 自测（判据 A12 / A20）

```bash
cd $HUB_ROOT/plugins/channels/dsh-ask
node selftest.mjs    # 全过退出 0；末行 SUMMARY {"checks":12,"failed":0,...}
```

全替身：不连真库、不连手机、不弹通知、不起 DSH。真实装载与真机点按归用户（§12.7 / S12）。

## 配置（装载块的 `config`）

最小可用：

```yaml
config:
  enabled: true
  hub_url: http://127.0.0.1:8799
  phone_mcp_url: http://<bridge-host>:9500/mcp
  hub_root: $HUB_ROOT
  timeout_ms: 600000
  python: python3
```

`enabled` 缺省 **false**；`hub_root` 只用于拼 `PYTHONPATH=<hub_root>/src`，不改库位置。
其余键（`poll_ms` / `hub_timeout_ms` / `phone_timeout_ms` / `ledger_timeout_ms` / `debug` 等）见 §12.2。

## 台账命令（手工排查用）

```bash
export PYTHONPATH=$HUB_ROOT/src
python3 ledger.py get <delivery_id>            # 单行 JSON
python3 ledger.py expire <delivery_id>         # 置 expired + 释放租约（保留 acked/replied）
python3 ledger.py ack <delivery_id> --action ack
python3 ledger.py reply <delivery_id> --text "…"
```

库位置按 `hub.config.yaml` 的 `root`（本机 `~/.dsh/info-hub/hub.db`）；测试时用 `HUB_CONFIG` + `HUB_ROOT` 指向临时根。

## 已知限制

- 手机一次只能答**第一问**；其余问题在返回里补 `selected: []`（= 跳过）。
- 手机按钮的 `action` 必须是 §11.2 的动作键（§11.5 会校验），所以第一问的前 3 个选项按位置映射为
  `ack/done/snooze`；超过 3 个选项或要选任意项时请用文本回复（文本逐字等于某选项 label 时按该选项作答）。
- 手机 App 尚未实现 `deliver_show`/`deliver_dismiss`（S10）；真机双弹与「手机先答后 DSH 卡片消失」待 S12 确认。
- 装载后需**用户重启 `dsh web`** 才生效（热装载未实测）。

## 维护者接手

改 `index.mjs` 的入口形状 ⇒ 同步 `plugin.yaml` 的 `entry` 与 §12.1；
改台账列/语义 ⇒ 同步 §12.3 与 `ledger.py` 的子命令表；
改失败分支 ⇒ 先改 `selftest.mjs` 的对应用例，再改实现（判据先行）。
