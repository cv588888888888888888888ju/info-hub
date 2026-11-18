"""浏览历史分类与总结（M8）。

用户 2026-10-16 定的口径：**来源覆盖全部浏览历史**（不再只挑仓库页/视频页两类）；
每条只留「历史记录（时间 + 网址）+ 一条总结」；总结由**用途**与**网址**两个维度机械分类得出；
每条都要过一遍分类检查，**但这类信息只进报表与总结，不推送**（用户原话：「卡片不推送」）。

本模块是纯函数层：不碰数据库、不看时钟、不发事件。分类规则按「顺序即优先级」匹配，
未知一律进 `其它`（不猜）。语义见 INTERFACE.md §4.14、PROJECT.md §36。
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlsplit

#: 用途分类（顺序无关，只用于展示与校验）
PURPOSES = (
    "本机服务", "代码仓库", "AI 服务", "技术文档/博客", "社区/论坛", "视频/音频",
    "小说/阅读", "学习/课程", "游戏", "招聘/求职", "购物/采购", "网盘/文件",
    "资讯/新闻", "账号/邮箱", "搜索", "其它",
)
#: 未知用途的落点（不猜）
OTHER = "其它"
#: 只有这些用途值得拿去和知识库对账（其余只做统计，见 §36）
KB_RELEVANT = ("代码仓库", "技术文档/博客", "AI 服务")

#: 多段公共后缀（取站点名时要多切一段）
MULTI_TLDS = {"co.uk", "org.uk", "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
              "com.tw", "org.tw", "co.jp", "com.au", "com.hk", "co.kr"}
#: 站点名规则（先匹配**完整 host**，再匹配站点名；顺序即优先级）
DOMAIN_RULES: tuple[tuple[str, str], ...] = (
    ("搜索", r"^(www\.)?(google|bing|baidu|duckduckgo|sogou|ecosia|yandex)\.[a-z.]+$"),
    ("本机服务", r"^(127\.0\.0\.1|0\.0\.0\.0|localhost|\[::1\])$"),
    ("代码仓库", r"^(github|gitlab|gitee|codeberg|bitbucket)\.(com|io|org)$|^gist\.github\.com$|^github\.io$"),
    ("AI 服务", r"deepseek|openai|chatgpt|claude\.ai|anthropic|gemini|aistudio|siliconflow|openrouter"
                r"|huggingface|modelscope|opencode\.ai|ollama|mistral|groq|together\.ai|perplexity"
                r"|z\.ai|cctq\.ai|iflow\.cn|qianwenai|xiaomimimo|helpaio|poe\.com|moonshot|kimi"
                r"|typesafe\.ai|vibe-x\.app|trae\.ai|cursor\.com|windsurf"),
    ("技术文档/博客", r"^docs\.|readthedocs\.io$|^developer|mozilla\.org|wikipedia|^wiki|stackoverflow"
                      r"|python\.org|rust-lang|kernel\.org|man7|archlinux|csdn\.net|juejin|cnblogs"
                      r"|segmentfault|nvidia\.com|\.dev$|baeldung|spring\.io|kubernetes\.io"),
    ("社区/论坛", r"linux\.do|reddit|zhihu|v2ex|nodeseek|tieba|weibo|discord|t\.me|telegram|twitter"
                  r"|hostloc|52pojie|bbs|huxiu|sspai|medium\.com|juejin\.cn"),
    ("视频/音频", r"bilibili|youtube|spotify|netflix|music\.163|y\.qq\.com|douyin|kuaishou"),
    ("小说/阅读", r"novalpie|esjzone|qidian|zongheng|fanqie|ciweimao|linovelib|wenku|17k|jjwxc"),
    ("学习/课程", r"pintia|edu\.cn|mooc|coursera|edx|icourse163|xuetang|leetcode|nowcoder|lanqiao"
                  r"|chaoxing|xuexi\.cn|zhihuishu"),
    ("游戏", r"mcmod|steam|nexusmods|curseforge|game|4399"),
    ("招聘/求职", r"careersky|zhipin|lagou|boss\.com|51job|linkedin|shixiseng|nowcoder\.com/jobs"),
    ("购物/采购", r"taobao|tmall|jd\.com|amazon|aliexpress|pinduoduo|hostyun|dmit|aliyun|tencentcloud"
                  r"|koyeb|vultr|digitalocean|namecheap|cloudflare|bwh81|bandwagon|mi\.com|xiaomi"),
    ("网盘/文件", r"drive\.google|pan\.baidu|aliyundrive|123pan|onedrive|dropbox|mega\.nz"),
    ("资讯/新闻", r"ithome|36kr|cnbeta|hackernews|news\.ycombinator|theverge"),
    ("账号/邮箱", r"^(live|outlook|hotmail|gmail|mail|accounts|login|passport|alipay)\.|^qq\.com$"
                  r"|^microsoft\.com$|^apple\.com$"),
)
#: 路径规则（匹配**整条 URL**，用于站内分流，优先级低于站点规则）
PATH_RULES: tuple[tuple[str, str], ...] = (
    ("搜索", r"(google|bing|baidu|duckduckgo|sogou|ecosia)\.[a-z.]+/(search|s\b|s\?|webhp)"
             r"|/scholar|^baidu\.com/?$"),
    ("账号/邮箱", r"/login|/signin|/signup|/oauth|password_reset|/session"),
)

_VIDEO_RE = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)", re.I)
_TAIL_RE = re.compile(r"[\s|_-]*[_|]?\s*(?:哔哩哔哩|bilibili)(?:_bilibili)?\s*$", re.I)
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class HistoryError(Exception):
    """分类/取数失败（URL 解析不了等）。"""


def site(url: str) -> str:
    """URL → 站点名（`https://a.b.example.co.uk/x` ⇒ `example.co.uk`；IP/localhost 原样）。"""
    host = urlsplit(str(url or "").strip()).hostname or ""
    host = host.lower()
    if not host:
        return ""
    if _IP_RE.match(host) or host in ("localhost", "::1"):
        return host
    parts = host.split(".")
    if len(parts) < 2:
        return host
    two = ".".join(parts[-2:])
    if two in MULTI_TLDS and len(parts) > 2:
        return ".".join(parts[-3:])
    return two


def purpose(url: str, title: str = "") -> str:
    """URL → 用途（host 规则 → 站点规则 → 路径规则 → `其它`，不猜）。"""
    u = str(url or "").strip()
    host = (urlsplit(u).hostname or "").lower()
    s = site(u)
    for name, pat in DOMAIN_RULES:
        if host and re.search(pat, host):
            return name
    for name, pat in DOMAIN_RULES:
        if s and re.search(pat, s):
            return name
    low = u.lower()
    for name, pat in PATH_RULES:
        if re.search(pat, low):
            return name
    return OTHER


def classify(url: str, title: str = "") -> dict:
    """一条历史记录 → `{"site", "purpose"}`。"""
    return {"site": site(url), "purpose": purpose(url, title)}


def visit_id(url: str) -> str:
    """历史条目的稳定 id（同一条网址只算一条，与访问次数无关）。"""
    return "visit:" + hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:16]


def clean_title(title: str, limit: int = 120) -> str:
    """标题：去掉站点尾巴、折成一行、截断到 limit 字。"""
    t = _TAIL_RE.sub("", str(title or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[: max(0, int(limit))]


def one_line(ts: str, item: dict) -> str:
    """一条总结：`时间 · 站点 · 用途 · 「标题」`。"""
    title = item.get("title") or ""
    tail = f"「{title}」" if title else ""
    return " · ".join(x for x in (str(ts or "")[:16], item.get("site") or "",
                                  item.get("purpose") or OTHER, tail) if x)


def summarize(store, config=None, days: float = 1, now: datetime | None = None,
              top: int = 10) -> dict:
    """窗口内的浏览历史 → 用途/站点分布 + **一条总结**（不产卡片、不推送）。"""
    now = now or datetime.now().astimezone()
    since = (now - timedelta(days=float(days))).isoformat(timespec="seconds")
    # `store.events` 一次只认一个 source（见 INTERFACE §5.1）；历史只有这一个 source
    rows = store.events(limit=100000, source="browser-history", since=since)
    by_purpose: Counter = Counter()
    by_site: Counter = Counter()
    seen: set[str] = set()
    for row in rows:
        p = dict(row.get("payload") or {})
        url = str(p.get("url") or "")
        vid = str(p.get("id") or "") or visit_id(url)
        if vid in seen:                      # 同一网址只算一次（历史里可能多条）
            continue
        seen.add(vid)
        info = classify(url, str(p.get("title") or ""))
        pr = str(p.get("purpose") or info["purpose"])
        st = str(p.get("site") or info["site"])
        by_purpose[pr] += 1
        if st:
            by_site[st] += 1
    total = sum(by_purpose.values())
    head = "、".join(f"{k} {v}" for k, v in by_purpose.most_common(4))
    line = (f"最近 {days:g} 天浏览 {total} 个页面（去重后）：{head}" if total
            else f"最近 {days:g} 天没有浏览记录")
    return {
        "days": float(days), "since": since, "total": total,
        "by_purpose": dict(by_purpose.most_common()),
        "top_sites": [{"site": s, "count": n} for s, n in by_site.most_common(max(1, int(top)))],
        "line": line,
    }


def render_block(store, config=None, days: float = 1) -> str:
    """报表区块（markdown）：一句话总结 + 用途分布 + 主力站点。"""
    data = summarize(store, config, days=days)
    out = [f"### 浏览历史（最近 {days:g} 天）", "", data["line"], ""]
    if data["total"]:
        out += ["| 用途 | 页面数 |", "| --- | --- |"]
        out += [f"| {k} | {v} |" for k, v in data["by_purpose"].items()]
        out += ["", "主力站点：" + "、".join(f"{s['site']}({s['count']})"
                                            for s in data["top_sites"][:10]), ""]
    return "\n".join(out)
