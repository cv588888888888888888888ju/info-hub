"""规则引擎（M1，出口 A）。契约见 INTERFACE.md §4.5；阈值只在 hub.config.yaml 的 `rules.<name>.*`。

铁律：规则**只读** events，只写 todos（L2）；永不改报表（L3，见 PROJECT.md §5.1）。
"""

from __future__ import annotations

from infohub import bili, kb

import re
from datetime import datetime, timedelta
from pathlib import Path

RULES: dict = {}
DOMAIN_RE = re.compile(r"https?://([^/\s:]+)")


class RuleError(Exception):
    pass


def rule(name: str):
    def deco(fn):
        RULES[name] = fn
        return fn
    return deco


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _enabled(config, name: str) -> bool:
    return bool(config.get(f"rules.{name}.enabled", True))


def _val(config, name: str, key: str, default):
    return config.get(f"rules.{name}.{key}", default)


def _candidate(rule_name: str, subject: str, bucket: str, title: str, why: str,
               confidence: float, event_ids: list) -> dict:
    return {"rule": rule_name, "dedupe_key": f"{rule_name}:{subject}:{bucket}",
            "title": title, "why": why, "confidence": confidence,
            "source_event_ids": sorted(set(event_ids))}


# ---------------------------------------------------------------- 规则
@rule("fs-repeat")
def fs_repeat(store, config, now) -> list[dict]:
    hours = float(_val(config, "fs-repeat", "window_hours", 24))
    min_count = int(_val(config, "fs-repeat", "min_count", 3))
    since = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = store.events(limit=int(_val(config, "fs-repeat", "scan_limit", 5000)), source="fs", since=since)

    groups: dict[str, list[dict]] = {}
    for row in rows:
        path = (row.get("payload") or {}).get("path")
        if path:
            groups.setdefault(path, []).append(row)

    out = []
    for path, items in groups.items():
        if len(items) < min_count:
            continue
        newest = max(items, key=lambda r: r["ts"])
        bucket = _parse(newest["ts"]).date().isoformat()
        out.append(_candidate(
            "fs-repeat", path, bucket,
            f"「{path}」{hours:g} 小时内改了 {len(items)} 次",
            "同一文件反复改动，像是没收尾的活", 0.6, [r["id"] for r in items]))
    return out


@rule("session-stale")
def session_stale(store, config, now) -> list[dict]:
    hours = float(_val(config, "session-stale", "stale_hours", 48))
    settled = tuple(_val(config, "session-stale", "settled_kinds", ["turn/end"]) or [])
    fresh = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = store.events(limit=int(_val(config, "session-stale", "scan_limit", 20000)), source="dsh-session")

    newest_per_session: dict[str, dict] = {}
    for row in rows:                     # 已按 id 倒序
        sid = (row.get("payload") or {}).get("session_id")
        if sid and sid not in newest_per_session:
            newest_per_session[sid] = row

    out = []
    for sid, row in newest_per_session.items():
        if row["ts"] >= fresh or row["kind"] in settled:
            continue
        bucket = _parse(row["ts"]).date().isoformat()
        out.append(_candidate(
            "session-stale", sid, bucket,
            f"会话 {sid[:18]}… 已停 {hours:g} 小时以上没走到收尾",
            f"最后一条事件是 {row['kind']}（{row['ts']}），没有 turn/end", 0.5, [row["id"]]))
    return out


