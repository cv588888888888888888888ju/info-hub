"""报表引擎（M3）。契约见 INTERFACE.md §4.7。

分离原则（用户 m01271：报表和系统数据分离、报表我能手动改、每次改动有记录）：

- 报表文件**是人拥有的**：自由文字机器永不覆盖。
- 机器只重写 `<!-- hub:auto:begin X --> … <!-- hub:auto:end X -->` 区块之间的内容。
- 任何一次写入（机器生成 / 人手改）都追加一条 `report_revisions`（author + 前后 hash + diff）。
- 内容没变就不写、不留痕（幂等）；报表路径只允许落在 `reports_dir` 下。

报表**只读**系统数据，绝不回写 `events` / `todos` / `notifications`。
"""

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path

from infohub import events as E

BLOCK_RE = re.compile(
    r"<!--\s*hub:auto:begin\s+([A-Za-z0-9_-]+)\s*-->\n?(.*?)\n?<!--\s*hub:auto:end\s+\1\s*-->",
    re.S)
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

HEADER = """# info-hub 报表

> 这份报表是**你的**：`hub:auto` 区块之间的内容由机器重写，区块外的文字永不覆盖。
> 每次改动（机器或人）都会进 `report_revisions`，用 `bin/hub-report revisions` 查。
"""


class ReportError(Exception):
    """报表层错误（名字非法 / 越出报表目录 / 写失败）。"""


# --------------------------------------------------------------------------- 区块渲染
def render_stats(store, config) -> str:
    data = store.stats()
    lines = [f"- 事件总数：**{data['events_total']}**（{data['events_first'] or '-'} → {data['events_last'] or '-'}）",
             f"- 库：`{data['db']}`（{data['db_bytes']} 字节）", ""]
    lines.append("| 来源 | 条数 |")
    lines.append("|---|---|")
    for src, n in sorted(data["per_source"].items()):
        lines.append(f"| {src} | {n} |")
    bad = [r for r in data["collector_state"] if r["last_error"]]
    lines.append("")
    if bad:
        lines.append("采集器异常：")
        lines += [f"- `{r['key']}`：{r['last_error'][:120]}" for r in bad]
    else:
        lines.append("采集器：全部正常。")
    return "\n".join(lines)


def render_todos(store, config) -> str:
    limit = int(config.get("report.todos_limit") or 30)
    rows = [r for r in store.todos(limit=limit) if r["status"] in ("open", "doing")]
    if not rows:
        return "（没有未完成待办）"
    out = ["| id | 状态 | 标题 | 依据 |", "|---|---|---|---|"]
    for row in rows:
        tag = "（人改）" if row["human_override"] else ""
        out.append(f"| `{row['id']}` | {row['status']}{tag} | {row['title'][:80]} |"
                   f" {len(row['source_event_ids'])} 条 |")
    return "\n".join(out)


def render_notifications(store, config) -> str:
    limit = int(config.get("report.notifications_limit") or 10)
    rows = store.notifications(limit=limit)
    if not rows:
        return "（还没有通知记录）"
    out = ["| 时间 | 类别 | 结果 | 内容 |", "|---|---|---|---|"]
    for row in rows:
        out.append(f"| {row['ts']} | {row['kind']} | {'已弹' if row['shown'] else '失败'} |"
                   f" {row['body'][:80]} |")
    return "\n".join(out)


def render_digest(store, config) -> str:
    """M4b：最近一次 LLM 归纳（区块内容由 `digest` 视图插件给，只读库）。

    归纳插件未启用/坏了 ⇒ 区块里**显式**写一句占位：不让整份报表生成挂掉，也不静默留空。
    """
    from infohub import views
    try:
        digest = views.module(config, "digest")
    except views.ViewError as exc:
        return f"_（归纳视图插件不可用：{exc}）_\n"
    return digest.render_block(store, config)


