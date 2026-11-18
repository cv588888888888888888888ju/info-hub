# 控制台「媒体」面板契约（plugins/views/ui）

状态：已实现。代码：`plugins/views/ui/ui.py`。验收：`tests/check_ui_assets.sh`。
定位：控制台是 view 插件（`INTERFACE.md` §4.8 / §10），媒体面板是它的**只读**附加面——
只把「允许根」里的图片与文档列出来、按需把字节吐回去，**不写任何系统数据**（与 §4.8 同一条纪律）。

本文件是该面板的契约：键名、JSON 形状、id 编码、越界语义、上限。代码里的注释只写指针。

## 1. 允许根（唯一的数据面）

面板只扫**允许根**，不碰数据根下的其它目录（`hub.db`、`logs`、`state`、`vendor` 等一律不在扫描范围）。

| 配置键 | 语义 | 缺省回退 |
|---|---|---|
| `ui.assets.enabled` | 面板与三条路由的开关 | `true` |
| `ui.assets.roots` | 允许根列表，**相对数据根的路径**（不是绝对路径） | `[collect.screenshot.dir 或 "screens", reports_dir 或 "reports"]` |

- 允许根必须是 `root` 之下的目录；配置成绝对路径或解析后跑出 `root` 的条目**被静默跳过**（不是报错），不参与列表也不参与取字节。
- 缺省两个根对应本项目现有产物：截图（`collect.screenshot.dir`，空 = `<root>/screens`）与报表（`reports_dir`，缺省 `reports`）。
- 改动值只改 `hub.config.yaml` 一处；本节右列的「缺省回退」只是没配时的代码兜底，不是第二份真相。
- 扫描纪律：只下钻有限层（`os.walk` 不跟软链目录）、跳过隐藏文件/隐藏目录、每个请求最多扫 `ui.assets.max_scan` 个候选文件（见 §5），**不遍历整个磁盘**。目录与文件名按倒序遍历 ⇒ 撞到 `max_scan` 上限时保留的是**最新那批**（截图按 `YYYY-MM-DD/` 命名，倒序即时间倒序）。

## 2. `GET /api/assets`

查询参数：

| 参数 | 取值 | 缺省 |
|---|---|---|
| `kind` | `image` / `doc` / `all` | `all` |
| `limit` | 整数；超出 `ui.assets.list_limit` 的硬上限 ⇒ `400` | `ui.assets.list_limit` |
| `cursor` | 上一页返回的 `nextCursor`（不透明串） | 无（第一页） |

响应 `200`：

```json
{
  "kind": "image",
  "limit": 60,
  "items": [
    {
      "id": "c2NyZWVucy8yMDI2LTExLTAzLzA4NDMxNi5qcGc",
      "kind": "image",
      "name": "2026-11-03/084316.jpg",
      "size": 137245,
      "mtime": "2026-11-03T08:43:16+08:00",
      "url": "/asset/c2NyZWVucy8yMDI2LTExLTAzLzA4NDMxNi5qcGc",
      "thumb": "/thumbs/c2NyZWVucy8yMDI2LTExLTAzLzA4NDMxNi5qcGc"
    }
  ],
  "nextCursor": "WzE3NjIxNDQxOTYwMDAwMDAwLCJzY3JlZW5zLzIwMjYtMTEtMDMvMDg0MzE2LmpwZyJd"
}
```

- 排序：按 `mtime` **倒序**（文件修改时间，同刻再按 `rel` 倒序），跨多个允许根合并后统一排序。
- `name` = 相对**它所属允许根**的路径（用于展示；不同允许根下可重名，`id` 才是唯一键）。
- `mtime` = 本地时区 ISO8601（秒精度，与 `infohub.events.iso_from_epoch` 同形）。
- `thumb` 只在 `kind=image` 时出现（文档没有缩略图）。
- `nextCursor`：只在还有下一页时出现；把它原样回传给 `cursor` 取下一页。cursor 只是「上一页最后一项的排序键」的编码，**不是权限凭据**，伪造它最多改变翻页位置，不改变可见范围（每个请求都重新做 §3 的包含校验）。
- `enabled=false` ⇒ `404 {"error":"assets-disabled"}`。

## 3. `GET /asset/<id>` 与 `GET /thumbs/<id>`

- `/asset/<id>`：返回该文件的**原始字节**，`Content-Type` 取自 §5 的类型表，`Content-Length` 为真实字节数，`X-Content-Type-Options: nosniff`。
  `Content-Disposition`：**图片 `inline`**（浏览器里直接看）；**文档 `attachment`**（下载而不是在控制台同源渲染，避开 HTML/脚本注入面）。文件名同时给 ASCII 回退与 `filename*=UTF-8''…`。
- `/thumbs/<id>`：图片缩略图。**有 PIL 就现生成**（按 `ui.assets.thumb_width` 等比缩、`ui.assets.thumb_quality`，内存缓存 `ui.assets.thumb_cache` 条）；**没有 PIL 就回原图字节**，宽度约束交给浏览器（`<img>` + CSS `object-fit`），不引入新依赖。对文档 id 请求缩略图 ⇒ `404`。单张图解码失败 ⇒ fail-soft 回原图字节（不 500）。
- 两个路由都是只读（GET），不写文件、不写库、不留痕。
- `/thumbs/<id>` 是可选路由：页面用得上就有，403/404 语义与 `/asset/<id>` 完全一致。

## 4. id 编码规则（不可伪造的关键）

`id = base64url(数据根相对路径)`，去掉 `=` 填充，字符集严格 `[A-Za-z0-9_-]`。