@rule("clipboard-domain-repeat")
def clipboard_domain_repeat(store, config, now) -> list[dict]:
    min_count = int(_val(config, "clipboard-domain-repeat", "min_count", 3))
    scan = int(_val(config, "clipboard-domain-repeat", "scan_limit", 20))
    rows = store.events(limit=scan, source="clipboard")

    groups: dict[str, list[dict]] = {}
    for row in rows:
        match = DOMAIN_RE.search(str((row.get("payload") or {}).get("head") or ""))
        if match:
            groups.setdefault(match.group(1).lower(), []).append(row)

    out = []
    for domain, items in groups.items():
        if len(items) < min_count:
            continue
        newest = max(items, key=lambda r: r["ts"])
        bucket = _parse(newest["ts"]).date().isoformat()
        out.append(_candidate(
            "clipboard-domain-repeat", domain, bucket,
            f"同一个域名（{domain}）连续出现 {len(items)} 条剪贴板链接",
            "反复复制同一来源的链接，可能是在追同一件事", 0.55, [r["id"] for r in items]))
    return out


@rule("bili-unintegrated")
def bili_unintegrated(store, config, now) -> list[dict]:
    """M5/M5b：bilibili 条目与浏览器历史里看到的工具，KB 里搜不到 ⇒ 提一条待办（「这个工具忘记整合了」）。

    两个来源共用一个 KB 判定与一份缓存（`kb:<owner/repo>`）：`bilibili` 的条目靠
    `bili.extract_tools` 提工具，`browser-history` 的仓库页 URL 里本身就带 `owner/repo`。

    fail-closed：KB 读不了 ⇒ 抛错（`analyze` 记进 errors），**绝不**产出「没整合」的待办——
    否则 KB 一挂就会刷一堆假待办。
    """
    days = float(_val(config, "bili-unintegrated", "window_days", 14))
    scan = int(_val(config, "bili-unintegrated", "scan_limit", 2000))
    cache_days = float(_val(config, "bili-unintegrated", "cache_days", 30))
    name_min = int(_val(config, "bili-unintegrated", "name_min_len", 4))
    kb_root = Path(str(_val(config, "bili-unintegrated", "kb_root", "~/knowledge_base"))).expanduser()

    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    # `store.events` 一次只认一个 source ⇒ 两个来源各扫一遍再按 id 倒序并回来（scan 是每源上限）
    rows = store.events(limit=scan, source="bilibili", since=since)
    rows += store.events(limit=scan, source="browser-history", since=since)
    if not rows:
        return []
    rows.sort(key=lambda r: r["id"], reverse=True)

    tools: dict[str, dict] = {}
    for row in rows:                        # 已按 id 倒序
        payload = row.get("payload") or {}
        # M8：全量历史里绝大多数是视频/小说/搜索页，只有**仓库页 URL** 才可能带工具线索。
        # 非仓库页标题里的 `a/b` 是假货（教训见 PROJECT.md §33）⇒ 历史来源先按 URL 判门。
        if row.get("source") == "browser-history" and not bili.GITHUB_RE.search(
                str(payload.get("url") or "")):
            continue
        for tool in bili.extract_tools([payload]):
            entry = tools.setdefault(tool["key"],
                                     dict(tool, ids=[], newest=row["ts"], source=row["source"]))
            entry["ids"].append(row["id"])
            if row["ts"] > entry["newest"]:
                entry["newest"] = row["ts"]

    if not Path(kb_root).is_dir():
        raise RuleError(f"KB 根不可读（fail-closed，不产出「没整合」待办）: {kb_root}")

    out = []
    for key, info in tools.items():
        cache_key = f"kb:{key}"
        state = store.state_get(cache_key) or {}
        cursor = state.get("cursor") or {}
        cached_at = _parse(cursor["at"]) if cursor.get("at") else None
        fresh = cached_at is not None and (now - cached_at) < timedelta(days=cache_days)
        if fresh and "found" in cursor:
            found = bool(cursor["found"])
        else:
            try:
                found, hit_path = kb.contains(kb_root, key)
                if not found and len(info["repo"]) >= name_min:
                    found, hit_path = kb.contains(kb_root, info["repo"], word=True)
            except kb.KbError as exc:
                raise RuleError(str(exc)) from exc
            store.state_put(cache_key, cursor={"found": found, "at": now.isoformat(timespec="seconds"),
                                               "path": hit_path}, ok=True)
        if found:
            continue
        bucket = _parse(info["newest"]).date().isoformat()
        origin = ("来自浏览器历史" if info.get("source") == "browser-history"
                  else "来自 bilibili 条目")
        out.append(_candidate(
            "bili-unintegrated", key, bucket,
            f"工具「{info['name']}」在知识库里查不到",
            f"{origin}「{info['title'] or info['url']}」（{info['newest']}）",
            0.5, info["ids"]))
    return out


