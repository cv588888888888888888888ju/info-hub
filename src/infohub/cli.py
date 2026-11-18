"""CLI：`bin/hub-collect` 与 `bin/hub-store` 共用。语义见 INTERFACE.md §2、§6。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime as _dt, timedelta as _timedelta
from pathlib import Path

from . import sources
from . import deliver
from . import plugin as plugins
from . import views
from . import derived
from .derived import DerivedError
from .bus import Bus
from .config import Config, ConfigError, load
from .events import iso, make_event
from .store import Store, StoreError

EXIT_OK, EXIT_FATAL, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    pass


def _emit(text: str, quiet: bool) -> None:
    """人读日志一律走 stderr：stdout 留给 --json 的机器可读汇总（INTERFACE.md §2.1）。"""
    if not quiet:
        print(text, file=sys.stderr, flush=True)


def _json_out(obj, enabled: bool) -> None:
    if enabled:
        print(json.dumps(obj, ensure_ascii=False, sort_keys=True), flush=True)


def _processor_module(config, name: str):
    """按插件清单找已启用的 processor 插件并返回其模块（S14 起规则引擎是插件）。"""
    for row in plugins.list_plugins(config):
        if (row.get("kind") == "processor" and row.get("name") == name
                and row.get("status") == "ok" and not row.get("shadowed")):
            return plugins.load_module(row)
    raise RuntimeError(f"processor 插件未安装/未启用: {name}（查 plugins.{name}.enabled）")


# --------------------------------------------------------------------------- collect
def run_round(config: Config, store: Store, bus: Bus, names: list[str] | None = None,
              quiet: bool = False) -> dict:
    """跑一轮采集。失败语义见 INTERFACE.md §6：单采集器失败不影响其它，退出码仍 0。"""
    registry = sources.registry(config, only_enabled=(names is None))
    wanted = config.enabled_collectors() if names is None else names
    chosen = [n for n in wanted if n in registry]
    summary: dict = {"run_at": iso(), "sources": {}, "ingested": 0, "skipped_duplicate": 0,
                     "window_new": 0}

    pending: list[dict] = []
    for name in chosen:
        try:
            events = registry[name](config, store)
            summary["sources"][name] = {"ok": True, "emitted": len(events), "skipped": 0, "error": None}
            pending.extend(events)
            _emit(f"[{name}] emitted={len(events)}", quiet)
        except Exception as exc:  # 单个采集器失败不致命
            detail = f"{type(exc).__name__}: {exc}"
            summary["sources"][name] = {"ok": False, "emitted": 0, "skipped": 0, "error": detail}
            store.state_put(f"run:{name}", ok=False, error=detail)
            _emit(f"[{name}] FAILED {detail}", quiet)

    if pending:
        bus.emit(pending)                     # 先进 spool
    backlog = bus.read_all()                  # 再统一入库
    # 触发面（M2f）：这一轮是否真的收进了新的界面事件。通知层只在它为真时才被评估。
    summary["window_new"] = sum(1 for e in backlog if e.get("source") == "window")
    if not backlog:
        if not pending:
            _emit("nothing new", quiet)
        return summary

    try:
        inserted, duplicates = store.insert_events(backlog)
    except StoreError as exc:
        # 入库失败：spool 保留，下一轮重放（去重键保证幂等）
        summary["error"] = str(exc)
        _emit(f"ingest failed, spool kept ({len(backlog)} events): {exc}", quiet)
        return summary

    bus.truncate()
    summary["ingested"] = inserted
    summary["skipped_duplicate"] = duplicates
    summary["backlog"] = len(backlog)
    _emit(f"ingested={inserted} duplicate={duplicates} backlog={len(backlog)}", quiet)
    return summary


def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _notify_after_ui(config: Config, store: Store, summary: dict, quiet: bool = False) -> dict:
    """触发面（M2f）：只有**这一轮真的收进了新的 window 事件**才评估一次通知。

    为什么不是定时器：例外条文①要求「只在检测到界面活动时评估」；若用 timer 触发，
    「评估」就变成按时间表发生，条文①②就只剩门在挡、触发点本身失守。
    这里把触发点钉死在「采集到新界面事件」这件事上 —— 定时的只是**采集**，不是**推送**。
    门 1（`notify.enabled`）仍由 `dispatch` 自己判；本函数只管"要不要去评估"。
    """
    if not summary.get("window_new"):
        return {"skipped": True, "reason": "no-new-ui-event", "sent": 0}
    from .notify import dispatch
    result = dispatch(config, store)
    _emit(f"[notify] sent={result['sent']} reason={result['reason']}", quiet)
    return result


def main_collect(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-collect", description="info-hub 采集一轮（M0）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 $HUB_CONFIG 或 hub.config.yaml）")
    parser.add_argument("--once", action="store_true", help="跑一轮后退出（M0 唯一模式）")
    parser.add_argument("--source", default=None, help="只跑指定采集器，逗号分隔")
    parser.add_argument("--notify", action="store_true",
                        help="采完后若这一轮真收进了新的 window 事件，就评估一次通知（M2f；开关仍看 notify.enabled）")
    parser.add_argument("--selftest", action="store_true", help="临时目录里跑合成事件自检")
    parser.add_argument("--json", action="store_true", help="输出机器可读汇总")
    parser.add_argument("--quiet", action="store_true", help="不打日志")
    args = parser.parse_args(argv)

    if args.selftest:
        from .selftest import run as selftest_run
        return selftest_run(json_out=args.json, quiet=args.quiet)

    names = [s.strip() for s in args.source.split(",") if s.strip()] if args.source else None
    try:
        config = load(args.config)
        config.ensure_dirs()
        store = Store(config.db_path)
        store.migrate()
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    lock = _lock(config.state_dir / "collect.lock")
    if lock is None:
        _emit("another collect is running, skipped", args.quiet)
        _json_out({"run_at": iso(), "skipped": "locked"}, args.json)
        store.close()
        return EXIT_OK
    try:
        summary = run_round(config, store, Bus(config.bus_path), names, args.quiet)
        if args.notify:
            summary["notify"] = _notify_after_ui(config, store, summary, args.quiet)
    finally:
        store.close()
        lock.close()

    _json_out(summary, args.json)
    return EXIT_OK


# --------------------------------------------------------------------------- rules（M1）
def main_rules(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-rules", description="info-hub 加工层（M1）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只算不写")
    parser.add_argument("--limit", type=int, default=50, help="board-sync 单次最多推几张")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("derive")       # 规则 → 待办
    sub.add_parser("board-sync")   # 待办 → 看板卡片
    sub.add_parser("rules")        # 列出规则与开关

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return EXIT_USAGE
    try:
        config = load(args.config)
        config.ensure_dirs()
    except ConfigError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    if args.cmd == "rules":
        try:
            RULES = _processor_module(config, "rules").RULES
        except RuntimeError as exc:
            print(f"fatal: {exc}", file=sys.stderr)
            return EXIT_FATAL
        items = [{"rule": name, "enabled": bool(config.get(f"rules.{name}.enabled", True))}
                 for name in sorted(RULES)]
        if args.json:
            print(json.dumps({"rules": items}, ensure_ascii=False, indent=2))
        else:
            for item in items:
                print(f"{'on ' if item['enabled'] else 'off'} {item['rule']}")
        return EXIT_OK

    try:
        store = Store(config.db_path)
        store.migrate()
    except StoreError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        if args.cmd == "derive":
            try:
                derive = _processor_module(config, "rules").derive
            except RuntimeError as exc:
                print(f"fatal: {exc}", file=sys.stderr)
                return EXIT_FATAL
            result = derive(store, config, dry_run=args.dry_run)
            summary = {"candidates": len(result["candidates"]), "created": result["created"],
                       "refreshed": result["refreshed"], "errors": result["errors"],
                       "dry_run": args.dry_run, "items": []}
            for cand in result["candidates"]:
                _emit(f"[{cand['rule']}] {cand['title']}  (证据 {len(cand['source_event_ids'])} 条)",
                      args.quiet)
                summary["items"].append({"rule": cand["rule"], "key": cand["dedupe_key"],
                                         "title": cand["title"]})
            _emit(f"created={len(result['created'])} refreshed={len(result['refreshed'])}"
                  + (" (dry-run)" if args.dry_run else ""), args.quiet)
            if bool(config.get("vision.enabled", False)):
                V = None
                try:
                    V = derived.module(config, "vision")
                    vres = V.run(config, store, dry_run=args.dry_run)
                except Exception as exc:        # 画面语义/插件缺位都永远不拖垮 derive
                    vres = {"error": f"{type(exc).__name__}: {exc}"}
                summary["vision"] = vres
                if V is not None:
                    _emit(V.summary_line(vres), args.quiet)
            if bool(config.get("vision.vlm.enabled", False)):     # M12 粗判，同样不拖垮 derive
                from . import vlm as M
                try:
                    mres = M.run(config, store, dry_run=args.dry_run)
                except Exception as exc:
                    mres = {"error": f"{type(exc).__name__}: {exc}"}
                summary["vlm"] = mres
                _emit(M.summary_line(mres), args.quiet)
            for err in result["errors"]:
                _emit(f"[rule-error] {err['rule']}: {err['error']}", args.quiet)
            # M16/M10：顺带把当天与前一天的成品各写一遍（幂等；前一天补 23 点后的尾巴）。
            # 语义见 INTERFACE.md §2.14 / §4.15；单路失败只记一行，不影响 derive 退出码。
            _now = _dt.now().astimezone()
            for _when in (_now, _now - _timedelta(days=1)):
                _day = _when.strftime("%Y-%m-%d")
                try:
                    F = derived.module(config, "features")
                    _ver = str(config.get("behavior.feat_version", F.VERSION) or F.VERSION)
                    _rows = F.build(store, _day, _ver)
                    store.features_replace(_day, _ver, _rows)
                    _emit(f"行为特征：{_day} 写 {len(_rows)} 行", args.quiet)
                except Exception as exc:
                    _emit(f"行为特征：跳过（{_day} {type(exc).__name__}）", args.quiet)
                try:
                    _eps = _activity_derive(config, store, _day)
                    _emit(f"行为片段：{_day} {len(_eps)} 段", args.quiet)
                except Exception as exc:
                    _emit(f"行为片段：跳过（{_day} {type(exc).__name__}）", args.quiet)
            _json_out(summary, args.json)
            return EXIT_OK

        if args.cmd == "board-sync":
            try:
                sync = views.module(config, "board").sync
            except views.ViewError as exc:
                print(f"fatal: {exc}", file=sys.stderr)
                return EXIT_FATAL
            result = sync(store, config, dry_run=args.dry_run, limit=args.limit)
            for item in result["pushed"]:
                _emit(f"card {item.get('card', '-')} ← {item['id']}", args.quiet)
            for err in result["errors"]:
                _emit(f"[board-error] {err['todo']}: {err['error']}", args.quiet)
            _json_out(result, args.json)
            return EXIT_OK

        raise UsageError(f"unknown subcommand {args.cmd}")
    finally:
        store.close()


# --------------------------------------------------------------------------- store
def _store_open(args) -> tuple[Config, Store]:
    config = load(args.config)
    config.ensure_dirs()
    store = Store(config.db_path)
    store.migrate()
    return config, store


def _print_events(rows: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for row in rows:
        payload = row.get("payload")
        brief = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        print(f"{row['id']:>6}  {row['ts']}  {row['source']:<12} {row['kind']:<28} {brief[:120]}")


def _store_redact(config: Config, store: Store, args) -> int:
    """历史数据验证码脱敏：只 UPDATE `payload`，绝不 DELETE 行（INTERFACE.md §2.2 / §5.14）。"""
    from . import redact as R

    source = args.source
    if not R.enabled(config):
        if args.json:
            print(json.dumps({"source": source, "enabled": False, "scanned": 0, "hit": 0,
                              "updated": 0, "dry_run": bool(args.dry_run),
                              "version": R.version(config)}, ensure_ascii=False))
        else:
            print("redact: disabled (redact.enabled=false) —— 不动库")
        return EXIT_OK

    scanned = hit = updated = 0
    try:
        for row in store.iter_events(source=source, limit=args.limit):
            scanned += 1
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            new_payload, summary = R.apply(payload, config)
            if not summary["hit"]:
                continue
            hit += 1
            if args.dry_run:
                continue
            if store.update_event_payload(row["id"], new_payload):
                updated += 1
    except ValueError as exc:            # redact.mode 非法 ⇒ 配置非法（§6）
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    result = {"source": source, "enabled": True, "scanned": scanned, "hit": hit,
              "updated": updated, "dry_run": bool(args.dry_run), "version": R.version(config)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif args.dry_run:
        print(f"redact: source={source} scanned={scanned} hit={hit}"
              f"（dry-run，未写库） version={result['version']}")
    else:
        print(f"redact: source={source} scanned={scanned} hit={hit} updated={updated}"
              f" version={result['version']}")
    return EXIT_OK


def _store_todo_add(store: Store, args) -> int:
    """建一条人工来源的待办（INTERFACE.md §2.2）。

    幂等口径与规则引擎一致：同 `dedupe_key` 已存在 ⇒ 只刷新证据集合，
    返回既有 id 且 `new=false`。author 记 `manual`，规则引擎不得覆盖。
    """
    title = (args.title or "").strip()
    if not title:
        print("fatal: --title 不能为空", file=sys.stderr)
        return EXIT_USAGE
    key = (args.dedupe_key or "").strip()
    if not key:
        key = "manual:" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    todo_id, created = store.upsert_todo({
        "dedupe_key": key,
        "title": title,
        "why": args.why or "",
        "confidence": args.confidence or 0.0,
        "source_event_ids": list(dict.fromkeys(args.source_event_id or [])),
    })
    if created:
        store.write_todo_revision(todo_id, "status", None, "open",
                                  author="manual", reason="todo-add")
    out = {"id": todo_id, "new": created, "dedupe_key": key, "title": title}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{'新建' if created else '已存在'} {todo_id}  {title}")
    return EXIT_OK


def main_store(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-store", description="info-hub 存储查询/维护（M0）")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd")

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--json", action="store_true")

    p_events = sub.add_parser("events")
    p_events.add_argument("--limit", type=int, default=20)
    p_events.add_argument("--source", default=None)
    p_events.add_argument("--since", default=None)
    p_events.add_argument("--json", action="store_true")

    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--json", action="store_true")

    # M15：历史数据验证码脱敏（只 UPDATE payload，绝不 DELETE 行；INTERFACE.md §2.2 / §5.14）
    p_redact = sub.add_parser("redact")
    p_redact.add_argument("--source", default="phone-hub")
    p_redact.add_argument("--dry-run", action="store_true")
    p_redact.add_argument("--limit", type=int, default=0, help="最多扫几条（0 = 全部）")
    p_redact.add_argument("--json", action="store_true")

    p_todos = sub.add_parser("todos")
    p_todos.add_argument("--status", default=None)
    p_todos.add_argument("--limit", type=int, default=50)
    p_todos.add_argument("--json", action="store_true")

    p_todo_set = sub.add_parser("todo-set")
    p_todo_set.add_argument("id")
    p_todo_set.add_argument("status", choices=["open", "doing", "done", "dropped"])
    p_todo_set.add_argument("--json", action="store_true")

    # M3a：外部（agent / 脚本）直接建一条待办——不是规则引擎产的，author 记 manual
    p_todo_add = sub.add_parser("todo-add")
    p_todo_add.add_argument("--title", required=True, help="一句话标题（进清单与报表）")
    p_todo_add.add_argument("--why", default="", help="为什么记它（可选）")
    p_todo_add.add_argument("--dedupe-key", default=None,
                            help="幂等键；缺省 = manual:<sha1(title)[:12]>")
    p_todo_add.add_argument("--source-event-id", action="append", default=None,
                            help="证据事件 id，可重复")
    p_todo_add.add_argument("--confidence", type=float, default=0.0)
    p_todo_add.add_argument("--json", action="store_true")

    # M3c：单条待办的完整视图（元信息 + 证据链 + 改动记录）
    p_todo_one = sub.add_parser("todo")
    p_todo_one.add_argument("id")
    p_todo_one.add_argument("--json", action="store_true")

    # M3b：清单的改动记录（与报表的 report-revisions 对称）
    p_todo_rev = sub.add_parser("todo-revisions")
    p_todo_rev.add_argument("id", nargs="?", default=None, help="留空 = 最近所有待办的改动")
    p_todo_rev.add_argument("--limit", type=int, default=20)
    p_todo_rev.add_argument("--json", action="store_true")

    p_doc = sub.add_parser("doctor")
    p_doc.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return EXIT_USAGE

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        if args.cmd == "stats":
            data = store.stats()
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(f"库: {data['db']}  ({data['db_bytes']} 字节)")
                print(f"事件: {data['events_total']}  首={data['events_first']}  末={data['events_last']}")
                for src, n in sorted(data["per_source"].items()):
                    print(f"  {src:<12} {n}")
                for row in data["collector_state"]:
                    flag = "ok" if not row["last_error"] else f"ERR {row['last_error'][:60]}"
                    print(f"  state {row['key']:<28} {row['last_ok_at'] or '-'}  {flag}")
            return EXIT_OK

        if args.cmd == "events":
            rows = store.events(limit=args.limit, source=args.source, since=args.since)
            _print_events(rows, args.json)
            return EXIT_OK

        if args.cmd == "prune":
            deleted = store.prune(int(config.get("retention_days") or 0))
            text_days = int(config.get("vision.retention_text_days", 7) or 0)
            texts = store.prune_shot_text(text_days) if text_days else 0
            if args.json:
                print(json.dumps({"deleted": deleted, "texts": texts,
                                  "retention_days": config.get("retention_days"),
                                  "text_days": text_days}, ensure_ascii=False))
            else:
                print(f"prune: deleted={deleted} (retention_days={config.get('retention_days')})"
                      f" ocr_text_pruned={texts} (text_days={text_days})")
            return EXIT_OK

        if args.cmd == "redact":
            return _store_redact(config, store, args)

        if args.cmd == "todo-add":
            return _store_todo_add(store, args)

        if args.cmd == "todos":
            rows = store.todos(status=args.status, limit=args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    flag = "人改" if row["human_override"] else "   "
                    card = row["board_card_id"] or "-"
                    print(f"{row['id']:<28} {row['status']:<8} {flag} card={card:<12}"
                          f" {row['title'][:70]}")
            return EXIT_OK

        if args.cmd == "todo":
            detail = store.todo_detail(args.id)
            if detail is None:
                if args.json:
                    print(json.dumps({"error": "not-found", "id": args.id}, ensure_ascii=False))
                else:
                    print(f"没有这条待办: {args.id}")
                return EXIT_FATAL
            if args.json:
                print(json.dumps(detail, ensure_ascii=False, indent=2))
            else:
                td = detail["todo"]
                flag = "（人改）" if td["human_override"] else ""
                print(f"{td['id']}  [{td['status']}{flag}]  {td['title']}")
                print(f"  为什么: {td.get('why') or '-'}  置信度: {td.get('confidence')}")
                print(f"  依据 {len(detail['evidence'])} 条事件：")
                for ev in detail["evidence"]:
                    if ev.get("missing"):
                        print(f"    {ev['id']}  <missing>（原事件已不在库中）")
                    else:
                        print(f"    #{ev['id']}  {ev['ts'][:19]}  {ev['source']}/{ev['kind']}"
                              f"  {json.dumps(ev['payload'], ensure_ascii=False)[:120]}")
                print(f"  改动记录 {len(detail['revisions'])} 条：")
                for rv in detail["revisions"]:
                    print(f"    {rv['at'][:19]}  {rv['author']:<5} {rv['field']}:"
                          f" {rv['before'] or '-'} → {rv['after'] or '-'}  ({rv['reason'] or '-'})")
            return EXIT_OK

        if args.cmd == "todo-revisions":
            rows = store.todo_revisions(todo_id=args.id, limit=args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(f"{row['at'][:19]}  {row['author']:<5} {row['todo_id']:<28} "
                          f"{row['field']}: {row['before'] or '-'} → {row['after'] or '-'}"
                          f"  ({row['reason'] or '-'})")
            return EXIT_OK

        if args.cmd == "todo-set":
            ok = store.set_todo_status(args.id, args.status, human=True, reason="cli")
            if args.json:
                print(json.dumps({"id": args.id, "status": args.status, "ok": ok}, ensure_ascii=False))
            else:
                print(f"{args.id} → {args.status}" if ok else f"没有这条待办: {args.id}")
            return EXIT_OK if ok else EXIT_FATAL

        if args.cmd == "doctor":
            items: list[dict] = []

            def check(name: str, ok: bool, detail: str) -> None:
                items.append({"name": name, "ok": bool(ok), "detail": detail})

            check("config", config.path.is_file(), str(config.path))
            writable = os.access(config.root, os.W_OK) if config.root.exists() else False
            check("root", config.root.is_dir() and writable, f"{config.root} (可写={writable})")
            try:
                store.conn.execute("SELECT 1").fetchone()
                check("db", True, str(config.db_path))
            except Exception as exc:
                check("db", False, str(exc))
            check("wl-paste", shutil.which("wl-paste") is not None,
                  shutil.which("wl-paste") or "未安装 wl-clipboard")
            check("pyatspi", importlib.util.find_spec("pyatspi") is not None, "无障碍栈")
            sessions_root = Path(str(config.get("sessions_root") or "")).expanduser()
            check("sessions_root", sessions_root.is_dir(), str(sessions_root))
            for root_str in (config.collector_get("fs", "roots") or []):
                p = Path(str(root_str)).expanduser()
                check(f"fs-root:{p.name}", p.is_dir(), str(p))

            ready = all(i["ok"] for i in items)
            if args.json:
                print(json.dumps({"ready": ready, "items": items}, ensure_ascii=False, indent=2))
            else:
                for item in items:
                    print(f"{'OK ' if item['ok'] else 'NO '} {item['name']:<24} {item['detail']}")
                print("ready" if ready else "NOT ready")
            return EXIT_OK if ready else EXIT_FATAL

        raise UsageError(f"unknown subcommand {args.cmd}")
    except (StoreError, UsageError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


# --------------------------------------------------------------------------- notify（M2）
def main_report(argv: list[str] | None = None) -> int:
    """报表层（M3）：机器只重写自动区块，人改留痕。语义见 INTERFACE.md §2.5 / §4.7。"""
    parser = argparse.ArgumentParser(prog="hub-report", description="info-hub 报表层（M3）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--report", default=None, help="报表名（默认取 config report.name）")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("generate", help="按系统数据重写自动区块（author=hub）")
    sub.add_parser("show", help="打印报表全文")
    sub.add_parser("revisions", help="列出修改记录")
    p_write = sub.add_parser("write", help="人手改（author=human）")
    p_write.add_argument("--text", default=None, help="整篇替换")
    p_write.add_argument("--file", default=None, help="从文件读入整篇替换")
    p_write.add_argument("--append", default=None, help="追加一段（不改动已有内容）")
    p_write.add_argument("--reason", default="cli")
    p_rev = sub.add_parser("diff", help="看某次修订的 diff")
    p_rev.add_argument("--rev", type=int, default=0)
    args = parser.parse_args(argv)
    cmd = args.cmd or "show"

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        R = views.module(config, "report")
    except views.ViewError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        store.close()
        return EXIT_FATAL

    try:
        if cmd == "generate":
            result = R.generate(config, store, reason="cli", name=args.report)
            _emit(f"report {result['report']}: " +
                  (f"changed rev=#{result['rev']} author={result['author']}" if result["changed"]
                   else "没有变化（未留痕）"), args.quiet)
            _json_out(result, args.json)
            return EXIT_OK

        if cmd == "show":
            text = R.read_report(config, args.report)
            if args.json:
                print(json.dumps({"report": R.report_path(config, args.report).stem,
                                  "path": str(R.report_path(config, args.report)),
                                  "text": text}, ensure_ascii=False))
            else:
                print(text, end="" if text.endswith("\n") else "\n")
            return EXIT_OK

        if cmd == "revisions":
            limit = int(config.get("report.revisions_show") or 20)
            rows = R.revisions(config, store, args.report, limit=limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                if not rows:
                    print("（还没有修改记录）")
                for row in rows:
                    print(f"#{row['rev']:<3} {row['at']} {row['author']:<5} {(row['reason'] or '-'):<12}"
                          f" {row['before_hash'][:8]}→{row['after_hash'][:8]}")
            return EXIT_OK

        if cmd == "write":
            before = R.read_report(config, args.report)
            if args.file:
                text = Path(args.file).expanduser().read_text(encoding="utf-8")
            elif args.text is not None:
                text = args.text
            elif args.append is not None:
                text = before.rstrip("\n") + "\n\n" + args.append.rstrip("\n") + "\n"
            else:
                raise UsageError("write 需要 --text / --file / --append 之一")
            result = R.write_report(config, store, text, author="human",
                                    reason=args.reason, name=args.report)
            _emit(f"report {result['report']}: " +
                  (f"changed rev=#{result['rev']}" if result["changed"] else "没有变化（未留痕）"),
                  args.quiet)
            _json_out(result, args.json)
            return EXIT_OK

        if cmd == "diff":
            row = store.report_revision(R.report_path(config, args.report).stem,
                                        args.rev or None)
            if row is None:
                raise UsageError("没有这次修订")
            if args.json:
                print(json.dumps(row, ensure_ascii=False, indent=2))
            else:
                print(f"#{row['rev']} {row['at']} {row['author']} {row['reason'] or '-'}")
                print(row["diff"] or "（无 diff）")
            return EXIT_OK

        raise UsageError(f"unknown subcommand {cmd}")
    except R.ReportError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except (StoreError, UsageError, OSError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


def main_digest(argv: list[str] | None = None) -> int:
    """加工层（M4b）：把窗口内系统数据交给**可替换的命令**归纳，拿回带证据指针的条目。

    加工层换谁 = 换 `digest.command`（换模型不改代码）；输出必须挂真实事件 id，否则丢条。
    语义见 INTERFACE.md §4.9。
    """
    parser = argparse.ArgumentParser(prog="hub-digest", description="info-hub 归纳层（M4b）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="归纳一次（默认动作）")
    p_run.add_argument("--window-days", type=int, default=None)
    p_run.add_argument("--force", action="store_true", help="同一份输入也重新归纳（花 token）")
    p_run.add_argument("--dry-run", action="store_true", help="只组装提示词，不调加工层、不写库")
    sub.add_parser("show", help="看已有归纳记录")
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        D = views.module(config, "digest")
    except views.ViewError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        store.close()
        return EXIT_FATAL

    try:
        if cmd == "run":
            result = D.run(config, store, window_days=args.window_days,
                           force=bool(getattr(args, "force", False)),
                           dry_run=bool(getattr(args, "dry_run", False)))
            if result.get("reason") == "ok":
                _emit(f"digest #{result['digest_id']} 条目 {len(result['items'])}"
                      f" 丢弃无依据 {result['dropped']} 耗时 {result.get('elapsed_ms')}ms"
                      f"（后端 {result.get('backend')}）", args.quiet)
                for item in result["items"]:
                    _emit(f"  · {item['text']}  evidence={item['evidence']}", args.quiet)
            else:
                _emit(f"digest skip reason={result['reason']} ({result.get('detail', '')})",
                      args.quiet)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            elif result.get("reason") == "dry-run" and not args.quiet:
                print(result["prompt"], end="")
            return EXIT_OK

        if cmd == "show":
            rows = store.digests(limit=10)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                if not rows:
                    print("（还没有归纳记录）")
                for row in rows:
                    print(f"#{row['id']} {row['ts']} 窗口 {row['window_days']}天"
                          f" 条目 {len(row['items'])} 丢弃 {row['dropped']}"
                          f" 输入 {row['input_hash']} 后端 {row['backend']}")
                    for item in row["items"]:
                        print(f"  · {item['text']}  evidence={item['evidence']}")
            return EXIT_OK

        raise UsageError(f"unknown subcommand {cmd}")
    except (D.DigestError, StoreError, UsageError, OSError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


def main_ui(argv: list[str] | None = None) -> int:
    """本地控制台（M3）：只读系统数据 + 可手改报表。语义见 INTERFACE.md §2.6。"""
    parser = argparse.ArgumentParser(prog="hub-ui", description="info-hub 本地控制台（M3）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None, help="0 = 让内核挑端口")
    parser.add_argument("--report", default=None)
    parser.add_argument("--once", action="store_true", help="处理完一个请求就退出（验收用）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    try:
        # 控制台本身是 view 插件（S16）：未安装/未启用/加载失败都由 views 层折成 ViewError。
        serve = views.entry(config, "ui")
        result = serve(config, store, host=args.host, port=args.port, once=args.once,
                       report_name=args.report)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return EXIT_OK
    except (views.ViewError, OSError, RuntimeError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


def main_notify(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-notify", description="info-hub 通知层（M2）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只评估不发送、不写台账")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("dispatch")   # 评估一次（默认动作）
    sub.add_parser("status")     # 摊开五道门的现状
    p_test = sub.add_parser("test", help="自检：真弹一条探针通知（绕开门 1–8，不碰待办，台账记 kind=probe）")
    p_test.add_argument("--dry-run", action="store_true", help="只回报会发什么：不弹、不记账")
    sub.add_parser("effectiveness")  # M4a：按类别的有效性 + 降频判定
    p_act = sub.add_parser("act", help="M2b：把通知上的按钮动作回写成待办状态")
    p_act.add_argument("dbus_id", type=int, help="gdbus Notify 返回的系统通知 id")
    p_act.add_argument("action",
                       help="完成|acted | 忽略|ignore | 忽略此类|ignore-kind | 稍后|snooze | 打开|open")
    p_sup = sub.add_parser("suppress", help="M2c：压住某类通知若干天（到期自动恢复）")
    p_sup.add_argument("kind", help="通知类别（= 待办幂等键的规则段，如 fs-repeat）")
    p_sup.add_argument("--days", type=int, default=None, help="压多少天（缺省取 notify.suppress_days）")
    p_sup.add_argument("--note", default=None)
    p_unsup = sub.add_parser("unsuppress", help="M2c：解除某类的抑制")
    p_unsup.add_argument("kind")
    p_sups = sub.add_parser("suppressions", help="M2c：列出抑制（默认只看生效中的）")
    p_sups.add_argument("--all", action="store_true", help="连已到期的也列出来")
    p_listen = sub.add_parser("listen", help="M2b：监听系统通知动作回调（常驻，用 --timeout 收工）")
    p_listen.add_argument("--once", action="store_true", help="处理第一个动作后退出")
    p_listen.add_argument("--timeout", type=float, default=None, help="跑多少秒后退出（不填=一直跑）")
    args = parser.parse_args(argv)
    cmd = args.cmd or "dispatch"

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        from .notify import dispatch, status as notify_status

        if cmd == "dispatch":
            result = dispatch(config, store, dry_run=args.dry_run)
            if result["sent"]:
                _emit(f"sent kind={result['kind']} todo={result['todo']['id']} :: {result['body']}",
                      args.quiet)
            elif result.get("error"):
                _emit(f"send FAILED kind={result['kind']}: {result['error']}", args.quiet)
            else:
                _emit(f"skip reason={result['reason']} ({result['detail']})", args.quiet)
            _json_out(result, args.json)
            return EXIT_OK

        if cmd == "test":
            # 自检：绕开门，真弹一条探针通知并记台账（PROJECT.md §15 的 M2 判据）
            from .notify import probe as notify_probe
            result = notify_probe(config, store, dry_run=args.dry_run)
            if result["sent"]:
                _emit(f"自检已弹 kind=probe 后端={result['backend']}"
                      f" 系统通知 id={result['notification_id']}"
                      f" 台账 #{result['ledger_id']}", args.quiet)
            elif result["error"]:
                _emit(f"自检**没弹出去**（{result['error']}）—— 台账不记", args.quiet)
            else:
                _emit(f"自检 {result['detail']}", args.quiet)
            _json_out(result, args.json)
            return EXIT_OK

        if cmd == "status":
            data = notify_status(config, store)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(f"开关      {'开' if data['enabled'] else '关'}   后端 {data['backend']}"
                      f"   需界面事件 {data['require_ui_event']}")
                print(f"勿扰      {data.get('dnd')}   空闲 {data.get('idle_s')}s")
                print(f"待提示待办 {data['pending_todos']}   已弹总数 {data['shown_total']}"
                      f"   近 1 小时 {data['shown_1h']}")
                print(f"最近一条  {data['last_shown'] or '-'}")
                win = data.get("latest_window_event")
                print(f"最新界面事件 {win['ts'] + ' #' + str(win['id']) if win else '（还没有）'}")
                for row in data["notifications"][:5]:
                    flag = "已弹" if row["shown"] else "失败"
                    print(f"  [{flag}] {row['ts']} {row['kind']:<24} {row['body'][:60]}")
                if data.get("downshifted"):
                    print(f"降频中    {', '.join(data['downshifted'])}")
                print(f"按钮      {' / '.join(data.get('actions') or []) or '（无）'}"
                      f"   稍后 {data.get('snooze_minutes')} 分钟")
            return EXIT_OK

        if cmd == "act":
            from .notify import ActionError, act
            try:
                info = act(config, store, args.dbus_id, args.action)
            except ActionError as exc:
                print(f"fatal: {exc}", file=sys.stderr)
                return EXIT_FATAL
            if info.get("kind") and info.get("suppressed_until"):
                _emit(f"act dbus_id={info['dbus_id']} action={info['action']}"
                      f" → todo {info['todo_id']} = {info['status']}；类别 {info['kind']}"
                      f" 压到 {info['suppressed_until']}（{info['days']} 天）", args.quiet)
            else:
                _emit(f"act dbus_id={info['dbus_id']} action={info['action']}"
                      f" → todo {info['todo_id']} = {info['status']}", args.quiet)
            _json_out(info, args.json)
            return EXIT_OK

        if cmd == "suppress":
            from .notify import ActionError, suppress
            try:
                info = suppress(config, store, args.kind, days=args.days, note=args.note)
            except (ActionError, StoreError) as exc:
                print(f"fatal: {exc}", file=sys.stderr)
                return EXIT_FATAL
            _emit(f"suppress {info['kind']} → {info['until']}（{info['days']} 天）", args.quiet)
            _json_out(info, args.json)
            return EXIT_OK

        if cmd == "unsuppress":
            from .notify import unsuppress
            info = unsuppress(config, store, args.kind)
            _emit(f"unsuppress {info['kind']} removed={info['removed']}"
                  f"（原来是 {info['was_until'] or '-'}）", args.quiet)
            _json_out(info, args.json)
            return EXIT_OK

        if cmd == "suppressions":
            from .notify import suppressions
            rows = suppressions(store, active_only=not args.all)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                if not rows:
                    print("（没有抑制）")
                for row in rows:
                    print(f"  {row['kind']:<26} 到 {row['until']}  {row.get('source') or '-'}"
                          f"  {row.get('note') or ''}")
            return EXIT_OK

        if cmd == "listen":
            from .notify import ActionError, listen
            try:
                info = listen(config, store, once=bool(args.once), timeout_s=args.timeout)
            except ActionError as exc:
                print(f"fatal: {exc}", file=sys.stderr)
                return EXIT_FATAL
            _emit(f"listen stopped={info['stopped']} signals={info['signals']}"
                  f" acted={info['acted']} ignored={info['ignored']}"
                  f" opened={info.get('opened', 0)} snoozed={info.get('snoozed', 0)}"
                  f" suppressed={info.get('suppressed', 0)} closed={info['closed']}"
                  f" errors={len(info['errors'])}", args.quiet)
            for err in info["errors"]:
                _emit(f"  ! {err}", args.quiet)
            _json_out(info, args.json)
            return EXIT_OK

        if cmd == "effectiveness":
            from .notify import effectiveness
            data = effectiveness(config, store)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(f"窗口 {data['window_days']} 天 · 判定期 {data['ignore_after_hours']}h ·"
                      f" 降频阈值 >{data['threshold']:.0%}（样本 ≥{data['min_samples']}）"
                      f" · 降频后每天 {data['downshift_max_per_day']} 条")
                if not data["kinds"]:
                    print("（窗口内没有已弹通知）")
                for row in data["kinds"]:
                    rate = "-" if row["ignored_rate"] is None else f"{row['ignored_rate']:.0%}"
                    print(f"  {row['kind']:<26} 弹 {row['shown']:<3} 响应 {row['acted']:<3}"
                          f" 忽略 {row['ignored']:<3} 待判 {row['pending']:<3}"
                          f" 忽略率 {rate:<5} {'⇒ 降频' if row['downshifted'] else ''}")
            return EXIT_OK

        raise UsageError(f"unknown subcommand {cmd}")
    except (StoreError, UsageError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


def main_week(argv: list[str] | None = None) -> int:
    """周报（M4c）：把归纳条目 / 待办 / 通知按周聚合，落一份只读周报文件。语义见 INTERFACE.md §4.10。"""
    parser = argparse.ArgumentParser(prog="hub-week", description="info-hub 周报（M4c）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="生成周报（默认动作）")
    p_run.add_argument("--weeks", type=int, default=1, help="连报几周（默认 1）")
    p_run.add_argument("--week-of", default=None, help="该周内任意一天 YYYY-MM-DD（默认本周）")
    p_run.add_argument("--dry-run", action="store_true", help="只渲染并打印，不落盘")
    sub.add_parser("list", help="列出已生成的周报")
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        W = views.module(config, "week")
    except views.ViewError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        store.close()
        return EXIT_FATAL

    try:
        if cmd == "run":
            data = W.run(config, store, weeks=args.weeks, week_of=args.week_of,
                         dry_run=bool(args.dry_run))
            if args.quiet:
                pass
            elif data["reason"] == "dry-run":
                print(data["text"], end="")
            else:
                print(f"week {data['path']} 区间 {data['start'][:10]} → {data['end'][:10]}"
                      f" 事件 {data['event_total']} 待办 +{data['todos_created']}"
                      f"/完成 {data['todos_done']} 通知 {data['notifications_shown']}"
                      f" 归纳 {len(data['digests'])} 条/{data['items_total']} 项")
            if args.json:
                slim = {k: v for k, v in data.items() if k != "text"}
                print(json.dumps(slim, ensure_ascii=False, sort_keys=True), flush=True)
            return EXIT_OK

        if cmd == "list":
            rows = W.list_weeks(config)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                if not rows:
                    print("（还没有周报）")
                for row in rows:
                    print(f"{row['name']:<28} {row['bytes']:>7} B  {row['mtime']}")
            return EXIT_OK

        raise UsageError(f"unknown subcommand {cmd}")
    except (StoreError, UsageError, W.WeekError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


def main_screencast(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-screencast", description="info-hub 静默截屏（M7）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("consent", help="一次性「共享屏幕」同意（会弹对话框，之后存 restore_token 不再弹）")
    sub.add_parser("status", help="看同意状态、落盘目录与配置")
    p_grab = sub.add_parser("grab", help="立刻取一帧（真机自证用；会受活跃度/间隔约束吗？不 —— 本命令直接取）")
    p_grab.add_argument("--out", default=None, help="输出路径（默认 <screens>/<日期>/<时间>.jpg）")
    sub.add_parser("prune", help="按 keep_days 删过期图（留元数据）")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return EXIT_USAGE
    try:
        config = load(args.config)
        config.ensure_dirs()
    except ConfigError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        SC = derived.module(config, "screencast")
    except DerivedError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        if args.cmd == "consent":
            data = SC.consent(config)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, sort_keys=True), flush=True)
            elif not args.quiet:
                print(f"已同意：node={data['node_id']} size={data['size']}"
                      f" token={'有' if data['restore_token'] else '（没拿到，下次还会弹）'}"
                      f" 落盘 {data['token_file']}", file=sys.stderr)
                if data["restore_token"]:
                    print("下一步：把 hub.config.yaml 的 collect.screenshot.enabled 改成 true 即开始取帧"
                          "（interval_s 与活跃门在 collector 里；不活跃完全不取）", file=sys.stderr)
                else:
                    print("没拿到持久令牌：下次取帧还会弹同意框；采集器在没令牌时**不会**弹框，只会记失败。",
                          file=sys.stderr)
            return EXIT_OK if data["restore_token"] else EXIT_FATAL

        if args.cmd == "status":
            token = SC.load_token(config)
            root = SC.shots_dir(config)
            shots = sorted(root.rglob("*.jpg")) if root.is_dir() else []
            total = sum(p.stat().st_size for p in shots)
            data = {"enabled": bool(config.collector_get("screenshot", "enabled", False)),
                    "backend": config.collector_get("screenshot", "backend", "screencast"),
                    "consent": bool(token.get("restore_token")),
                    "consent_at": token.get("at"), "node_id": token.get("node_id"),
                    "size": token.get("size"), "token_file": str(SC.token_path(config)),
                    "dir": str(root), "shots": len(shots), "bytes": total,
                    "oldest": shots[0].stat().st_mtime if shots else None,
                    "newest": shots[-1].stat().st_mtime if shots else None,
                    "keep_days": int(config.collector_get("screenshot", "keep_days", 7) or 7)}
            if args.json:
                print(json.dumps(data, ensure_ascii=False, sort_keys=True), flush=True)
            elif not args.quiet:
                print(f"截屏 {'开' if data['enabled'] else '关'} backend={data['backend']}"
                      f" 同意={'有' if data['consent'] else '无'}"
                      f" 图 {data['shots']} 张/{data['bytes']} B 留 {data['keep_days']} 天", file=sys.stderr)
            return EXIT_OK

        if args.cmd == "grab":
            out = args.out or (SC.shots_dir(config) / _dt.now().astimezone().strftime("%Y-%m-%d")
                               / f"{_dt.now().astimezone().strftime('%H%M%S')}.jpg")
            info = SC.grab(config, out)
            if args.json:
                print(json.dumps(info, ensure_ascii=False, sort_keys=True), flush=True)
            elif not args.quiet:
                print(f"已取帧 {info['path']} {info['width']}x{info['height']}"
                      f" {info['bytes']} B backend={info['backend']}", file=sys.stderr)
            return EXIT_OK

        if args.cmd == "prune":
            data = SC.prune(config)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, sort_keys=True), flush=True)
            elif not args.quiet:
                print(f"prune 删 {len(data['removed'])} 张（释放 {data['freed_bytes']} B）"
                      f" 留 {data['kept']} 张 / {data['keep_days']} 天", file=sys.stderr)
            return EXIT_OK

        raise UsageError(f"unknown subcommand {args.cmd}")
    except (SC.ScreencastError, UsageError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL


# --------------------------------------------------------------------------- M10：转译层
def _activity_derive(config, store, day: str) -> list[dict]:
    """从 events 重算某天的片段并落库（幂等：同输入两次逐字节相同）。语义见 INTERFACE.md §4.15。"""
    from . import behavior as B

    version = str(config.get("behavior.def_version", "ep-1") or "ep-1")
    weights = config.get("behavior.focus_weights", None)
    start, end = B.day_bounds(day)
    idle_active_s = config.get("behavior.idle_active_s", 180)
    rows = B.segment(
        B.samples(store.activity_samples(start, end),
                  idle_active_s=float(idle_active_s) if idle_active_s is not None else None),
        interval_s=int(config.collector_get("activity", "interval_s", 60) or 60),
        gap_s=int(config.get("behavior.gap_s", 300) or 300),
        min_active_s=int(config.get("behavior.min_active_s", 60) or 60),
        switch_per_min_full=float(config.get("behavior.switch_per_min_full", 1.0) or 1.0),
        weights=weights if isinstance(weights, dict) else {},
        def_version=version,
    )
    store.episodes_replace(day, version, rows)
    return rows


def _activity_table(rows: list[dict]) -> list[str]:
    from .behavior import hhmm

    lines = []
    for row in sorted(rows, key=lambda r: r["started_at"]):
        lines.append(f"  {hhmm(row['started_at'])}–{hhmm(row['ended_at'])}"
                     f"  {row['active_s'] // 60:>3} 分  专注 {row['focus']:>3}"
                     f"  切 {row['switch_count']:>3} 次  空档 {row['gap_max_s']:>4}s")
    return lines


def main_vision(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-vision", description="画面语义层（M11）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="单轮最多读几张")
    parser.add_argument("--dry-run", action="store_true", help="只跑不写")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--dry-run", action="store_true")
    p_lab = sub.add_parser("labels")
    p_lab.add_argument("--day", default=None)
    p_lab.add_argument("--limit", type=int, default=50)
    sub.add_parser("stats")
    sub.add_parser("deps")
    p_cls = sub.add_parser("classify")
    p_cls.add_argument("text", nargs="+")
    p_vlm = sub.add_parser("vlm")
    p_vlm.add_argument("--limit", type=int, default=None)
    p_vlm.add_argument("--dry-run", action="store_true")
    p_act = sub.add_parser("act")
    p_act.add_argument("--day", default=None)
    p_act.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        config.ensure_dirs()
    except ConfigError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        V = derived.module(config, "vision")
    except DerivedError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    if args.cmd == "classify":                 # 不需要库：纯词表判定
        got = V.classify(V.normalize(" ".join(args.text)),
                         min_hits=int(config.get("vision.min_hits", V.DEFAULT_MIN_HITS)))
        if args.json:
            print(json.dumps(got, ensure_ascii=False))
        else:
            print(f"{got['category']}  score={got['score']}  hits={got['hits']}")
        return EXIT_OK

    if args.cmd == "deps":
        try:
            V.engine(config)
            ok, detail = True, "rapidocr 可用"
        except V.VisionUnavailable as exc:
            ok, detail = False, str(exc)
        if args.json:
            print(json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False))
        else:
            print(f"{'OK ' if ok else 'MISS'} {detail}")
        return EXIT_OK if ok else EXIT_FATAL

    try:
        store = Store(config.db_path)
        store.migrate()
    except StoreError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        if args.cmd == "labels":
            rows = store.shot_labels(day=args.day, limit=args.limit)
            if args.json:
                print(json.dumps({"labels": rows}, ensure_ascii=False, indent=2))
            else:
                for r in rows:
                    hits = ",".join(f"{k}{v}" for k, v in (r["hits"] or {}).items()) or "-"
                    print(f"{r['ts'][11:16]}  {r['category']:<8} {r['score']:.2f}  {hits}")
                if not rows:
                    print("（还没有类别，先跑 bin/hub-vision run）")
            return EXIT_OK

        if args.cmd == "stats":
            st = store.vision_stats()
            if args.json:
                print(json.dumps(st, ensure_ascii=False, indent=2))
            else:
                cats = "，".join(f"{k} {v}" for k, v in st["by_category"].items()) or "无"
                print(f"已标注 {st['labeled']} 张（规则命中 {st['from_rules']} 张）：{cats}")
                print(f"保留原文 {st['with_raw_text']} 条（vision.keep_raw="
                      f"{bool(config.get('vision.keep_raw', False))}）")
            return EXIT_OK

        if args.cmd == "vlm":
            from . import vlm as M
            result = M.run(config, store, limit=args.limit, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(M.summary_line(result))
                if result.get("pending"):
                    v = result["verdicts"]
                    print(f"  工作 {v['work']} 张 / 玩 {v['play']} 张 / 判不出 {v['unknown']} 张"
                          f"（模型 {result.get('model') or '-'}，版本 {result['version']}）")
            return EXIT_OK

        if args.cmd == "act":
            st = store.verdict_stats(day=args.day)
            rows = store.shot_verdicts(day=args.day, limit=args.limit)
            if args.json:
                print(json.dumps({"stats": st, "verdicts": rows}, ensure_ascii=False, indent=2))
                return EXIT_OK
            if not st["total"]:
                print("还没有画面粗判（先跑 bin/hub-vision vlm）")
                return EXIT_OK
            by = st["by_verdict"]
            print(f"已粗判 {st['total']} 张：工作 {by.get('work', 0)} 张 / "
                  f"玩 {by.get('play', 0)} 张 / 判不出 {by.get('unknown', 0)} 张；"
                  f"还没判 {st['pending']} 张")
            for r in rows:
                label = {"work": "工作", "play": "玩"}.get(r["verdict"], "判不出")
                print(f"  {r['ts'][:16].replace('T', ' ')}  {label:<4} "
                      f"{r['img_w']}x{r['img_h']}  {r['latency_ms']}ms  {r['raw'] or ''}")
            return EXIT_OK

        result = V.run(config, store, limit=getattr(args, "limit", None),
                       dry_run=getattr(args, "dry_run", False))
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(V.summary_line(result))
            for cat, n in sorted(result.get("categories", {}).items()):
                print(f"  {cat:<9} {n} 张")
        return EXIT_OK
    finally:
        store.close()


def main_activity(argv: list[str] | None = None) -> int:
    """转译层（M10）：把活动采样翻成「什么时候开始干活 / 多专注 / 多久」。语义见 INTERFACE.md §2.10。"""

    parser = argparse.ArgumentParser(
        prog="hub-activity",
        description="info-hub 转译层（M10）：什么时候在干活 / 多久 / 专注度")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("sample", help="采一条活跃采样（在场/离开 + 窗口集合哈希）")
    p_ep = sub.add_parser("episodes", help="重算并落库某天的片段")
    p_ep.add_argument("--day", default=None, help="YYYY-MM-DD（默认今天）")
    p_day = sub.add_parser("day", help="白话汇总某天（默认今天）")
    p_day.add_argument("--date", default=None, help="YYYY-MM-DD（默认今天）")

    args = parser.parse_args(argv)
    cmd = args.cmd or "day"

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        from . import behavior as B
        try:
            A = derived.module(config, "activity")
        except DerivedError as exc:
            print(f"fatal: {exc}", file=sys.stderr)
            return EXIT_FATAL

        if cmd == "sample":
            try:
                fresh = A.collect(config, store)
            except sources.CollectorError as exc:
                print(f"fatal: {exc}", file=sys.stderr)
                return EXIT_FATAL
            if bool(config.get("collect.camera.enabled", False)):
                C = derived.module(config, "camera")
                try:
                    cam = C.sample(config)
                    fresh = list(fresh or []) + [make_event("camera", "camera.sample", cam)]
                except Exception as exc:        # 摄像头永远不拖垮活动采样
                    _emit(f"cam: 跳过（{type(exc).__name__}: {exc}）", args.quiet)
            inserted = 0
            if fresh:
                inserted, _dupes = store.insert_events(fresh)
            if args.json:
                print(json.dumps({"events": fresh, "inserted": inserted},
                                 ensure_ascii=False, sort_keys=True))
            elif not args.quiet:
                act = next((e for e in fresh if e["source"] == "activity"), None)
                cam = next((e for e in fresh if e["source"] == "camera"), None)
                if act:
                    payload = act["payload"]
                    print(f"{'在场' if payload['active'] else '离开'}"
                          f"  idle={payload['idle_s']}s  应用 {len(payload['apps'])} 个"
                          f"  集合 {payload['frame_hash']}", flush=True)
                if cam:
                    c = cam["payload"]
                    label = {"away": "不在座", "present": "在座但没看屏幕", "facing": "看屏幕"}[c["state"]]
                    print(f"  {label}  脸 {c['faces']} 张  占比 {c['face_ratio']}"
                          f"  偏航 {c['yaw']}  {c['latency_ms']}ms", flush=True)
                if not act and not cam:
                    _emit("（上一分钟内已采过，跳过）", args.quiet)
            return EXIT_OK

        day = (getattr(args, "day", None) or getattr(args, "date", None)
               or _dt.now().astimezone().strftime("%Y-%m-%d"))
        version = str(config.get("behavior.def_version", "ep-1") or "ep-1")

        if cmd == "episodes":
            rows = _activity_derive(config, store, day)
            if args.json:
                print(json.dumps({"day": day, "def_version": version, "episodes": rows},
                                 ensure_ascii=False, sort_keys=True))
            elif not args.quiet:
                print(f"{day}  口径 {version}  片段 {len(rows)} 段", flush=True)
                for line in _activity_table(rows):
                    print(line)
            return EXIT_OK

        if cmd != "day":
            raise UsageError(f"unknown subcommand {cmd}")

        start, end = B.day_bounds(day)
        day_samples = store.activity_samples(start, end)      # 只看这一天，不看全库
        rows = store.episodes(day=day, def_version=version)
        if not rows and day_samples:
            rows = _activity_derive(config, store, day)

        day_start_s = int(config.get("behavior.day_start_min_s", 600) or 600)
        data = B.summarize(rows, day_start_s=day_start_s)
        data.update({"day": day, "def_version": version, "detail": rows,
                     "samples": len(day_samples), "sampled_all": store.activity_span()})
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
            return EXIT_OK
        if args.quiet:
            return EXIT_OK

        if data["samples"] == 0:
            print(f"{day}：这一天没有活动采样。"
                  "先在 info-hub 目录跑 bin/hub-activity sample，或启动 info-hub-activity.timer。")
            return EXIT_OK
        if data["episodes"] == 0:
            print(f"{day}：有 {data['samples']} 条采样，但没有够长的连续在场片段（每段要 ≥"
                  f"{config.get('behavior.min_active_s', 60)} 秒在场）。")
            return EXIT_OK

        print(f"{day}  共 {data['episodes']} 段")
        if data["start_at"]:
            print(f"  开始干活  {B.hhmm(data['start_at'])}")
        else:
            print(f"  开始干活  （在场累计不足 {day_start_s // 60} 分钟，不算开工）")
        hours, mins = divmod(data["active_min"], 60)
        print(f"  在场合计  {hours} 小时 {mins} 分" if hours else f"  在场合计  {mins} 分")
        # M18：扣掉「手机游戏同时在跑」的那部分（游戏白名单在前台够久 ⇒ 不算在电脑上干活）
        feat_ver = str(config.get("behavior.feat_version", "feat-1") or "feat-1")
        game_by_hour = {r["hour"]: r.get("phone_game_min")
                        for r in store.features_day(day, feat_ver)}
        if any(v for v in game_by_hour.values()):
            clean = B.clean_minutes(
                rows, game_by_hour,
                threshold=float(config.get("behavior.phone_game_exclude_min", 30) or 30))
            ch, cm = divmod(clean["clean"], 60)
            print(f"  扣掉手机  {clean['game']} 分（手机游戏同时在跑） ⇒ 电脑上净 "
                  + (f"{ch} 小时 {cm} 分" if ch else f"{cm} 分"))
        print(f"  平均专注  {data['avg_focus']}")
        hands = _input_totals(store, start, end)
        if hands["samples"]:
            print(f"  手上     按键 {hands['keys']} 次 / 点击 {hands['clicks']} 次"
                  f" / 光标 {_fmt_wan(hands['dist_px'])} px")
        for line in _activity_table(rows):
            print(line)
        print("  （专注度只由在场/切换/空档三样算出，不读屏幕内容，"
              "分不清写代码和刷视频）")
        return EXIT_OK
    except (DerivedError, StoreError, UsageError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


def _input_totals(store, start: str, end: str) -> dict:
    """某天键盘/鼠标的计数（M13）。只读聚合值，库里没有 keycode。"""
    keys = clicks = wheel = dist = 0
    n = 0
    for ev in store.events(source="input", since=start, limit=5000):
        if ev["ts"] > end:
            continue
        payload = ev["payload"]
        keys += int(payload.get("keys", 0))
        clicks += int(payload.get("clicks", 0))
        wheel += int(payload.get("wheel", 0))
        dist += int(payload.get("dist_px", 0))
        n += 1
    return {"samples": n, "keys": keys, "clicks": clicks, "wheel": wheel, "dist_px": dist}


def _fmt_wan(n: int) -> str:
    return f"{n / 10000:.1f} 万" if n >= 10000 else str(n)


def main_input(argv: list[str] | None = None) -> int:
    """M13 输入节奏：读 /dev/input 的**计数**（键盘按下次数 / 点击 / 光标距离）。"""
    parser = argparse.ArgumentParser(
        prog="hub-input", description="info-hub 输入节奏（M13）：键盘鼠标的计数，不读内容")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--window-s", type=float, default=None)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("listen", help="常驻：每 window_s 采一条 input.sample")
    sub.add_parser("sample", help="只采一个窗口并打印（不进库，调参用）")
    sub.add_parser("probe", help="列出可读的输入设备")
    p_st = sub.add_parser("stats")
    p_st.add_argument("--day", default=None)

    args = parser.parse_args(argv)
    cmd = args.cmd or "probe"

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    try:
        I = derived.module(config, "input")
    except DerivedError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    if cmd == "probe":
        try:
            devs = I.devices()
        except Exception as exc:
            print(f"fatal: {exc}", file=sys.stderr)
            return EXIT_FATAL
        if args.json:
            print(json.dumps({"devices": devs}, ensure_ascii=False, indent=2))
        else:
            print(f"可读设备 {len(devs)} 个（需要 input 组；见 INTERFACE.md §4.18）")
            for d in devs:
                caps = ",".join(k for k, ok in (("key", d["has_key"]), ("rel", d["has_rel"]),
                                                ("abs", d["has_abs"])) if ok) or "-"
                print(f"  {d['path']:<24} {d['name'][:34]:<34} {caps}")
        return EXIT_OK

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    window = float(args.window_s if args.window_s is not None
                   else config.get("collect.input.window_s", 60) or 60)

    try:
        if cmd == "sample":
            try:
                sampler = I.Sampler()
            except PermissionError as exc:
                print(f"fatal: {exc}", file=sys.stderr)
                return EXIT_FATAL
            try:
                payload = sampler.read_window(window)
            finally:
                sampler.close()
            payload["devices"] = len(I.devices())
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"{payload['secs']}s 内：按键 {payload['keys']} 次 / 点击 {payload['clicks']} 次"
                      f" / 滚轮 {payload['wheel']} / 光标 {payload['dist_px']} px")
            return EXIT_OK

        if cmd == "stats":
            day = args.day or __import__("datetime").datetime.now().astimezone().strftime("%Y-%m-%d")
            start, end = f"{day}T00:00:00+08:00", f"{day}T23:59:59+08:00"
            data = _input_totals(store, start, end)
            if args.json:
                print(json.dumps({"day": day, **data}, ensure_ascii=False))
            else:
                print(f"{day}  采样 {data['samples']} 条：按键 {data['keys']} / 点击 {data['clicks']}"
                      f" / 滚轮 {data['wheel']} / 光标 {_fmt_wan(data['dist_px'])} px")
            return EXIT_OK

        if cmd != "listen":
            raise UsageError(f"unknown subcommand {cmd}")

        if not bool(config.get("collect.input.enabled", True)):
            _emit("input: collect.input.enabled=false，退出", args.quiet)
            return EXIT_OK
        try:
            sampler = I.Sampler()
        except PermissionError as exc:
            print(f"fatal: {exc}", file=sys.stderr)
            return EXIT_FATAL
        devs = len(I.devices())
        _emit(f"input: 开始采样，{devs} 个设备，窗口 {window:.0f}s", args.quiet)
        try:
            while True:
                payload = sampler.read_window(window)
                payload["devices"] = devs
                ev = make_event("input", "input.sample", payload, privacy="normal")
                store.insert_events([ev])
                if not args.quiet:
                    print(f"{ev['ts']}  按键 {payload['keys']} / 点击 {payload['clicks']}"
                          f" / 光标 {payload['dist_px']} px", flush=True)
        except KeyboardInterrupt:
            return EXIT_OK
        finally:
            sampler.close()
    except (StoreError, UsageError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


# --------------------------------------------------------------------------- M14：校准
def main_calib(argv: list[str] | None = None) -> int:
    """M14：自报 + 三路特征 AUC。语义见 INTERFACE.md §2.13。"""
    parser = argparse.ArgumentParser(prog="hub-calib", description="info-hub 校准（自报 vs 特征）")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    p_rate = sub.add_parser("rate", help="记一次自报：1 专注 / 2 一般 / 3 走神")
    p_rate.add_argument("level", type=int, choices=[1, 2, 3])
    p_rep = sub.add_parser("report", help="算各路特征的 AUC")
    p_rep.add_argument("--days", type=int, default=2)
    p_rep.add_argument("--window-min", type=int, default=60)

    args = parser.parse_args(argv)
    cmd = args.cmd or "report"
    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        K = derived.module(config, "calib")
    except DerivedError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        if cmd == "rate":
            ev = make_event("self", "self.report", {"level": int(args.level)}, privacy="normal")
            store.insert_events([ev])
            if args.json:
                print(json.dumps({"event": ev}, ensure_ascii=False))
            elif not args.quiet:
                print(f"记下了：{K.LEVELS[int(args.level)]}")
            return EXIT_OK

        if cmd != "report":
            raise UsageError(f"unknown subcommand {cmd}")

        data = K.report(store, days=args.days, window_min=args.window_min)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return EXIT_OK
        n = data["reports"]
        print(f"自报 {n} 条（专注 {data['by_level']['专注']} / 一般 {data['by_level']['一般']} / "
              f"走神 {data['by_level']['走神']}），每条的窗口 {data['window_min']} 分钟")
        if n < 6:
            print("样本太少（<6 条），先攒两天再算；现在算出来的数不可信。")
            return EXIT_OK
        names = {"keys_per_win": "按键速率", "clicks_per_win": "点击速率",
                 "dist_per_win": "光标距离", "straightness": "轨迹直度",
                 "presence": "在场比例", "facing": "看屏幕比例"}
        print("          特征        AUC   说明")
        for k, v in data["auc"].items():
            if v is None:
                print(f"  {names.get(k, k):<10}   --   数据不足")
                continue
            hint = "越相关" if v >= 0.65 else ("反向" if v <= 0.35 else "没区分度")
            print(f"  {names.get(k, k):<10}  {v:.2f}   {hint}")
        print("（AUC 1.0 = 完美分开「专注」和「不专注」，0.5 = 跟扔硬币一样）")
        return EXIT_OK
    except (StoreError, UsageError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


# --------------------------------------------------------------------------- M16：行为特征
def _phone_day_line(rows: list[dict]) -> None:
    """M17：手机屏幕当天累计一行（没有快照就不打印，不拿 0 冒充）。"""
    if not rows or rows[0].get("phone_day_min") is None:
        return
    minutes = int(rows[0]["phone_day_min"])
    top = rows[0].get("phone_day_top") or "?"
    print(f"手机屏幕：{minutes} 分 ≈ {minutes / 60:.1f} 小时，最常用 {top}")


def main_features(argv: list[str] | None = None) -> int:
    """M16：每小时一行的人指标。语义见 INTERFACE.md §2.14 / §4.22。"""
    parser = argparse.ArgumentParser(prog="hub-features", description="info-hub 行为特征层")
    parser.add_argument("--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    for name in ("build", "show"):
        sp = sub.add_parser(name)
        sp.add_argument("--day", default=None)
        sp.add_argument("--date", default=None, help="同 --day（旧写法）")
        sp.add_argument("--quiet", action="store_true")
        sp.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    cmd = args.cmd or "show"

    try:
        config, store = _store_open(args)
    except (ConfigError, StoreError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    try:
        F = derived.module(config, "features")
    except DerivedError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    day = (getattr(args, "day", None) or getattr(args, "date", None)
           or _dt.now().astimezone().strftime("%Y-%m-%d"))
    version = str(config.get("behavior.feat_version", F.VERSION) or F.VERSION)
    try:
        if cmd == "build":
            rows = F.build(store, day, version)
            store.features_replace(day, version, rows)
            filled = [r for r in rows if any(r[k] is not None for k in
                      ("presence_ratio", "facing_ratio", "keys", "phone_notif", "vision_cat"))
                      or r["switch_count"]]
            if args.json:
                print(json.dumps({"day": day, "version": version, "rows": len(rows),
                                  "filled": len(filled), "features": rows},
                                 ensure_ascii=False, sort_keys=True))
            elif not args.quiet:
                print(f"{day}  口径 {version}  写 {len(rows)} 行（有数据的 {len(filled)} 小时）")
            return EXIT_OK

        if cmd != "show":
            raise UsageError(f"unknown subcommand {cmd}")

        rows = store.features_day(day, version) or F.build(store, day, version)
        if args.json:
            print(json.dumps({"day": day, "version": version, "features": rows},
                             ensure_ascii=False, indent=2))
            return EXIT_OK
        if not args.quiet:
            print(f"{day}  口径 {version}")
            _phone_day_line(rows)          # M17：手机屏幕先报一行，再看逐小时
            for line in F.table(rows):
                print(line)
            print("（coverage 六位：在场/看屏/输入/窗口/手机/截图；缺的那路是「-」不是 0）")
        return EXIT_OK
    except (StoreError, UsageError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        store.close()


# --------------------------------------------------------------------------- M8：浏览历史
def main_history(argv: list[str] | None = None) -> int:
    """`hub-history`：浏览历史的分类与总结（只读；这类信息只进报表，不推送）。"""
    from . import history as H

    parser = argparse.ArgumentParser(prog="hub-history",
                                     description="info-hub 浏览历史分类/总结（M8）")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd")

    p_sum = sub.add_parser("summary", help="用途/站点分布 + 一条总结")
    p_sum.add_argument("--days", type=float, default=1)
    p_sum.add_argument("--top", type=int, default=10)
    p_sum.add_argument("--json", action="store_true")

    p_cls = sub.add_parser("classify", help="单条 URL 的分类（调试用）")
    p_cls.add_argument("url")
    p_cls.add_argument("--title", default="")
    p_cls.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return EXIT_USAGE

    try:
        if args.cmd == "classify":
            data = H.classify(args.url, args.title)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, sort_keys=True))
            else:
                print(f"{data['site']} · {data['purpose']}")
            return EXIT_OK

        config, store = _store_open(args)
        data = H.summarize(store, config, days=args.days, top=args.top)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        else:
            print(data["line"])
            for row in data["top_sites"]:
                print(f"  {row['site']:<28} {row['count']}")
        return EXIT_OK
    except (ConfigError, StoreError, UsageError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

# --------------------------------------------------------------------------- plugin
def main_plugin(argv: list[str] | None = None) -> int:
    """插件宿主：逐插件状态（INTERFACE.md §10）。坏插件只记 reason，退出码仍 0。"""
    parser = argparse.ArgumentParser(prog="hub-plugin", description="插件清单与加载状态（INTERFACE.md §10）")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="逐个插件的状态")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--kind", default=None, choices=list(plugins.KINDS))

    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        rows = plugins.list_plugins(config)
    except (ConfigError, plugins.PluginError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL

    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.json:
        print(json.dumps({"plugins": rows}, ensure_ascii=False, sort_keys=True), flush=True)
        return EXIT_OK
    for row in rows:
        detail = row["reason"] or ""
        flag = "" if row["enabled"] else "off"
        print(f"{row['name']:<28} {row['kind'] or '-':<10} {row['status']:<9} {flag:<4} {detail}".rstrip())
    return EXIT_OK

# ---------------------------------------------------------------------- deliver
def main_deliver(argv: list[str] | None = None) -> int:
    """投递台账命令 `bin/hub-deliver`（S4）。语义与退出码见 INTERFACE.md §11 / deliver.py。"""
    return deliver.main(argv)

