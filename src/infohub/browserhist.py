"""浏览器历史取数：把本机 Chromium 系浏览器（Edge/Chrome/Chromium）的 History 库折成条目。

三类条目：
- `repo`  —— github/gitlab 的**仓库页**（M5b；URL 里天然带 owner/repo，零猜测）
- `video` —— bilibili **视频页**（M5b）
- `history` —— **全部**浏览记录（M8，用户 2026-10-16 定：「只留历史记录 + 一条总结」）；
  每条只留时间、网址、站点、用途、标题前 120 字，分类口径在 `history.py`

一旦 `kinds` 里含 `history`，它是**兜底类**：同一行历史只产出一条 `history` 条目（不会再产
`repo`/`video`），避免同一次访问在库里记两遍。History 存着全部浏览记录（敏感），所有条目事件
都按 `privacy="sensitive"` 入库；用途分类与总结**只进报表，不推送**。

取数**只读**：先把 History（连同 `-wal`/`-shm`）拷进临时目录，再以 `mode=ro` 打开**副本**——
不碰正在运行的浏览器，也不动它的库文件。语义见 INTERFACE.md §4.12。
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bili
from . import history as H

#: 浏览器名 → `~/.config/<目录名>`
BROWSER_DIRS = {"edge": "microsoft-edge", "chrome": "google-chrome", "chromium": "chromium"}
#: Chromium 的 `last_visit_time` 是 1601-01-01 起的**微秒**（不是 Unix 秒）
CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
VIDEO_RE = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)", re.I)
#: 站点标题尾巴（"..._哔哩哔哩_bilibili"），只是展示噪声，去掉后 KB 更好对
TITLE_TAIL_RE = re.compile(r"[\s|_-]*[_|]?\s*(?:哔哩哔哩|bilibili)(?:_bilibili)?\s*$", re.I)
#: 条目类别；`history` 是兜底类（见模块 docstring）
KINDS = ("repo", "video", "history")


class HistError(Exception):
    """历史库读不了（不存在 / 拷不出来 / 不是 Chromium 历史库）。"""


def root_dir(browser: str = "edge") -> Path:
    name = BROWSER_DIRS.get(str(browser or "edge").lower())
    if not name:
        raise HistError(f"不认识的浏览器：{browser}（支持 {'/'.join(sorted(BROWSER_DIRS))}）")
    return Path("~/.config").expanduser() / name


def db_path(cfg: dict) -> Path:
    """按配置定位 History 库：`db` 给了就用它；否则 `~/.config/<browser>/<profile>/History`，
    `profile` 为空 ⇒ 取 History 最新的那个档案目录。"""
    explicit = str(cfg.get("db") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    root = root_dir(str(cfg.get("browser") or "edge"))
    profile = str(cfg.get("profile") or "").strip()
    if profile:
        return root / profile / "History"
    cands = [p for p in root.glob("*/History") if p.is_file()]
    if not cands:
        raise HistError(f"没找到浏览历史库（{root}/*/History）")
    return max(cands, key=lambda p: p.stat().st_mtime)


def ts_iso(us: int) -> str:
    """Chromium 微秒时间戳 → 本地 ISO8601（与 `events.iso` 同口径）。"""
    return (CHROME_EPOCH + timedelta(microseconds=int(us))).astimezone().isoformat(timespec="seconds")


def read_rows(path: Path) -> list[dict]:
    """只读读一遍 `urls` 表（经临时副本，避免锁住浏览器正在用的库）。"""
    if not path.is_file():
        raise HistError(f"历史库不存在：{path}")
    tmp = Path(tempfile.mkdtemp(prefix="infohub-hist-"))
    try:
        for name in (path.name, path.name + "-wal", path.name + "-shm"):
            src = path.parent / name
            if src.is_file():
                shutil.copy2(src, tmp / name)
        try:
            con = sqlite3.connect(f"file:{tmp / path.name}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise HistError(f"打不开历史库副本：{exc}") from exc
        try:
            raw = con.execute(
                "SELECT url, title, last_visit_time, visit_count FROM urls").fetchall()
        except sqlite3.Error as exc:
            raise HistError(f"读 urls 表失败（不是 Chromium 历史库？）：{exc}") from exc
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return [{"url": str(r[0] or ""), "title": str(r[1] or ""),
             "last_visit_time": int(r[2] or 0), "visit_count": int(r[3] or 0)} for r in raw]


def video_item(row: dict) -> dict | None:
    m = VIDEO_RE.search(row["url"])
    if not m:
        return None
    vid = m.group(1)
    return {"id": f"video:{vid}", "kind": "video",
            "title": TITLE_TAIL_RE.sub("", row["title"]).strip()[:200],
            "url": f"https://www.bilibili.com/video/{vid}", "desc": "",
            "visits": row["visit_count"], "ts": ts_iso(row["last_visit_time"])}


def repo_item(row: dict) -> dict | None:
    """仓库页 → 条目。`owner/repo` 直接从 URL 来，**不猜**；过滤明显不是仓库的段（复用 bili 的口径）。"""
    m = bili.GITHUB_RE.search(row["url"])
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    owner_l, repo_l = owner.lower(), repo.lower()
    if owner_l in bili.BAD_OWNERS or repo_l in bili.BAD_REPOS:
        return None
    if bili.BAD_SEGMENT_RE.match(owner) or bili.BAD_SEGMENT_RE.match(repo):
        return None
    host = "gitlab.com" if "gitlab.com" in row["url"].lower() else "github.com"
    return {"id": f"repo:{owner_l}/{repo_l}", "kind": "repo",
            "title": f"{owner}/{repo}", "url": f"https://{host}/{owner}/{repo}",
            "desc": row["title"][:200], "visits": row["visit_count"],
            "ts": ts_iso(row["last_visit_time"])}


def history_item(row: dict, title_limit: int = 120) -> dict | None:
    """任意一条历史 → `history` 条目（M8）：时间 + 网址 + 站点 + 用途 + 标题前 `title_limit` 字。

    网址为空的记录直接丢（没有可留的东西）；分类口径全在 `history.py`，这里不做判断。
    """
    url = str(row.get("url") or "").strip()
    if not url:
        return None
    info = H.classify(url, str(row.get("title") or ""))
    return {"id": H.visit_id(url), "kind": "history",
            "title": H.clean_title(row.get("title"), limit=title_limit),
            "url": url[:500], "desc": "", "visits": row.get("visit_count") or 0,
            "site": info["site"], "purpose": info["purpose"],
            "ts": ts_iso(row["last_visit_time"])}


def collect_items(*, db: Path, since_days: float = 14, max_items: int = 200,
                  kinds: tuple = KINDS, now: datetime | None = None,
                  title_limit: int = 120) -> list[dict]:
    """把历史折成条目。

    同一 id（同一网址 / 同一个视频 / 同一个仓库）只留**最近一次**访问；`since_days` 之前的记录
    不看；`max_items` 是**每类**上限（否则视频会把仓库挤出窗口）。`history` 一旦在 `kinds` 里
    就是兜底类：命中它的行不再产 `repo`/`video`。返回顺序 = `kinds` 顺序，类内按时间倒序。
    """
    want = [str(k).lower() for k in (kinds or KINDS)]
    now = now or datetime.now().astimezone()
    cut_us = int((((now - timedelta(days=float(since_days))).astimezone(timezone.utc))
                  - CHROME_EPOCH).total_seconds() * 1_000_000) if float(since_days) > 0 else 0

    best: dict[str, dict] = {}
    for row in read_rows(db):
        if cut_us and row["last_visit_time"] < cut_us:
            continue
        item = None
        if "history" in want:
            item = history_item(row, title_limit=title_limit)
        if item is None and "video" in want:
            item = video_item(row)
        if item is None and "repo" in want:
            item = repo_item(row)
        if item is None:
            continue
        prev = best.get(item["id"])
        if prev is None or row["last_visit_time"] > prev["_us"]:
            item["_us"] = row["last_visit_time"]
            best[item["id"]] = item

    out: list[dict] = []
    for kind in want:
        group = sorted([it for it in best.values() if it["kind"] == kind],
                       key=lambda it: -it["_us"])[:max(0, int(max_items))]
        for it in group:
            it.pop("_us", None)
        out += group
    return out
