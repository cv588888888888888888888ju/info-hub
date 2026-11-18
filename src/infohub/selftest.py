"""自检：在临时目录里跑合成事件与真实采集器，不碰真实数据。INTERFACE.md §2.1 `--selftest`。"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

import yaml

from . import sources
from .bus import Bus
from .cli import run_round
from .config import Config
from .events import fingerprint, iso, make_event
from .store import Store

CHECKS: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def _synthetic_config(root: Path, fs_root: Path) -> Config:
    raw = {
        "root": str(root),
        "db": "hub.db",
        "bus": "state/bus.jsonl",
        "reports_dir": "reports",
        "log_dir": "logs",
        "log_name": "hub.log",
        "retention_days": 180,
        "sessions_root": str(root / "sessions"),
        "collect": {
            "fs": {"enabled": True, "roots": [str(fs_root)], "max_files": 10,
                   "ignore_dirs": [], "ignore_globs": ["*.tmp"], "timeout_s": 5},
            "fake-broken": {"enabled": True},
            "fake-ok": {"enabled": True},
        },
    }
    path = root / "selftest.config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return Config(raw, path)


def run(json_out: bool = False, quiet: bool = False) -> int:
    CHECKS.clear()
    tmp = Path(tempfile.mkdtemp(prefix="infohub-selftest-"))
    fs_root = tmp / "watched"
    fs_root.mkdir(parents=True)
    (fs_root / "note.md").write_text("hello", encoding="utf-8")
    (fs_root / "skip.tmp").write_text("x", encoding="utf-8")
    (tmp / "sessions").mkdir()

    try:
        config = _synthetic_config(tmp, fs_root)
        config.ensure_dirs()
        store = Store(config.db_path)
        store.migrate()
        bus = Bus(config.bus_path)

        # 1) 指纹与去重键
        ev = make_event("clipboard", "clip.copy", {"head": "abc", "chars": 3, "truncated": False})
        _check("fingerprint/stable", ev["fingerprint"] == fingerprint("clipboard", "clip.copy", ev["payload"]))

        # 2) bus 往返
        bus.emit([ev])
        _check("bus/emit-read", len(bus.read_all()) == 1)
        bus.truncate()
        _check("bus/truncate", bus.read_all() == [])

        # 3) 入库 + 重复丢弃
        ins, dup = store.insert_events([ev, dict(ev)])
        _check("store/insert", ins == 1 and dup == 1, f"inserted={ins} duplicate={dup}")

        # 4) 真实采集器（fs）＋ 失败采集器不影响其它（§6）
        real_registry = sources.registry

        def fake_registry(config=None, **kw):
            return {
                "fs": real_registry(config)["fs"],
                "fake-ok": lambda cfg, st: [make_event("fake-ok", "noop", {"n": 1}, ts=iso())],
                "fake-broken": lambda cfg, st: (_ for _ in ()).throw(RuntimeError("boom")),
            }

        sources.registry = fake_registry  # type: ignore[assignment]
        try:
            first = run_round(config, store, bus, ["fs", "fake-broken", "fake-ok"], quiet=True)
        finally:
            sources.registry = real_registry  # type: ignore[assignment]

        _check("round/broken-not-fatal", first["sources"]["fake-broken"]["ok"] is False
               and first["sources"]["fs"]["ok"] is True and first["sources"]["fake-ok"]["ok"] is True,
               json.dumps({k: v["ok"] for k, v in first["sources"].items()}))
        _check("round/ingested-2", first["ingested"] == 2, f"ingested={first['ingested']}")
        fs_events = [e for e in store.events(limit=10) if e["source"] == "fs"]
        _check("fs/one-change", len(fs_events) == 1, f"fs_events={len(fs_events)}")
        _check("fs/ignore-glob", not any(e["payload"].get("path", "").endswith(".tmp") for e in fs_events))

        # 5) 游标抑制重复
        second = run_round(config, store, bus, ["fs"], quiet=True)
        _check("fs/cursor-dedupe", second["sources"]["fs"]["emitted"] == 0,
               f"emitted={second['sources']['fs']['emitted']}")

        # 6) spool 重放幂等（模拟崩溃后重放）
        bus.emit([make_event("fake-ok", "noop", {"n": 2}, ts="2026-01-01T00:00:00+08:00")])
        replay = run_round(config, store, bus, [], quiet=True)
        _check("spool/replay-ingest", replay["ingested"] == 1, f"ingested={replay['ingested']}")
        again = run_round(config, store, bus, [], quiet=True)
        _check("spool/empty-noop", again["ingested"] == 0)

        # 7) 游标状态持久化
        state = store.state_get(f"fs:{fs_root}") or {}
        _check("state/cursor", isinstance(state.get("cursor"), dict) and "mtime" in state["cursor"])
        broken = store.state_get("run:fake-broken") or {}
        _check("state/error-recorded", "boom" in (broken.get("last_error") or ""))

        # 8) prune（确定性：插一条 2020 年的事件再按 30 天保留期删）
        store.insert_events([make_event("fake-ok", "noop", {"n": 3}, ts="2020-01-01T00:00:00+08:00")])
        deleted = store.prune(30)
        # 两条过期：2020 年的合成事件 + 上面 spool 重放用的 2026-01-01 事件
        _check("store/prune", deleted == 2, f"deleted={deleted}")

        # 9) 表齐全
        tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _check("schema/tables", {"events", "collector_state", "todos", "report_revisions", "notifications"} <= tables,
               ",".join(sorted(tables)))
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    if json_out:
        print(json.dumps({"checks": total, "passed": passed,
                          "items": [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS]},
                         ensure_ascii=False))
    elif not quiet:
        for name, ok, detail in CHECKS:
            print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail else ""))
        print(f"selftest: {passed}/{total}")
    return 0 if passed == total else 1
