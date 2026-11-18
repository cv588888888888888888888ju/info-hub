"""把手机上 helper APK 落盘的回执同步回投递台账（INTERFACE.md §11.7b 的回执通道）。

helper APK（`tools/hubreplier/`）在手机侧把用户的回复/选项追加到
`files/replies/all.jsonl`（schema 见文件头注释与 README）。本模块用 adb 读那条文件，
按**行号游标**只处理新行（all.jsonl 只追加、不重写），把每行折成台账的 ack / reply。

为什么不用 HTTP：真机实测 `adb reverse` 不稳定（同一 reverse 17:01 通、17:16 之后请求消失），
所以回执以「落盘 + adb 拉取」为主通道；helper 里的 POST 只当兜底。

一行形如（键固定，多一个键忽略）：
  {"id": "d-…", "kind": "reply"|"action", "text": "..."|null, "action": "ack"|null,
   "src": "remoteinput"|"button"|"test", "at": "2026-11-14T17:30:00+08:00"}
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import deliver as D

#: 手机侧文件（相对 App 私有目录；run-as 读）
REMOTE_FILE = "files/replies/all.jsonl"
#: 游标文件（相对数据根）
CURSOR_REL = "state/phone-replies.cursor"


class SyncError(RuntimeError):
    """adb / 设备 / 包不可用（调用方退出码 1）。"""


def _adb(config, args: list[str]) -> str:
    exe = str(config.get("phone_remind.adb") or "adb")
    device = str(config.get("phone_remind.device") or "").strip()
    if not device:
        raise SyncError("需要 phone_remind.device（adb devices 的序列号）")
    timeout = float(config.get("phone_remind.adb_timeout_s") or 15)
    cmd = [exe, "-s", device, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"adb 超时（{timeout:g}s）: {' '.join(cmd)}") from exc
    except OSError as exc:
        raise SyncError(f"adb 起不来: {exc}") from exc
    if proc.returncode != 0:
        raise SyncError((proc.stderr or proc.stdout or "").strip()[:300] or f"adb rc={proc.returncode}")
    return proc.stdout or ""


def fetch_lines(config) -> list[str]:
    """读手机侧 all.jsonl（整份）。文件不存在 ⇒ 空列表（不是错误：还没人回过）。"""
    pkg = str(config.get("phone_remind.helper_pkg") or "com.chenty.hubreplier")
    try:
        out = _adb(config, ["exec-out", "run-as", pkg, "cat", REMOTE_FILE])
    except SyncError as exc:
        msg = str(exc)
        if "No such file" in msg or "No such file or directory" in msg:
            return []
        raise
    return [line for line in out.splitlines() if line.strip()]


def _cursor_path(config) -> Path:
    return Path(config.get("root")) / CURSOR_REL


def read_cursor(config) -> int:
    path = _cursor_path(config)
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip() or 0))
    except (OSError, ValueError):
        return 0


def write_cursor(config, n: int) -> None:
    path = _cursor_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(int(n)), encoding="utf-8")
    os.replace(tmp, path)


def parse_line(line: str) -> dict | None:
    """一行 → 规整后的回执；坏行返回 None（跳过并计数，不让一行脏数据挡住整批）。"""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    ident = str(raw.get("id") or "").strip()
    if not ident:
        return None
    # 容两种写法：本文件头部的 schema（kind/src/at），以及 helper 0.1.0 起的旧键（mode/source/received_at）。
    kind = str(raw.get("kind") or raw.get("mode") or "").strip() or "reply"
    item = {"id": ident, "kind": kind,
            "at": raw.get("at") or raw.get("received_at"),
            "src": raw.get("src") or raw.get("source")}
    if kind == "action":
        item["action"] = str(raw.get("action") or "").strip() or None
        if not item["action"]:
            return None
    else:
        item["text"] = str(raw.get("text") or "")
    return item


def sync(config, store, apply: bool = False, limit: int | None = None) -> dict:
    """把「游标之后」的新回执同步进台账；apply=False 只报告不改库。返回给 CLI 打 JSON。"""
    lines = fetch_lines(config)
    cursor = read_cursor(config)
    rotated = len(lines) < cursor          # 文件被清过/重写 ⇒ 游标失真，从头读（ack 幂等，reply 同值覆盖）
    start = 0 if rotated else cursor
    todo = lines[start:]
    if limit is not None:
        todo = todo[: max(0, int(limit))]
    applied: list[dict] = []
    skipped: list[dict] = []
    for line in todo:
        item = parse_line(line)
        if item is None:
            skipped.append({"line": line[:200], "why": "bad-line"})
            continue
        if not apply:
            applied.append({**item, "applied": False})
            continue
        try:
            if item["kind"] == "action":
                row = D.ack(store, item["id"], item.get("action"))
            else:
                row = D.reply(store, item["id"], item["text"])
        except D.DeliveryNotFound:
            skipped.append({"id": item["id"], "why": "not-found"})
            continue
        except D.DeliveryError as exc:
            skipped.append({"id": item["id"], "why": str(exc)[:200]})
            continue
        applied.append({**item, "applied": True, "status": row.get("status")})
    if apply:
        write_cursor(config, start + len(todo))
    return {"total": len(lines), "cursor_before": cursor, "rotated": rotated,
            "new": len(todo), "applied": applied, "skipped": skipped}
#: CLI 退出码（INTERFACE.md §11.7b）：0 正常；1 adb/设备/包不可用；2 配置或库错。
def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from .config import load
    from .store import Store

    ap = argparse.ArgumentParser(prog="hub-phone-replies", add_help=True)
    ap.add_argument("--apply", action="store_true", help="真的写台账（缺省只报告）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--limit", type=int, default=None, help="本次最多处理几行")
    args = ap.parse_args(argv)

    try:
        config = load()
        store = Store(config.db_path)
        store.migrate()
    except Exception as exc:                      # 配置/库错 ⇒ 2
        print(f"hub-phone-replies: {exc}", file=sys.stderr)
        return 2

    try:
        result = sync(config, store, apply=args.apply, limit=args.limit)
    except SyncError as exc:                      # adb / 设备 / 包 ⇒ 1
        print(f"hub-phone-replies: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        mode = "apply" if args.apply else "dry-run"
        print(f"[{mode}] total={result['total']} cursor={result['cursor_before']} "
              f"new={result['new']} applied={sum(1 for i in result['applied'] if i.get('applied'))} "
              f"skipped={len(result['skipped'])}")
        for item in result["applied"]:
            print(f"  + {item['id']} {item['kind']} "
                  f"{item.get('text') or item.get('action')} → {item.get('status') or '(dry)'}")
        for item in result["skipped"]:
            print(f"  ! {item}")
    return 0