- 编码的是**相对数据根**的路径（例：`screens/2026-11-03/084316.jpg`），不是绝对路径——**绝对路径从不进入响应**（列表、错误体、头，一律不回显）。
- 解码侧先做**字面校验**（这一步先于任何文件系统操作）：
  1. 非空、只含 `[A-Za-z0-9_-]`、能 base64url 解码成 UTF-8；
  2. 不以 `/` 开头（绝对路径）；
  3. 不含 NUL、反斜杠、首尾空白；
  4. 按 `/` 切段后，任何一段不得是空串、`.` 或 `..`（`a//b`、`a/./b`、`a/../b` 全部拒绝）。
- 通过字面校验后，服务端再把 `root/rel` **解析软链后的真实路径**与允许根的真实路径做包含校验；不包含 ⇒ `403`。
- 结论：客户端拿到 id 只能指向「允许根内、已存在的文件」；改一个字符最多得到 `403` / `404`。

## 5. 类型与大小上限

- 图片（`kind=image`）：`.jpg .jpeg .png .gif .webp .bmp`。**`.svg` 不在内**（同源内联 SVG 可执行脚本，宁可不看）。
- 文档（`kind=doc`）：`.md .txt .pdf .json .csv .log`。
- 扩展名不在上表 ⇒ 不列、取字节时 `403 {"error":"type-not-allowed"}`。
- 单文件上限 `ui.assets.max_bytes`（缺省 32 MiB）：列出来，取字节时 `413 {"error":"too-large"}`（不是 403/404——它不是越界，也不是不存在）。

| 键 | 语义 | 缺省回退 |
|---|---|---|
| `ui.assets.list_limit` | 列表缺省页大小（同时也是上限，1–200） | `60` |
| `ui.assets.max_bytes` | 单文件可下发的字节上限 | `33554432` |
| `ui.assets.max_scan` | 单次扫描候选文件上限（多个允许根合计） | `3000` |
| `ui.assets.thumb_width` | 缩略图目标宽（px，等比） | `360` |
| `ui.assets.thumb_quality` | 缩略图 JPEG 质量 | `72` |
| `ui.assets.thumb_cache` | 缩略图内存缓存条数 | `64` |
| `ui.assets.cache_s` | 列表结果缓存秒数（0 = 不缓存） | `5` |

## 6. 错误语义（机械可分）

| 情形 | 状态码 | 正文 |
|---|---|---|
| id 字面非法（非 base64url、含 `..`、绝对路径、反斜杠、空段） | `403` | `{"error":"bad-id"}` |
| 解析真实路径不在允许根内（含软链逃逸） | `403` | `{"error":"outside-root"}` |
| 扩展名不在白名单 | `403` | `{"error":"type-not-allowed"}` |
| 文件不存在 / 不是普通文件 / 缩略图请求的不是图片 | `404` | `{"error":"not-found"}` |
| 文件超过 `max_bytes` | `413` | `{"error":"too-large"}` |
| `kind` / `limit` / `cursor` 形态非法 | `400` | `{"error":"…"}` |
| `ui.assets.enabled=false` | `404` | `{"error":"assets-disabled"}` |

- **403 = 越界（可能被利用的入口），404 = 不存在（含「你猜对了目录但没有这个文件」）**。两者都不区分「根外存在」与「根外不存在」，不泄露目录结构。
- 错误体只有稳定的短错误码，**绝不含绝对路径、不含文件名、不含 errno 文本**。
- loopback 守卫（`Host` / `Origin` / `Sec-Fetch-Site`）对三条路由先生效，与 §4.8 一致。

## 7. 页面

`/` 的 HTML 里新增一个**全宽** `<section id="media">`，放在原两列之后：

- 图片：`<div id="mediagrid">` 网格，每格是 `<a href="/asset/<id>" target="_blank"><img loading="lazy" src="/thumbs/<id>">` + 文件名。
- 文档：`<div id="medialist">` 表格（名称 / 大小 / 时间 / 打开链接，链接到 `/asset/<id>`）。
- 纯 `fetch`，无框架；**媒体数据在首页 `/api/state` 之后异步加载**，缩略图 `loading="lazy"` ⇒ 不拖慢首页首屏。
- 不删改既有容器的 id / 选择器（`deliveries`、`stats`、`todos`、`text`、`revs` 等），老验收脚本照旧能断言。

## 8. 验收

`tests/check_ui_assets.sh`：隔离根（临时目录 + 改 `root:` 行）+ 临时端口（内核分配），自起自杀，不碰真库、不动常驻服务。
覆盖：列表形状、image 有 `url`/`thumb`、doc 有大小/时间、mtime 倒序、正常取字节（200 + Content-Type + 字节一致）、`../` 403、绝对路径 403、软链逃逸 403、不存在 404、错误体不回显绝对路径、页面含媒体容器。

## 9. 已知限制

- 扫描是「每次列表现扫」（带 `cache_s` 短缓存），没有索引；文件数超过 `max_scan` 时只列到上限，**不做全量**（本地单用户浏览足够；真上量再谈索引表）。
- 不做 Range 请求、不做 ETag/304；每次都是完整字节（大小受 `max_bytes` 约束）。
- 缩略图缓存是进程内的，重启即空；不落盘。
- 服务是单线程的（`HTTPServer`，见 §4.8 的理由）：下发大文件期间其它请求排队。文件大小受 `max_bytes` 约束，本地单用户浏览可接受。
- 缩略图/原图不参与页面首屏：媒体区块在 `/api/state` 之后单独 fetch，`<img loading="lazy">`。
- 文档一律 `attachment`（浏览器下载）；想内联预览（PDF 等）以后按需加白名单键，届时改本契约。
