"""周报（M4c）：把归纳条目 / 待办 / 通知按周聚合，落一份**只读**周报文件。

契约见 INTERFACE.md §4.10。纪律（与 M0 无关，但同样硬）：
- 只读库、只写 `reports/hub-week-<ISO年>-W<周>.md`，**不碰**可手改的 `hub-report.md`；
- 周界 = 本地时区的 ISO 周（周一 00:00 起，半开区间 `start <= ts < end`）；
- 文件名按**区间最后一天**所在的 ISO 周（`hub-week-<年>-W<周>.md`）；`--weeks 2` 的连报也落在当周文件；
- 每条归纳条目照样带 `evidence`（真实事件 id），没依据的不进周报。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

#: 周报文件名模板
WEEK_FILE = "hub-week-{year}-W{week:02d}.md"


class WeekError(Exception):
    """周报层失败（配置/时间参数不合法）。"""


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return out.astimezone() if out.tzinfo else out


def week_bounds(weeks: int = 1, week_of: str | None = None,
                now: datetime | None = None) -> tuple[datetime, datetime]:
    """返回 `(start, end)`：`start` = 目标周周一 00:00（本地时区），`end = start + 7*weeks 天`。

    `week_of` 给该周内任意一天（`YYYY-MM-DD`）；不给则用「今天所在的那一周」。
    `weeks=2` 表示连报两周（从上一周的周一起算）。
    """
    weeks = int(weeks)
    if weeks < 1:
        raise WeekError("--weeks 必须 >= 1")
    if week_of:
        anchor = _dt(week_of)
        if anchor is None:
            raise WeekError(f"--week-of 不是日期：{week_of!r}")
    else:
        anchor = now or datetime.now().astimezone()
    if anchor.tzinfo is None:
        anchor = anchor.astimezone()
    monday = (anchor - timedelta(days=anchor.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = monday - timedelta(days=7 * (weeks - 1))
    return start, start + timedelta(days=7 * weeks)


def build(store, config, weeks: int = 1, week_of: str | None = None,
          now: datetime | None = None) -> dict:
    """组装周报数据（不写任何东西）。"""
    start, end = week_bounds(weeks=weeks, week_of=week_of, now=now)
    iso_start, iso_end = start.isoformat(), end.isoformat()
    # 文件名按**区间最后一天**所在的 ISO 周：连报多周也落在当周文件，不会倒回去覆盖历史周文件
    last = end - timedelta(seconds=1)
    now_dt = (now or datetime.now().astimezone())
    if now_dt.tzinfo is None:
        now_dt = now_dt.astimezone()

    counts = store.event_counts(since=iso_start, until=iso_end)
    stamp = lambda v: _dt(v)  # noqa: E731
    in_range = lambda v: (stamp(v) is not None and start <= stamp(v) < end)  # noqa: E731

    created = [t for t in store.todos(limit=100000) if in_range(t.get("created_at"))]
    done = [t for t in store.todos(status="done", limit=100000) if in_range(t.get("updated_at"))]
    open_now = store.todos(status="open", limit=100000)
    shown = [n for n in store.notifications(limit=100000, shown_only=True) if in_range(n.get("ts"))]
    failed = [n for n in store.notifications(limit=100000) if in_range(n.get("ts")) and not n.get("shown")]

    eff_days = max(1, (now_dt - start).days)
    effectiveness = store.notification_effectiveness(window_days=eff_days)

    digests = store.digests(limit=1000, since=iso_start)
    week_digests = [d for d in digests if in_range(d.get("ts")) and stamp(d.get("ts")) < end]
    items: list[dict] = []
    for digest in week_digests:
        for item in digest.get("items") or []:
            items.append({"text": item.get("text", ""), "evidence": item.get("evidence") or [],
                          "digest_id": digest.get("id"), "ts": digest.get("ts")})

    return {
        "weeks": int(weeks), "start": iso_start, "end": iso_end,
        "iso_year": last.isocalendar().year, "iso_week": last.isocalendar().week,
        "event_counts": counts, "event_total": sum(counts.values()),
        "todos_created": len(created), "todos_done": len(done), "todos_open": len(open_now),
        "todos_created_keys": [t.get("dedupe_key") for t in created],
        "todos_done_keys": [t.get("dedupe_key") for t in done],
        "notifications_shown": len(shown), "notifications_failed": len(failed),
        "effectiveness_days": eff_days, "effectiveness": effectiveness,
        "digests": [{"id": d.get("id"), "ts": d.get("ts"), "window_days": d.get("window_days"),
                     "backend": d.get("backend"), "articles": len(d.get("items") or []),
                     "dropped": d.get("dropped")} for d in week_digests],
        "items": items, "items_total": len(items),
        "generated_at": now_dt.isoformat(timespec="seconds"),
    }


def render(data: dict) -> str:
    """把 `build()` 的数据渲染成周报 markdown。"""
    lines = [
        f"# 信息中枢周报 · {data['iso_year']}-W{data['iso_week']:02d}",
        "",
        f"> 区间 `{data['start']}` → `{data['end']}`（本地时区，半开）；"
        f"生成于 `{data['generated_at']}`。",
        "> 本文件由 `hub-week` 自动生成，是**只读产物**；要手改的是报表 `hub-report.md`（那份才留痕）。",
        "",
        "## 概览",
        "",
        "| 项 | 数 |",
        "|---|---|",
        f"| 入库事件 | {data['event_total']} |",
        f"| 新增待办 | {data['todos_created']} |",
        f"| 完成待办 | {data['todos_done']} |",
        f"| 仍未处理待办（全局） | {data['todos_open']} |",
        f"| 弹出通知 | {data['notifications_shown']} |",
        f"| 发送失败 | {data['notifications_failed']} |",
        f"| 归纳次数 | {len(data['digests'])} |",
        f"| 归纳条目 | {data['items_total']} |",
        "",
        "## 本周归纳条目（每条带依据）",
        "",
    ]
    if data["items"]:
        for item in data["items"]:
            ev = ",".join(f"#{e}" for e in item["evidence"]) or "（无）"
            lines.append(f"- {item['text']}  <small>evidence: {ev} · 归纳 #{item['digest_id']}</small>")
    else:
        lines.append("_本周没有归纳记录（`hub-digest run` 未跑）或归纳结果为空。_")
    lines += ["", "## 事件来源", "", "| 来源 | 条数 |", "|---|---|"]
    lines += [f"| {src} | {n} |" for src, n in sorted(data["event_counts"].items())] \
        or ["| （无） | 0 |"]
    lines += ["", f"## 通知有效性（截至今天，近 {data['effectiveness_days']} 天）", "",
              "| 类别 | 弹出 | 已处理 | 忽略 | 待判 | 无指针 | 忽略率 |", "|---|---|---|---|---|---|---|"]
    if data["effectiveness"]:
        for row in data["effectiveness"]:
            rate = "-" if row["ignored_rate"] is None else f"{row['ignored_rate']:.0%}"
            lines.append(f"| {row['kind']} | {row['shown']} | {row['acted']} | {row['ignored']} |"
                         f" {row['pending']} | {row['unlinked']} | {rate} |")
    else:
        lines.append("| （窗口内没有已弹通知） | 0 | 0 | 0 | 0 | 0 | - |")
    lines.append("")
    return "\n".join(lines)


def week_path(config, data: dict) -> Path:
    return Path(config.reports_dir) / WEEK_FILE.format(year=data["iso_year"], week=data["iso_week"])


def write(config, data: dict) -> Path:
    path = week_path(config, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    return path


def run(config, store, weeks: int = 1, week_of: str | None = None,
        dry_run: bool = False) -> dict:
    """生成一份周报（`dry_run=True` 只返回文本，不落盘）。"""
    data = build(store, config, weeks=weeks, week_of=week_of)
    if dry_run:
        data["reason"] = "dry-run"
        data["text"] = render(data)
        return data
    data["path"] = str(write(config, data))
    data["reason"] = "ok"
    return data


def list_weeks(config) -> list[dict]:
    """列出已生成的周报文件（新的在前）。"""
    out = []
    for path in sorted(Path(config.reports_dir).glob("hub-week-*.md"), reverse=True):
        stat = path.stat()
        out.append({"name": path.name, "path": str(path), "bytes": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone()
                    .isoformat(timespec="seconds")})
    return out
