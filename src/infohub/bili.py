"""来源条目 → 工具提取（M5/M5b）：从来源条目里机械提取「工具」（GitHub/GitLab 仓库），再拿去 KB 对账。

来源不是一个：M5b 起**实际在跑的是浏览器历史**（`collect.browser-history`，仓库页/视频页），
M5 的 bilibili 收藏导出通路（`collect.bilibili`）默认关、当前不产数据。

- 取数（`fetch`）**不写死在代码里**：`source: command` 跑一条命令（JSON 走 stdout）或
  `source: file` 读一份手动导出的 JSON——和归纳层同一个套路（换来源 = 换配置）。
  条目形态：`{"items": [{"id": "...", "title": "...", "url": "...", "desc": "..."}]}`
- 提工具（`extract_tools`）**只认机械形态**：GitHub/GitLab 链接、以及 `owner/repo` 形态的词。
  没有斜杠的裸名字**不猜**（误报代价是给用户派一张假待办）。
- 对账（KB 里有没有）在规则层做（`rules.bili-unintegrated`），本模块只管取数与提取。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

GITHUB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:github|gitlab)\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
#: `owner/repo` 形态（前后不能紧贴路径分隔符或词字符）
REPO_TOKEN_RE = re.compile(
    r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9_.-]{1,38})/([A-Za-z0-9][A-Za-z0-9_.-]{1,38})(?![\w./-])")
#: 明显不是仓库的片段（日期、纯数字、常见路径段）
BAD_SEGMENT_RE = re.compile(r"^(?:\d{1,4}|\d{1,2}\.\d{1,2})$")
#: 站点路径段（出现在 owner 位置时它不是账号名）：真机历史里 `github.com/settings/*`、
#: `login/*`、`users/*`、`topics/*`、`orgs/*` 都会被 `GITHUB_RE` 误认成 `owner/repo`
BAD_OWNERS = {"http", "https", "app", "www", "api", "cdn", "static", "user", "userid",
              "docs", "blog", "space", "video", "read", "search", "list", "top",
              "settings", "login", "logout", "signup", "join", "account", "sessions",
              "orgs", "users", "topics", "trending", "explore", "features", "pricing",
              "enterprise", "marketplace", "notifications", "collections", "apps",
              "sponsors", "about", "dashboard", "security", "codespaces", "site", "contact",
              # 真机反例：`github.com/password_reset/<200 多位 base32>`（一条失效的重置链接）
              "password_reset", "password", "reset"}
#: github.com 后面这些段不是仓库名
BAD_REPOS = {"blob", "tree", "issues", "pulls", "releases", "raw", "wiki", "actions",
             "sponsors", "settings", "topics", "about", "archive"}
#: GitHub 的真实长度上限（owner ≤ 39、repo ≤ 100）。超过的不是仓库名，是 token/链接残留
MAX_OWNER_LEN = 39
MAX_REPO_LEN = 100


class BiliError(Exception):
    """取数失败（命令不存在/退出码非 0/输出不是 JSON）。"""


def _norm_url(url: str) -> str:
    return url.strip().rstrip(".,)】」>\"'")


def extract_tools(items: list[dict]) -> list[dict]:
    """从条目里提出工具引用。返回 `[{key, name, owner, repo, url, item_id, title}]`，按 key 去重。"""
    found: dict[str, dict] = {}
    for item in items or []:
        url = _norm_url(str(item.get("url") or ""))
        if GITHUB_RE.search(url):
            # URL 本身就是仓库页 ⇒ **只认 URL**。页面标题/正文里的斜杠词不是仓库
            # （真机反例：网页标题 "A collection of notebooks/recipes …" 会被当成 notebooks/recipes）。
            text = url
            pairs = GITHUB_RE.findall(text)
        else:
            text = " ".join(str(item.get(k) or "") for k in ("title", "url", "desc", "text"))
            pairs = GITHUB_RE.findall(text) + REPO_TOKEN_RE.findall(text)
        for owner, repo in pairs:
            owner_l, repo_l = owner.lower(), repo.lower()
            if repo_l.endswith(".git"):
                repo, repo_l = repo[:-4], repo_l[:-4]
            if owner_l in BAD_OWNERS or repo_l in BAD_REPOS:
                continue
            if BAD_SEGMENT_RE.match(owner) or BAD_SEGMENT_RE.match(repo):
                continue
            if len(repo_l) < 3 or repo_l.endswith((".py", ".js", ".md", ".json", ".yaml", ".yml")):
                continue
            if len(owner) > MAX_OWNER_LEN or len(repo) > MAX_REPO_LEN:
                continue
            key = f"{owner_l}/{repo_l}"
            entry = found.setdefault(key, {
                "key": key, "name": f"{owner}/{repo}", "owner": owner, "repo": repo,
                "url": url or f"https://github.com/{owner}/{repo}",
                "item_id": str(item.get("id") or ""),
                "title": str(item.get("title") or "")[:120], "mentions": 0})
            entry["mentions"] += 1
            if not entry.get("item_id") and item.get("id"):
                entry["item_id"] = str(item["id"])
    return [found[k] for k in sorted(found)]


def load_items(payload) -> list[dict]:
    """把来源给的东西折成条目列表（宽容：也接受裸数组）。"""
    if isinstance(payload, list):
        return [it for it in payload if isinstance(it, dict)]
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("list") or []
        return [it for it in items if isinstance(it, dict)]
    raise BiliError("来源输出既不是数组也不是 {'items': [...]}")


def fetch(cfg: dict) -> list[dict]:
    """按配置取条目：`source: command` 跑 argv；`source: file` 读 JSON 文件。"""
    source = str(cfg.get("source") or "command").lower()
    if source == "file":
        path = Path(str(cfg.get("input") or "")).expanduser()
        if not path.is_file():
            raise BiliError(f"输入文件不存在: {path}")
        try:
            return load_items(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise BiliError(f"读输入文件失败: {exc}") from exc

    command = list(cfg.get("command") or [])
    if not command:
        raise BiliError("collect.bilibili.command 为空（没接来源）")
    timeout = float(cfg.get("timeout_s") or 60)
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise BiliError(f"来源命令不存在: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BiliError(f"来源命令超时（>{timeout:g}s）") from exc
    if proc.returncode != 0:
        raise BiliError(f"来源命令退出码 {proc.returncode}: {proc.stderr.strip()[:200]}")
    try:
        return load_items(json.loads(proc.stdout or "null"))
    except ValueError as exc:
        raise BiliError(f"来源输出不是 JSON: {proc.stdout[:200]!r}") from exc