def render_history(store, config) -> str:
    """M8：浏览历史的一句话总结 + 用途分布 + 主力站点（只读库；这类信息只进报表、不推送）。"""
    from infohub import history as H
    days = float(config.get("report.history_days") or 1)
    return H.render_block(store, config, days=days)


BLOCKS = {"stats": render_stats, "todos": render_todos, "notifications": render_notifications,
          "digest": render_digest, "history": render_history}


def render_blocks(store, config) -> dict[str, str]:
    return {name: BLOCKS[name](store, config) for name in BLOCKS}


# --------------------------------------------------------------------------- 合并
def _block_text(name: str, body: str) -> str:
    return f"<!-- hub:auto:begin {name} -->\n{body.strip()}\n<!-- hub:auto:end {name} -->"


def merge(old_text: str, rendered: dict[str, str], order: list[str]) -> str:
    """只替换已知自动区块的内容；区块外的文字按原样保留；缺的区块补在末尾。"""
    if not old_text.strip():
        parts = [HEADER.rstrip(), ""]
        parts += [_block_text(n, rendered[n]) for n in order if n in rendered]
        return "\n\n".join(parts) + "\n"

    seen: set[str] = set()
    out: list[str] = []
    pos = 0
    for match in BLOCK_RE.finditer(old_text):
        out.append(old_text[pos:match.start()])
        name = match.group(1)
        seen.add(name)
        body = rendered.get(name, match.group(2))
        out.append(_block_text(name, body) if name in rendered else match.group(0))
        pos = match.end()
    out.append(old_text[pos:])
    text = "".join(out)
    missing = [n for n in order if n in rendered and n not in seen]
    if missing:
        text = text.rstrip("\n") + "\n\n" + "\n\n".join(_block_text(n, rendered[n]) for n in missing) + "\n"
    return text


# --------------------------------------------------------------------------- 路径与写入
def report_path(config, name: str | None = None) -> Path:
    raw = name or str(config.get("report.name") or "hub-report")
    if not NAME_RE.match(raw or ""):
        raise ReportError(f"报表名非法（只允许字母数字._-）: {raw!r}")
    path = (config.reports_dir / f"{raw}.md").resolve()
    root = config.reports_dir.resolve()
    if path.parent != root:
        raise ReportError(f"报表路径越出报表目录: {path}")
    return path


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _diff(before: str, after: str, max_chars: int) -> str:
    diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                          "before", "after", lineterm="", n=2))
    return diff[:max_chars]


def write_report(config, store, text: str, author: str, reason: str = "",
                 name: str | None = None) -> dict:
    """落盘 + 留痕。内容没变则什么都不做（幂等）。"""
    path = report_path(config, name)
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    before_hash, after_hash = _hash(before), _hash(text)
    if before_hash == after_hash:
        return {"report": path.stem, "path": str(path), "changed": False, "author": author,
                "before_hash": before_hash, "after_hash": after_hash, "rev": None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    rev = store.add_report_revision(path.stem, author, before_hash, after_hash,
                                    _diff(before, text, int(config.get("report.diff_max_chars") or 4000)),
                                    reason)
    return {"report": path.stem, "path": str(path), "changed": True, "author": author,
            "before_hash": before_hash, "after_hash": after_hash, "rev": rev}


def read_report(config, name: str | None = None) -> str:
    path = report_path(config, name)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def generate(config, store, reason: str = "generate", name: str | None = None) -> dict:
    """按系统数据重写自动区块（author=hub）。"""
    path = report_path(config, name)
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    order = [str(b) for b in (config.get("report.blocks") or list(BLOCKS))]
    merged = merge(old, render_blocks(store, config), order)
    result = write_report(config, store, merged, author="hub", reason=reason, name=name)
    result["at"] = E.iso()
    return result


def revisions(config, store, name: str | None = None, limit: int = 20) -> list[dict]:
    path = report_path(config, name)
    return store.report_revisions(path.stem)[-int(limit):][::-1]