@rule("study-stale")
def study_stale(store, config, now) -> list[dict]:
    """学习记录长期没更新 ⇒ 一条待办候选（常态状态，不发任何消息）。

    证据只来自 `study` 采集器投的 `study.pulse`；阈值 `rules.study-stale.stale_days` 必填
    （代码不写死，缺键 = 配置错误，fail-closed 进 `errors`）。幂等键的 bucket 取**最近一条
    学习记录那天**，所以同一个停滞状态跨天只对应一张待办，不会每天刷一张。
    """
    stale_days = _val(config, "study-stale", "stale_days", None)
    if stale_days is None:
        raise RuleError("缺配置 rules.study-stale.stale_days（阈值只放 hub.config.yaml）")
    try:
        stale_days = int(stale_days)
    except (TypeError, ValueError) as exc:
        raise RuleError(f"rules.study-stale.stale_days 不是整数: {stale_days!r}") from exc
    if stale_days <= 0:
        raise RuleError(f"rules.study-stale.stale_days 必须是正整数: {stale_days}")
    scan = int(_val(config, "study-stale", "scan_limit", 200))

    rows = [r for r in store.events(limit=scan, source="study") if r["kind"] == "study.pulse"]
    if not rows:
        return []
    payload = rows[0].get("payload") or {}
    kp = payload.get("latest_kp")
    latest_ts = payload.get("latest_ts")
    if not kp or not latest_ts:          # 日志一条记录都没有 ⇒ 没有可停滞的知识点
        return []
    try:
        latest_dt = _parse(latest_ts)
    except ValueError as exc:
        raise RuleError(f"study.pulse 的 latest_ts 读不出: {latest_ts!r}") from exc

    days = (now.date() - latest_dt.date()).days
    if days < stale_days:
        return []
    bucket = latest_dt.date().isoformat()
    return [_candidate(
        "study-stale", kp, bucket,
        f"「{kp}」已经 {days} 天没有新的学习记录",
        f"最近一条学习记录是 {latest_ts}（知识点 {kp}），阈值 {stale_days} 天",
        0.5, [rows[0]["id"]])]


# ---------------------------------------------------------------- 入口
def analyze(store, config, now: datetime | None = None) -> dict:
    """跑全部启用规则。单条规则抛错不影响其它（与采集器同口径）。"""
    now = now or datetime.now().astimezone()
    candidates: list[dict] = []
    errors: list[dict] = []
    for name in sorted(RULES):
        if not _enabled(config, name):
            continue
        try:
            candidates.extend(RULES[name](store, config, now))
        except Exception as exc:
            errors.append({"rule": name, "error": f"{type(exc).__name__}: {exc}"})
    return {"candidates": candidates, "errors": errors}


def derive(store, config, now: datetime | None = None, dry_run: bool = False) -> dict:
    """规则 → 待办（按 dedupe_key 幂等）。返回本次新建/命中的明细。"""
    result = analyze(store, config, now)
    created, refreshed = [], []
    for cand in result["candidates"]:
        if dry_run:
            exists = store.todo_exists(cand["dedupe_key"])
            (refreshed if exists else created).append(cand["dedupe_key"])
            continue
        todo_id, is_new = store.upsert_todo(cand)
        (created if is_new else refreshed).append(todo_id)
    return {"candidates": result["candidates"], "errors": result["errors"],
            "created": created, "refreshed": refreshed, "dry_run": dry_run}
