"""M4b：把一段窗口内的系统数据交给**可替换的**加工层（LLM），拿回带证据指针的归纳。

设计要点（契约见 INTERFACE.md §4.9）：
- 加工层只是**一个命令**（`digest.command`，argv 模板，提示词走 stdin）⇒ 换模型 = 换命令，
  不把任何厂商 API 写死进代码；测试用 `tests/digest_stub.py` 当假模型，全程离线。
- 输出必须能被机械审计：JSON `{"items":[{"text":…,"evidence":[event_id,…]}]}`；
  **证据指向不存在的事件 ⇒ 该条丢弃**（`dropped` 计数），不许"看起来像有依据"。
- 提示词里的事件 payload 先过 `privacy.redact`（M4 的脱敏，见 PROJECT.md §11）。
- 同一份输入（`input_hash`）只花一次 token：已有 digest 直接复用（`reused`）。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta

from infohub.events import canonical, iso
from infohub.privacy import redact_obj

MAX_PAYLOAD_CHARS = 400


class DigestError(Exception):
    """加工层不可用 / 输出不可解析。调用方折成退出码 1，不写半成品。"""


def _view(row: dict) -> dict:
    """事件行 → 提示词里的一行（payload 先脱敏再截断）。`events()` 已解析 payload，`todo_evidence()` 是原文。"""
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    payload, n = redact_obj(payload)
    return {"id": row["id"], "ts": row["ts"], "source": row["source"], "kind": row["kind"],
            "payload": json.dumps(payload, ensure_ascii=False)[:MAX_PAYLOAD_CHARS]}, n


def build_input(config, store, window_days: int | None = None) -> dict:
    """组装交给加工层的输入（确定性；同一份数据 ⇒ 同一份输入）。"""
    days = int(window_days or config.get("digest.window_days") or 1)
    since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    max_events = int(config.get("digest.max_events") or 200)
    events, redactions = [], 0
    for row in reversed(store.events(limit=max_events, since=since)):   # 时间正序，读起来像流水
        view, n = _view(row)
        redactions += n
        events.append(view)
    todos = [{"id": t["id"], "title": t["title"], "status": t["status"],
              "why": t.get("why") or "", "dedupe_key": t.get("dedupe_key") or ""}
             for t in store.todos(status="open", limit=int(config.get("digest.max_todos") or 30))]
    # 待办的证据事件必须一起进提示词（M4d）：事件流水只取最新 max_events 条，而待办的依据
    # 可能更老；若它不在提示词里，加工层照实引用也会被 `parse_output` 的审计丢掉。
    cap = int(config.get("digest.todo_evidence_events") or 40)
    known = {e["id"] for e in events}
    extra: list[dict] = []
    for t in todos:
        if len(extra) >= cap:
            break
        for ev in store.todo_evidence(t["id"], limit=5):
            if len(extra) >= cap:
                break
            if ev.get("missing") or not isinstance(ev.get("id"), int) or ev["id"] in known:
                continue
            view, n = _view(ev)
            redactions += n
            known.add(ev["id"])
            extra.append(view)
    events = sorted(events + extra, key=lambda e: e["id"])
    stats = store.stats()
    return {"window_days": days, "since": since,
            "events": events, "todos": todos,
            "event_total": stats["events_total"],
            "per_source": stats["per_source"],
            "redactions": redactions,
            "event_ids": [e["id"] for e in events]}


def input_hash(payload: dict) -> str:
    """「同一份输入」的指纹 = **看内容，不看时钟**（窗口天数 + 事件 + 待办）。

    `since` 每秒都在变、`redactions` 只是计数 ⇒ 都不进指纹；否则同一份数据永远算新输入、
    每次都白花一次 token。
    """
    material = {"window_days": payload.get("window_days"),
                "events": payload.get("events"),
                "todos": payload.get("todos")}
    return hashlib.sha256(canonical(material).encode("utf-8")).hexdigest()[:16]


def render_prompt(config, payload: dict) -> str:
    """提示词模板。**输出契约写在提示词里**，加工层换谁都得守。"""
    lines = [
        "你是本机「信息中枢」的归纳器。依据下面这段真实系统事件流水，给出最多 "
        f"{int(config.get('digest.max_items') or 6)} 条值得人看一眼的条目。",
        "",
        "硬要求：",
        "1. 只输出一个 JSON 对象，不要解释、不要 Markdown 代码块："
        ' {"items":[{"text":"…","evidence":[事件id,…]}]}',
        "2. 每条必须挂至少 1 个**上面流水里出现过**的事件 id；找不到依据就不许写这条。",
        "3. 不要编造文件、时间、人名；不要催促、不要建议日程（措辞纪律见 PROJECT.md §6.1）。",
        "4. text 一句中文，≤60 字。",
        "",
        f"窗口：{payload['since']} 起 {payload['window_days']} 天；事件 {len(payload['events'])} 条"
        f"（库内共 {payload['event_total']} 条）；未完成待办 {len(payload['todos'])} 条。",
        "",
        "## 待办（id | status | title）",
    ]
    lines += [f"- {t['id']} | {t['status']} | {t['title']}" for t in payload["todos"]] or ["-（无）"]
    lines += ["", "## 事件流水（id | ts | source/kind | payload）"]
    lines += [f"- {e['id']} | {e['ts']} | {e['source']}/{e['kind']} | {e['payload']}"
              for e in payload["events"]] or ["-（无）"]
    return "\n".join(lines) + "\n"


def _extract_json(text: str) -> dict:
    raw = str(text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise DigestError("加工层输出里没有 JSON 对象")
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError as exc:
        raise DigestError(f"加工层输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise DigestError("加工层输出缺少 items 数组")
    return data


def parse_output(text: str, valid_ids: set[int], max_items: int) -> dict:
    """解析 + **机械审计**：evidence 必须指向真实事件 id，否则丢条。"""
    data = _extract_json(text)
    items, dropped = [], 0
    for raw in data["items"]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        body = str(raw.get("text") or "").strip()
        evidence = [e for e in (raw.get("evidence") or []) if isinstance(e, int) and e in valid_ids]
        if not body or not evidence:
            dropped += 1
            continue
        items.append({"text": body, "evidence": evidence})
        if len(items) >= max(1, int(max_items)):
            break
    return {"items": items, "dropped": dropped}


def _invoke(config, prompt: str) -> tuple[str, str, int]:
    """跑 `digest.command`（argv 模板，提示词走 stdin）。返回 `(stdout, backend, elapsed_ms)`。"""
    argv = [str(x) for x in (config.get("digest.command") or [])]
    if not argv:
        raise DigestError("digest.command 没配置（换模型 = 换这条命令）")
    timeout = float(config.get("digest.timeout_s") or 120)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise DigestError(f"加工层命令不存在：{argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DigestError(f"加工层超时（{timeout:.0f}s）") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        raise DigestError(f"加工层退出码 {proc.returncode}：{(proc.stderr or '').strip()[:200]}")
    return proc.stdout, " ".join(argv[:2]), elapsed_ms


def run(config, store, window_days: int | None = None, force: bool = False,
        dry_run: bool = False) -> dict:
    """一次归纳。`dry_run` 只组装提示词（不调加工层、不写库）。"""
    payload = build_input(config, store, window_days=window_days)
    prompt = render_prompt(config, payload)
    ihash = input_hash(payload)
    phash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    out: dict = {"at": iso(), "window_days": payload["window_days"], "since": payload["since"],
                 "events": len(payload["events"]), "todos": len(payload["todos"]),
                 "redactions": payload["redactions"], "input_hash": ihash, "prompt_hash": phash,
                 "prompt_chars": len(prompt)}
    if not bool(config.get("digest.enabled", False)):
        return {**out, "reason": "disabled", "detail": "digest.enabled=false（唯一数值源在配置文件）"}
    if dry_run:
        return {**out, "reason": "dry-run", "prompt": prompt}
    existing = store.find_digest(ihash)
    if existing and not force:
        return {**out, "reason": "reused", "digest_id": existing["id"],
                "items": existing["items"], "dropped": existing["dropped"],
                "detail": "同一份输入已经归纳过，不重复花 token"}
    text, backend, elapsed_ms = _invoke(config, prompt)
    parsed = parse_output(text, set(payload["event_ids"]), int(config.get("digest.max_items") or 6))
    digest_id = store.add_digest(payload["window_days"], backend, phash, ihash, text,
                                 parsed["items"], parsed["dropped"], elapsed_ms)
    return {**out, "reason": "ok", "digest_id": digest_id, "backend": backend,
            "elapsed_ms": elapsed_ms, "items": parsed["items"], "dropped": parsed["dropped"]}


def render_block(store, config) -> str:
    """报表区块 `digest`：最近一次归纳（每条带 evidence 指针）。"""
    rows = store.digests(limit=1)
    if not rows:
        return "_（还没有归纳记录：`bin/hub-digest run` 一次）_\n"
    latest = rows[0]
    head = (f"_最近一次：{latest['ts']} · 窗口 {latest['window_days']} 天 · "
            f"丢弃无依据 {latest['dropped']} 条 · 输入 `{latest['input_hash']}`_")
    lines = [head, ""]
    if latest["error"]:
        lines.append(f"**上次失败**：{latest['error']}")
    if not latest["items"]:
        lines.append("_（本次没有可挂依据的条目）_")
    for item in latest["items"]:
        ev = ",".join(f"#{e}" for e in item["evidence"])
        lines.append(f"- {item['text']}  <small>evidence: {ev}</small>")
    return "\n".join(lines) + "\n"
