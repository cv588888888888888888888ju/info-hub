"""M4d：**本地确定性**归纳后端（不联网、零 token、不调任何外部加工层）。

本文件是归纳层（`digest.py`）的一个现成后端命令，读库直接产出，也能被 `digest.command` 指过来。
语义见 INTERFACE.md §4.9；措辞纪律见 PROJECT.md §6.1；数值只在 hub.config.yaml 一处。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta

from .config import ConfigError, load
from .store import Store, StoreError

# 兜底上限（条目预算不在这里，从配置取）：见 INTERFACE.md §4.9。
MAX_TEXT_CHARS = 60
MAX_TITLE_CHARS = 40


PROMPT_LINE_RE = re.compile(r"^- (\d+) \| (\S+) \| (\S+)/(\S+) \| ", re.M)


def _clip(text: str) -> str:
    text = str(text)
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS]


def _since(window_days: int) -> str:
    return (datetime.now().astimezone()
            - timedelta(days=int(window_days))).isoformat(timespec="seconds")


def parse_prompt_ids(prompt: str) -> dict[int, dict]:
    """从上游提示词的事件流水里读出**允许引用的事件 id**（契约见 INTERFACE.md §4.9）。

    提示词每行形如 `- 12 | 2026-10-16T09:00:00+08:00 | fs/fs.change | {...}`。机械保证：
    只有真在提示词里出现过的 id 才允许进 `evidence` —— 与归纳层 `parse_output` 的审计同一把
    尺子，免得本地后端自己另取一份数据、产出"审计不认"的假依据。
    """
    out: dict[int, dict] = {}
    for m in PROMPT_LINE_RE.finditer(str(prompt or "")):
        out[int(m.group(1))] = {"ts": m.group(2), "source": m.group(3), "kind": m.group(4)}
    return out


def build(config, store, window_days: int, prompt: str = "") -> str:
    """产出契约要求的 JSON 文本（确定性、只依据提示词里出现过的事件）。语义见 INTERFACE.md §4.9。"""
    days = int(window_days)
    since = _since(days)
    max_items = int(config.get("digest.max_items") or 6)
    per_item = int(config.get("digest.local.evidence_per_item") or 3)
    allowed = parse_prompt_ids(prompt)          # 允许引用的 id 集合 = 提示词里出现过的
    items: list[dict] = []

    for todo in store.todos(status="open", limit=int(config.get("digest.max_todos") or 30)):
        if len(items) >= max_items:
            break
        created = todo.get("created_at")
        if isinstance(created, str) and created and created < since:
            continue                      # 窗口外（§4.9）
        evidence: list[int] = []
        for row in store.todo_evidence(todo["id"]):
            if row.get("missing"):
                continue                  # 证据链空洞（§5.8）
            eid = row.get("id")
            if not isinstance(eid, int) or isinstance(eid, bool) or eid not in allowed:
                continue                  # 不在提示词里 ⇒ 不许引用（§4.9）
            evidence.append(eid)
            if len(evidence) >= per_item:
                break
        if not evidence:
            continue                      # 无依据不发（§4.9）
        text = f"待办：{str(todo.get('title') or '').strip()[:MAX_TITLE_CHARS]}"
        items.append({"text": _clip(text), "evidence": evidence})

    groups: dict[str, list[int]] = {}
    for eid, info in allowed.items():
        groups.setdefault(str(info.get("source") or ""), []).append(eid)
    for source, ids in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(items) >= max_items:
            break
        ids.sort(reverse=True)
        latest = str(allowed[ids[0]].get("ts") or "")
        items.append({"text": _clip(f"最近 {len(allowed)} 条事件里 {source} 占 {len(ids)} 条，最新 {latest}"),
                      "evidence": ids[:per_item]})

    return json.dumps({"items": items[:max_items]}, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    """命令入口。语义见 INTERFACE.md §4.9。"""
    parser = argparse.ArgumentParser(prog="hub-digest-local",
                                     description="info-hub 本地确定性归纳后端（M4d）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--window-days", type=int, default=7)
    args = parser.parse_args(argv)

    try:
        config = load(args.config)
        config.ensure_dirs()
        store = Store(config.db_path)
        store.migrate()
    except (ConfigError, StoreError, OSError, sqlite3.Error) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 1

    try:
        prompt = "" if sys.stdin.isatty() else sys.stdin.read()   # 上游提示词：读进来只为取"允许引用的 id"
        print(build(config, store, args.window_days, prompt=prompt), flush=True)
        return 0
    except (StoreError, OSError, sqlite3.Error) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
