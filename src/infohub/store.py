"""SQLite 存储层。表与列语义见 INTERFACE.md §5，失败语义见 §6。"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .events import canonical, iso, iso_from_epoch

UNSET: Any = object()


def _iso_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  source      TEXT NOT NULL,
  kind        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  privacy     TEXT NOT NULL DEFAULT 'normal',
  ingested_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS events_dedupe ON events(source, fingerprint, ts);
CREATE INDEX IF NOT EXISTS events_ts     ON events(ts DESC);
CREATE INDEX IF NOT EXISTS events_source ON events(source, ts DESC);

CREATE TABLE IF NOT EXISTS collector_state (
  key         TEXT PRIMARY KEY,
  cursor      TEXT,
  last_ok_at  TEXT,
  last_error  TEXT
);

CREATE TABLE IF NOT EXISTS todos (
  id              TEXT PRIMARY KEY,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  source_event_ids TEXT NOT NULL DEFAULT '[]',
  title           TEXT NOT NULL,
  why             TEXT,
  confidence      REAL,
  status          TEXT NOT NULL DEFAULT 'open',
  human_override  INTEGER NOT NULL DEFAULT 0,
  board_card_id   TEXT
);

CREATE TABLE IF NOT EXISTS todo_revisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id   TEXT NOT NULL,
    at        TEXT NOT NULL,
    author    TEXT NOT NULL,              -- human | hub
    field     TEXT NOT NULL,              -- status | snooze_until
    before    TEXT,
    after     TEXT,
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS todo_revisions_todo ON todo_revisions(todo_id, id);

CREATE TABLE IF NOT EXISTS report_revisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  report      TEXT NOT NULL,
  rev         INTEGER NOT NULL,
  at          TEXT NOT NULL,
  author      TEXT NOT NULL,
  reason      TEXT,
  before_hash TEXT NOT NULL,
  after_hash  TEXT NOT NULL,
  diff        TEXT NOT NULL DEFAULT '',
  UNIQUE(report, rev)
);

CREATE TABLE IF NOT EXISTS notifications (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  kind            TEXT NOT NULL,
  body            TEXT NOT NULL,
  basis_event_ids TEXT NOT NULL DEFAULT '[]',
  shown           INTEGER NOT NULL DEFAULT 0,
  action          TEXT
);

CREATE TABLE IF NOT EXISTS digests (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  window_days     INTEGER NOT NULL,
  backend         TEXT NOT NULL,
  prompt_hash     TEXT NOT NULL,
  input_hash      TEXT NOT NULL,
  output          TEXT NOT NULL,
  items           TEXT NOT NULL DEFAULT '[]',
  dropped         INTEGER NOT NULL DEFAULT 0,
  elapsed_ms      INTEGER NOT NULL DEFAULT 0,
  error           TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS digests_input ON digests(input_hash);

CREATE TABLE IF NOT EXISTS kind_suppressions (
  kind        TEXT PRIMARY KEY,
  until       TEXT NOT NULL,
  note        TEXT,
  source      TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
"""

# M10 派生层（INTERFACE.md §5.9）：行为片段。可从 events 全量重算，events 永不因它改动。
SCHEMA_M10 = """
CREATE TABLE IF NOT EXISTS episodes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  day           TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  ended_at      TEXT NOT NULL,
  active_s      INTEGER NOT NULL,
  span_s        INTEGER NOT NULL,
  presence      REAL NOT NULL,
  stability     REAL NOT NULL,
  continuity    REAL NOT NULL,
  focus         INTEGER NOT NULL,
  switch_count  INTEGER NOT NULL DEFAULT 0,
  gap_max_s     INTEGER NOT NULL DEFAULT 0,
  sample_count  INTEGER NOT NULL DEFAULT 0,
  def_version   TEXT NOT NULL,
  ingested_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS episodes_key ON episodes(day, started_at, def_version);
CREATE INDEX IF NOT EXISTS episodes_day ON episodes(day, started_at);
"""


SCHEMA_M11 = """
CREATE TABLE IF NOT EXISTS shot_text (
  sha256      TEXT PRIMARY KEY,
  ts          TEXT NOT NULL,
  chars       INTEGER NOT NULL,
  lines       INTEGER NOT NULL,
  text        TEXT,                 -- 仅 vision.keep_raw=1 时才有原文
  text_sha    TEXT NOT NULL,
  engine      TEXT NOT NULL,
  version     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shot_text_ts ON shot_text(ts);

CREATE TABLE IF NOT EXISTS shot_labels (
  sha256      TEXT PRIMARY KEY,
  ts          TEXT NOT NULL,
  category    TEXT NOT NULL,
  score       REAL NOT NULL,
  hits        TEXT NOT NULL,        -- JSON：{"code":3,"doc":1}
  method      TEXT NOT NULL,        -- rules | missing | error:xxx
  version     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shot_labels_ts ON shot_labels(ts);
CREATE INDEX IF NOT EXISTS shot_labels_cat ON shot_labels(category, ts);
"""


SCHEMA_M12 = """
CREATE TABLE IF NOT EXISTS shot_verdicts (
  sha256      TEXT NOT NULL,
  version     TEXT NOT NULL,        -- vlm-<算法版本>；换模型/换提示词 ⇒ 新行，可并存对比
  ts          TEXT NOT NULL,
  verdict     TEXT NOT NULL,        -- work | play | unknown
  raw         TEXT,                 -- 模型原话（截断），用来复盘判错
  model       TEXT,                 -- 模型文件名
  method      TEXT NOT NULL,        -- vlm | error:xxx
  img_w       INTEGER,              -- 实际喂进模型的尺寸（缩图后）
  img_h       INTEGER,
  latency_ms  INTEGER,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (sha256, version)
);
CREATE INDEX IF NOT EXISTS verdicts_ts ON shot_verdicts(ts);
CREATE INDEX IF NOT EXISTS verdicts_v ON shot_verdicts(verdict, ts);
"""


class StoreError(Exception):
    """存储层错误（不可写 / 库损坏等）。"""


SCHEMA_DELIVER = """
CREATE TABLE IF NOT EXISTS deliveries (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  channel     TEXT NOT NULL,
  mode        TEXT,
  kind        TEXT,
  todo_id     TEXT,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL DEFAULT '',
  options     TEXT,
  layout      TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',
  action      TEXT,
  reply       TEXT,
  reply_at    TEXT,
  acked_at    TEXT,
  remote_ref  TEXT,
  lease_owner TEXT,
  lease_until TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  archived_at TEXT,
  attachments TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_status  ON deliveries(status, created_at DESC);
CREATE INDEX IF NOT EXISTS deliveries_channel ON deliveries(channel, status);
CREATE INDEX IF NOT EXISTS deliveries_todo    ON deliveries(todo_id);
"""

# 投递台账的列顺序（INTERFACE.md §11.1）；`add_delivery` 按它拼 INSERT。
DELIVERY_COLUMNS = (
    "id", "created_at", "updated_at", "channel", "mode", "kind", "todo_id", "title", "body",
    "options", "layout", "status", "action", "reply", "reply_at", "acked_at", "remote_ref",
    "lease_owner", "lease_until", "attempts", "last_error", "archived_at", "attachments",
)


def _delivery_row(row) -> dict:
    """整行转 dict，并把 `options`/`layout`/`attachments` 三个 JSON 列解析回值（坏 JSON 保持原串）。"""
    item = dict(row)
    for key in ("options", "layout", "attachments"):
        raw = item.get(key)
        if raw is None:
            continue
        try:
            item[key] = json.loads(raw)
        except (TypeError, ValueError):
            pass
    return item


SCHEMA_M16 = """
CREATE TABLE IF NOT EXISTS features (
    day TEXT NOT NULL, hour INTEGER NOT NULL, feat_version TEXT NOT NULL,
    presence_ratio REAL, facing_ratio REAL, switch_count INTEGER,
    keys INTEGER, clicks INTEGER, dist_px INTEGER, straightness REAL,
    iki_median_ms INTEGER, iki_cv_ms REAL, pauses INTEGER, burst_max INTEGER, bs_ratio REAL,
    phone_notif INTEGER, phone_notif_min INTEGER,
    vision_cat TEXT, focus INTEGER, coverage TEXT,
    PRIMARY KEY (day, hour, feat_version)
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(str(self.path), timeout=10.0)
        except sqlite3.Error as exc:  # pragma: no cover
            raise StoreError(f"打开库失败 {self.path}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=10000")

    # ---- schema -----------------------------------------------------------
    def migrate(self) -> None:
        self.conn.executescript(SCHEMA + SCHEMA_M10 + SCHEMA_M11 + SCHEMA_M12 + SCHEMA_DELIVER
                               + SCHEMA_M16)
        self._upgrade()
        self.conn.commit()

    def _upgrade(self) -> None:
        """列级迁移（CREATE TABLE IF NOT EXISTS 不会补列）。"""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(todos)")}
        if "dedupe_key" not in cols:                  # M1：待办幂等键
            self.conn.execute("ALTER TABLE todos ADD COLUMN dedupe_key TEXT")
        if "notified_at" not in cols:                 # M2：已通知时间（NULL = 还没提示过）
            self.conn.execute("ALTER TABLE todos ADD COLUMN notified_at TEXT")
        if "snooze_until" not in cols:                # M2d：「稍后」推迟到什么时候（未到点不进候选）
            self.conn.execute("ALTER TABLE todos ADD COLUMN snooze_until TEXT")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS todos_dedupe ON todos(dedupe_key)")
        fcols = {row[1] for row in self.conn.execute("PRAGMA table_info(features)")}
        if fcols and "phone_day_min" not in fcols:    # M17：手机屏幕日累计（整天每行同值）
            self.conn.execute("ALTER TABLE features ADD COLUMN phone_day_min INTEGER")
        if fcols and "phone_day_top" not in fcols:
            self.conn.execute("ALTER TABLE features ADD COLUMN phone_day_top TEXT")
        if fcols and "phone_game_min" not in fcols:   # M18：这一小时手机游戏在前台多少分钟
            self.conn.execute("ALTER TABLE features ADD COLUMN phone_game_min INTEGER")
        ncols = {row[1] for row in self.conn.execute("PRAGMA table_info(notifications)")}
        if "todo_id" not in ncols:                    # M4a：通知 → 待办的指针（算有效性要它）
            self.conn.execute("ALTER TABLE notifications ADD COLUMN todo_id TEXT")
        if "dbus_id" not in ncols:                    # M2b：系统通知 id（动作回调要按它回查）
            self.conn.execute("ALTER TABLE notifications ADD COLUMN dbus_id INTEGER")
        dcols = {row[1] for row in self.conn.execute("PRAGMA table_info(deliveries)")}
        if dcols and "archived_at" not in dcols:      # 控制台消息中心：归档时间（NULL=未归档）
            self.conn.execute("ALTER TABLE deliveries ADD COLUMN archived_at TEXT")
        if dcols and "attachments" not in dcols:      # 控制台消息中心：附件（数据根相对路径的 JSON 数组）
            self.conn.execute("ALTER TABLE deliveries ADD COLUMN attachments TEXT")


    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    # ---- events -----------------------------------------------------------
    def insert_events(self, events: list[dict]) -> tuple[int, int]:
        """写入事件；返回 (新插入, 重复丢弃)。去重键见 INTERFACE.md §5.1。"""
        inserted = duplicates = 0
        now = iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for ev in events:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO events (ts, source, kind, payload, fingerprint, privacy, ingested_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ev.get("ts") or now, ev.get("source", "unknown"), ev.get("kind", "unknown"),
                     canonical(ev.get("payload") or {}), ev.get("fingerprint", ""),
                     ev.get("privacy", "normal"), now),
                )
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    duplicates += 1
            self.conn.commit()
        except sqlite3.Error as exc:
            self.conn.rollback()
            raise StoreError(f"写入事件失败: {exc}") from exc
        return inserted, duplicates

    def count_events(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def events(self, limit: int = 20, source: str | None = None,
               since: str | None = None) -> list[dict]:
        sql = "SELECT id, ts, source, kind, payload, privacy FROM events WHERE 1=1"
        args: list[Any] = []
        if source:
            sql += " AND source = ?"
            args.append(source)
        if since:
            sql += " AND datetime(ts) >= datetime(?)"
            args.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for row in self.conn.execute(sql, args):
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"])
            except (TypeError, ValueError):
                pass
            out.append(item)
        return out

    def iter_events(self, source: str | None = None, limit: int = 0) -> list[dict]:
        """按 id **升序**取整行（历史清洗/审计用：顺序稳定、可小步试跑）；`limit<=0` = 全部。"""
        sql = ("SELECT id, ts, source, kind, payload, fingerprint, privacy, ingested_at"
               " FROM events WHERE 1=1")
        args: list[Any] = []
        if source:
            sql += " AND source = ?"
            args.append(source)
        sql += " ORDER BY id ASC"
        if limit and int(limit) > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        out = []
        for row in self.conn.execute(sql, args):
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"])
            except (TypeError, ValueError):
                pass
            out.append(item)
        return out

    def update_event_payload(self, event_id: int, payload: dict) -> int:
        """只改一行的 `payload`（验证码脱敏清洗专用，INTERFACE.md §5.14）。

        `ts`/`source`/`kind`/`fingerprint`/`privacy`/`ingested_at` 一律不动，**绝不 DELETE 行**。
        """
        cur = self.conn.execute("UPDATE events SET payload = ? WHERE id = ?",
                                (canonical(payload), int(event_id)))
        self.conn.commit()
        return int(cur.rowcount or 0)

    def event_counts(self, since: str | None = None, until: str | None = None) -> dict[str, int]:
        """区间内按来源计数（周报用；Q27: 区间的半开写法 `since <= ts < until`）。"""
        sql = "SELECT source AS source, COUNT(*) AS n FROM events WHERE 1=1"
        args: list[Any] = []
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        if until:
            sql += " AND ts < ?"
            args.append(until)
        sql += " GROUP BY source ORDER BY source"
        return {r["source"]: r["n"] for r in self.conn.execute(sql, args)}

    def prune(self, retention_days: int) -> int:
        """按保留期删事件；永不触及报表与修订（INTERFACE.md §2.2）。"""
        cur = self.conn.execute(
            "DELETE FROM events WHERE datetime(ts) < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)

    # ---- collector state --------------------------------------------------
    def state_get(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM collector_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["cursor"] = json.loads(item["cursor"]) if item["cursor"] else None
        except (TypeError, ValueError):
            item["cursor"] = None
        return item

    def state_put(self, key: str, cursor: Any = UNSET, ok: Any = UNSET,
                  error: Any = UNSET) -> None:
        """部分更新游标/状态；未给出的字段保持原值。"""
        row = self.state_get(key) or {"key": key, "cursor": None, "last_ok_at": None, "last_error": None}
        if cursor is not UNSET:
            row["cursor"] = cursor
        if ok is True:
            row["last_ok_at"] = iso()
            row["last_error"] = None
        if error is not UNSET and error is not None:
            row["last_error"] = str(error)
        if ok is False and error is UNSET:
            row["last_error"] = row.get("last_error") or "unknown error"
        self.conn.execute(
            "INSERT INTO collector_state (key, cursor, last_ok_at, last_error) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET cursor=excluded.cursor,"
            " last_ok_at=excluded.last_ok_at, last_error=excluded.last_error",
            (key,
             canonical(row["cursor"]) if row.get("cursor") is not None else None,
             row.get("last_ok_at"), row.get("last_error")),
        )
        self.conn.commit()

    # ---- 待办（L2，M1） ----------------------------------------------------
    def upsert_todo(self, cand: dict) -> tuple[str, bool]:
        """按 dedupe_key 幂等建待办。返回 (id, 是否新建)。已存在则只刷新证据集合。"""
        key = cand["dedupe_key"]
        row = self.conn.execute("SELECT id FROM todos WHERE dedupe_key = ?", (key,)).fetchone()
        now = iso()
        if row is not None:
            self.conn.execute(
                "UPDATE todos SET updated_at = ?, source_event_ids = ? WHERE id = ?",
                (now, canonical(sorted(cand.get("source_event_ids") or [])), row["id"]))
            self.conn.commit()
            return str(row["id"]), False
        todo_id = cand.get("id") or f"t-{key.split(':')[-1]}-{abs(hash(key)) % 10**6:06d}"
        self.conn.execute(
            "INSERT INTO todos (id, created_at, updated_at, source_event_ids, title, why,"
            " confidence, status, human_override, board_card_id, dedupe_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, NULL, ?)",
            (todo_id, now, now, canonical(sorted(cand.get("source_event_ids") or [])),
             cand["title"], cand.get("why", ""), float(cand.get("confidence") or 0.0), key))
        self.conn.commit()
        return todo_id, True

    def todos(self, status: str | None = None, limit: int = 200,
              needing_board: bool = False) -> list[dict]:
        sql = "SELECT * FROM todos WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if needing_board:
            sql += " AND board_card_id IS NULL AND status = 'open'"
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for row in self.conn.execute(sql, args):
            item = dict(row)
            try:
                item["source_event_ids"] = json.loads(item["source_event_ids"])
            except (TypeError, ValueError):
                item["source_event_ids"] = []
            out.append(item)
        return out

    def todo_exists(self, dedupe_key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM todos WHERE dedupe_key = ?", (dedupe_key,)).fetchone() is not None

    def set_todo_status(self, todo_id: str, status: str, human: bool = True,
                        reason: str | None = None) -> bool:
        """改状态：置 human_override，规则引擎不再覆盖（INTERFACE.md §5.4）。

        M3b：**每次真改动都留痕**（`todo_revisions`，author = `human` / `hub`）——报表有改动记录，清单也得有。
        """
        before = self._todo_field(todo_id, "status")
        cur = self.conn.execute(
            "UPDATE todos SET status = ?, human_override = ?, updated_at = ? WHERE id = ?",
            (status, 1 if human else 0, iso(), todo_id))
        self.conn.commit()
        if cur.rowcount == 1 and before != status:
            self.write_todo_revision(todo_id, "status", before, status,
                                    author="human" if human else "hub", reason=reason)
        return cur.rowcount == 1

    def _todo_field(self, todo_id: str, field: str):
        """读某个待办字段的当前值（字段名由调用方写死，不走参数拼接白名单之外的输入）。"""
        assert field in ("status", "snooze_until", "title", "id")
        row = self.conn.execute(f"SELECT {field} FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return None if row is None else row[0]

    def write_todo_revision(self, todo_id: str, field: str, before, after,
                            author: str = "human", reason: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO todo_revisions (todo_id, at, author, field, before, after, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (todo_id, iso(), author, field,
             None if before is None else str(before), None if after is None else str(after), reason))
        self.conn.commit()
        return int(cur.lastrowid)

    def todo_revisions(self, todo_id: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM todo_revisions"
        args: tuple = ()
        if todo_id:
            sql += " WHERE todo_id = ?"
            args = (todo_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(sql, args + (int(limit),))]

    # ---- M3c：待办的证据链（每条待办必须能摊开成「哪几条原始事件」）----------------
    def todo_evidence(self, todo_id: str, limit: int = 20) -> list[dict]:
        """把 `todos.source_event_ids` 摊开成真实的 `events` 行。

        口径（INTERFACE.md §5.8）：**只读**；id 在本表里查不到就**如实标 `missing`**（不静默丢掉）——
        证据链出现空洞本身就是该看见的信号。
        """
        row = self.conn.execute("SELECT source_event_ids FROM todos WHERE id = ?", (todo_id,)).fetchone()
        if row is None:
            return []
        try:
            ids = json.loads(row["source_event_ids"] or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        out: list[dict] = []
        for eid in list(ids)[: int(limit)]:
            ev = self.conn.execute(
                "SELECT id, ts, source, kind, payload, privacy FROM events WHERE id = ?",
                (eid,)).fetchone()
            if ev is None:
                out.append({"id": eid, "missing": True})
                continue
            item = dict(ev)
            item["missing"] = False
            out.append(item)
        return out

    def todo_detail(self, todo_id: str, evidence_limit: int = 20,
                    revision_limit: int = 20) -> dict | None:
        row = self.conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        if row is None:
            return None
        todo = dict(row)
        return {"todo": todo,
                "evidence": self.todo_evidence(todo_id, limit=evidence_limit),
                "revisions": self.todo_revisions(todo_id, limit=revision_limit)}

    def set_todo_board_card(self, todo_id: str, card_id: str) -> None:
        self.conn.execute("UPDATE todos SET board_card_id = ?, updated_at = ? WHERE id = ?",
                          (card_id, iso(), todo_id))
        self.conn.commit()

    def set_todo_snooze(self, todo_id: str, until: str,
                        reason: str | None = None) -> bool:
        """「稍后」：推到 `until` 之后再说。**同时清掉 `notified_at`** ⇒ 到点会重新进候选（M2d）。

        M3b：留痕（`field=snooze_until`），author 固定 `human`（只有人点得到这个按钮/入口）。
        """
        before = self._todo_field(todo_id, "snooze_until")
        cur = self.conn.execute(
            "UPDATE todos SET snooze_until = ?, notified_at = NULL, updated_at = ? WHERE id = ?",
            (until, iso(), todo_id))
        self.conn.commit()
        if cur.rowcount == 1:
            self.write_todo_revision(todo_id, "snooze_until", before, until,
                                     author="human", reason=reason)
        return cur.rowcount == 1

    # ---- 通知（L3，M2） ----------------------------------------------------
    def pending_notify_todos(self, limit: int = 1) -> list[dict]:
        """还没提示过的 open 待办，最新在前（INTERFACE.md §5.5）。"""
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE status = 'open' AND notified_at IS NULL"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),))
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["source_event_ids"] = json.loads(item["source_event_ids"])
            except (TypeError, ValueError):
                item["source_event_ids"] = []
            out.append(item)
        return out

    def mark_todo_notified(self, todo_id: str, when: str | None = None) -> None:
        self.conn.execute("UPDATE todos SET notified_at = ?, updated_at = ? WHERE id = ?",
                          (when or iso(), when or iso(), todo_id))
        self.conn.commit()

    def add_notification(self, kind: str, body: str, basis_event_ids: list | None = None,
                         shown: bool = False, action: str | None = None,
                         todo_id: str | None = None, dbus_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO notifications (ts, kind, body, basis_event_ids, shown, action, todo_id, dbus_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iso(), kind, body, canonical(sorted(basis_event_ids or [])), 1 if shown else 0,
             action, todo_id, dbus_id))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def notification_by_dbus_id(self, dbus_id: int) -> dict | None:
        """按系统通知 id 回查台账（M2b：动作回调 → 待办）。没有则是 None（不是错）。"""
        row = self.conn.execute(
            "SELECT * FROM notifications WHERE dbus_id = ? ORDER BY id DESC LIMIT 1",
            (int(dbus_id),)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["basis_event_ids"] = json.loads(item["basis_event_ids"])
        except (TypeError, ValueError):
            item["basis_event_ids"] = []
        return item

    def mark_notification_action(self, notification_id: int, action: str) -> None:
        """把「用户点了哪个动作」写回台账（留痕，见 INTERFACE.md §5.5）。"""
        self.conn.execute("UPDATE notifications SET action = ? WHERE id = ?",
                          (str(action), int(notification_id)))
        self.conn.commit()

    def notifications(self, limit: int = 20, shown_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM notifications WHERE 1=1"
        if shown_only:
            sql += " AND shown = 1"
        sql += " ORDER BY id DESC LIMIT ?"
        out = []
        for row in self.conn.execute(sql, (int(limit),)):
            item = dict(row)
            try:
                item["basis_event_ids"] = json.loads(item["basis_event_ids"])
            except (TypeError, ValueError):
                item["basis_event_ids"] = []
            out.append(item)
        return out

    def notification_gate_counts(self, kind: str) -> dict:
        """节流闸门要的三个计数（INTERFACE.md §4.6）。只统计真正弹出去的。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(ts) AS last FROM notifications WHERE shown = 1").fetchone()
        hour = self.conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE shown = 1"
            " AND datetime(ts) >= datetime('now', '-1 hour')").fetchone()[0]
        day_kind = self.conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE shown = 1 AND kind = ?"
            " AND date(ts, 'localtime') = date('now', 'localtime')", (kind,)).fetchone()[0]
        return {"shown_total": int(row["n"]), "last_shown": row["last"],
                "shown_1h": int(hour), "shown_today_kind": int(day_kind)}

    # ---- 类别抑制（M2c：通知上的「忽略此类」）-------------------------------
    def suppress_kind(self, kind: str, days: int = 7, note: str | None = None,
                      source: str = "cli", now: datetime | None = None) -> dict:
        """把某类通知压住 `days` 天（到期自动失效，不是永久屏蔽）。留痕见 INTERFACE.md §5.6。"""
        kind = str(kind or "").strip()
        if not kind:
            raise StoreError("suppress_kind: kind 不能为空")
        days = int(days)
        if days <= 0:
            raise StoreError("suppress_kind: days 必须是正整数")
        now = now or datetime.now().astimezone()
        until = (now + timedelta(days=days)).isoformat(timespec="seconds")
        stamp = now.isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO kind_suppressions (kind, until, note, source, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(kind) DO UPDATE SET until = excluded.until, note = excluded.note,"
            " source = excluded.source, updated_at = excluded.updated_at",
            (kind, until, note, source, stamp, stamp))
        self.conn.commit()
        return {"kind": kind, "until": until, "days": days, "note": note, "source": source}

    def kind_suppressed(self, kind: str, now: datetime | None = None) -> dict | None:
        """该类现在是否被抑制；未被抑制（含已到期）返回 None。"""
        row = self.conn.execute("SELECT * FROM kind_suppressions WHERE kind = ?",
                                (str(kind or ""),)).fetchone()
        if row is None:
            return None
        until = _iso_dt(row["until"])
        now = now or datetime.now().astimezone()
        if until is None or until <= now:
            return None
        return dict(row)

    def suppressions(self, active_only: bool = False, now: datetime | None = None) -> list[dict]:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM kind_suppressions ORDER BY until DESC")]
        if not active_only:
            return rows
        now = now or datetime.now().astimezone()
        out = []
        for row in rows:
            until = _iso_dt(row["until"])
            if until is not None and until > now:
                out.append(row)
        return out

    def clear_kind_suppression(self, kind: str) -> bool:
        cur = self.conn.execute("DELETE FROM kind_suppressions WHERE kind = ?", (str(kind or ""),))
        self.conn.commit()
        return bool(cur.rowcount)

    def add_digest(self, window_days: int, backend: str, prompt_hash: str, input_hash: str,
                   output: str, items: list | None = None, dropped: int = 0,
                   elapsed_ms: int = 0, error: str | None = None) -> int:
        """M4b：落一次「LLM 归纳」的结果。`input_hash` 唯一 ⇒ 同一份输入不重复花 token。"""
        cur = self.conn.execute(
            "INSERT INTO digests (ts, window_days, backend, prompt_hash, input_hash, output,"
            " items, dropped, elapsed_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (iso(), int(window_days), backend, prompt_hash, input_hash, output,
             canonical(items or []), int(dropped), int(elapsed_ms), error))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def find_digest(self, input_hash: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM digests WHERE input_hash = ?", (input_hash,)).fetchone()
        return self._digest_row(row) if row else None

    def digests(self, limit: int = 10, since: str | None = None) -> list[dict]:
        sql = "SELECT * FROM digests WHERE 1=1"
        args: list[Any] = []
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [self._digest_row(r) for r in self.conn.execute(sql, args)]

    @staticmethod
    def _digest_row(row) -> dict:
        item = dict(row)
        try:
            item["items"] = json.loads(item["items"])
        except (TypeError, ValueError):
            item["items"] = []
        return item

    def notification_effectiveness(self, window_days: int = 14, ignore_after_hours: int = 24,
                                   now: str | None = None) -> list[dict]:
        """按 kind 统计「弹了之后有没有人动作」（M4a；INTERFACE.md §4.6）。

        判定（全机械，供自动降频用）：
        - `acted`：通知指向的待办已 `done`，且状态更新时间（`todos.updated_at`）在通知之后；
        - `ignored`：未 acted，且距通知已过 `ignore_after_hours`；
        - `pending`：未 acted，但还在判定期内（不冤枉）；
        - `unlinked`：没有 `todo_id` 指针（老数据）⇒ **不进样本**，不算 ignored。
        `samples = acted + ignored`，`ignored_rate = ignored / samples`（samples=0 时 rate=None）。
        """
        cutoff = (datetime.now().astimezone() - timedelta(days=int(window_days))).isoformat()
        rows = self.conn.execute(
            "SELECT n.kind AS kind, n.todo_id AS todo_id, n.ts AS ts,"
            " t.status AS todo_status, t.updated_at AS todo_updated"
            " FROM notifications n LEFT JOIN todos t ON t.id = n.todo_id"
            " WHERE n.shown = 1 AND n.ts >= ?", (cutoff,)).fetchall()
        now_dt = _iso_dt(now) or datetime.now().astimezone()
        buckets: dict[str, dict] = {}
        for row in rows:
            item = buckets.setdefault(row["kind"], {
                "kind": row["kind"], "shown": 0, "acted": 0, "ignored": 0,
                "pending": 0, "unlinked": 0, "samples": 0, "ignored_rate": None})
            item["shown"] += 1
            ts = _iso_dt(row["ts"])
            acted = bool(row["todo_id"]) and row["todo_status"] == "done" \
                and (ts is None or (_iso_dt(row["todo_updated"]) or now_dt) >= ts)
            if acted:
                item["acted"] += 1
            elif not row["todo_id"]:
                item["unlinked"] += 1
            elif ts is not None and (now_dt - ts) >= timedelta(hours=int(ignore_after_hours)):
                item["ignored"] += 1
            else:
                item["pending"] += 1
            item["samples"] = item["acted"] + item["ignored"]
            item["ignored_rate"] = (round(item["ignored"] / item["samples"], 4)
                                    if item["samples"] else None)
        return [buckets[k] for k in sorted(buckets)]

    def latest_event(self, source: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, ts, source, kind FROM events WHERE source = ? ORDER BY id DESC LIMIT 1",
            (source,)).fetchone()
        return dict(row) if row is not None else None

    # ---- 报表 -------------------------------------------------------------
    def report_revisions(self, report: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, report, rev, at, author, reason, before_hash, after_hash"
            " FROM report_revisions WHERE report = ? ORDER BY rev", (report,))
        return [dict(r) for r in rows]

    def add_report_revision(self, report: str, author: str, before_hash: str,
                            after_hash: str, diff: str = "", reason: str | None = None) -> int:
        """M3 用：追加一条报表修订（人改留痕）。"""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(rev), 0) FROM report_revisions WHERE report = ?", (report,)).fetchone()
        rev = int(row[0]) + 1
        self.conn.execute(
            "INSERT INTO report_revisions (report, rev, at, author, reason, before_hash, after_hash, diff)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (report, rev, iso(), author, reason, before_hash, after_hash, diff))
        self.conn.commit()
        return rev

    def report_revision(self, report: str, rev: int | None = None) -> dict | None:
        """取某次修订（含 diff）；rev 为空则取最新一次。"""
        if rev is None:
            row = self.conn.execute(
                "SELECT * FROM report_revisions WHERE report = ? ORDER BY rev DESC LIMIT 1",
                (report,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM report_revisions WHERE report = ? AND rev = ?",
                (report, int(rev))).fetchone()
        return dict(row) if row is not None else None

    # ---- 行为片段（M10，派生层）-------------------------------------------
    def activity_samples(self, since: str | None = None,
                         until: str | None = None) -> list[dict]:
        """按时间升序取 activity 采样（payload 已解析），供 behavior.segment 重算。"""
        sql = ("SELECT ts, payload FROM events WHERE source = 'activity'"
               " AND kind = 'activity.sample'")
        args: list[Any] = []
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        if until:
            sql += " AND ts < ?"
            args.append(until)
        sql += " ORDER BY ts, id"
        out = []
        for row in self.conn.execute(sql, args):
            payload = row["payload"]
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
            out.append({"ts": row["ts"], "source": "activity",
                        "kind": "activity.sample", "payload": payload})
        return out

    def episodes_replace(self, day: str, def_version: str, rows: list[dict]) -> int:
        """重建某天某口径的片段：先删后插 ⇒ 同输入跑两次结果一致（INTERFACE.md §4.15）。"""
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("DELETE FROM episodes WHERE day = ? AND def_version = ?",
                          (day, def_version))
        now = iso()
        for row in rows:
            self.conn.execute(
                "INSERT INTO episodes (day, started_at, ended_at, active_s, span_s,"
                " presence, stability, continuity, focus, switch_count, gap_max_s,"
                " sample_count, def_version, ingested_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["day"], row["started_at"], row["ended_at"], row["active_s"],
                 row["span_s"], row["presence"], row["stability"], row["continuity"],
                 row["focus"], row["switch_count"], row["gap_max_s"], row["sample_count"],
                 row["def_version"], now))
        self.conn.commit()
        return len(rows)

    def episodes(self, day: str | None = None, def_version: str | None = None,
                 limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM episodes WHERE 1=1"
        args: list[Any] = []
        if day:
            sql += " AND day = ?"
            args.append(day)
        if def_version:
            sql += " AND def_version = ?"
            args.append(def_version)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args)]

    def activity_span(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi FROM events"
            " WHERE source = 'activity' AND kind = 'activity.sample'").fetchone()
        return {"count": int(row["n"]), "first": row["lo"], "last": row["hi"]}


    # ---- M11：画面语义 -----------------------------------------------------
    def unlabeled_shots(self, limit: int = 8) -> list[dict]:
        """还没有类别的截屏（按时间升序补历史）。"""
        rows = self.conn.execute(
            """SELECT e.ts AS ts,
                      json_extract(e.payload, '$.sha256') AS sha256,
                      json_extract(e.payload, '$.path')   AS path
                 FROM events AS e
                 LEFT JOIN shot_labels AS l
                        ON l.sha256 = json_extract(e.payload, '$.sha256')
                WHERE e.source = 'screenshot'
                  AND json_extract(e.payload, '$.sha256') IS NOT NULL
                  AND l.sha256 IS NULL
                ORDER BY e.ts, e.id
                LIMIT ?""", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def shot_text_put(self, sha256: str, ts: str, chars: int, lines: int,
                      text: str | None, text_sha: str, engine: str, version: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO shot_text
               (sha256, ts, chars, lines, text, text_sha, engine, version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sha256, ts, int(chars), int(lines), text, text_sha, engine, version, iso()))
        self.conn.commit()

    def shot_label_put(self, sha256: str, ts: str, category: str, score: float,
                       hits: dict, method: str, version: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO shot_labels
               (sha256, ts, category, score, hits, method, version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sha256, ts, category, float(score), json.dumps(hits, ensure_ascii=False, sort_keys=True),
             method, version, iso()))
        self.conn.commit()

    def shot_labels(self, day: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM shot_labels"
        args: list = []
        if day:
            sql += " WHERE substr(ts, 1, 10) = ?"
            args.append(day)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            try:
                d["hits"] = json.loads(d["hits"] or "{}")
            except ValueError:
                d["hits"] = {}
            out.append(d)
        return out

    def vision_stats(self) -> dict:
        total = int(self.conn.execute("SELECT COUNT(*) FROM shot_labels").fetchone()[0])
        by_cat = {r[0]: int(r[1]) for r in self.conn.execute(
            "SELECT category, COUNT(*) FROM shot_labels GROUP BY category ORDER BY 2 DESC")}
        with_text = int(self.conn.execute(
            "SELECT COUNT(*) FROM shot_text WHERE text IS NOT NULL").fetchone()[0])
        labeled = int(self.conn.execute(
            "SELECT COUNT(*) FROM shot_labels WHERE method = 'rules'").fetchone()[0])
        return {"labeled": total, "by_category": by_cat, "with_raw_text": with_text,
                "from_rules": labeled}

    def prune_shot_text(self, days: int) -> int:
        """删过期原文（只删 text 字段，行与计数保留）。"""
        if days <= 0:
            return 0
        cur = self.conn.execute(
            "DELETE FROM shot_text WHERE created_at < datetime('now', ?)",
            (f"-{int(days)} days",))
        self.conn.commit()
        return int(cur.rowcount or 0)

    # ---- M12：画面粗判（work / play） -------------------------------------
    def unverdict_shots(self, limit: int = 8, version: str = "vlm-1") -> list[dict]:
        """还没有该版本粗判的截屏（按时间升序补历史）。"""
        rows = self.conn.execute(
            """SELECT e.ts AS ts,
                      json_extract(e.payload, '$.sha256') AS sha256,
                      json_extract(e.payload, '$.path')   AS path
                 FROM events AS e
                 LEFT JOIN shot_verdicts AS v
                        ON v.sha256 = json_extract(e.payload, '$.sha256')
                       AND v.version = ?
                WHERE e.source = 'screenshot'
                  AND json_extract(e.payload, '$.sha256') IS NOT NULL
                  AND v.sha256 IS NULL
                ORDER BY e.ts, e.id
                LIMIT ?""", (version, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def shot_verdict_put(self, sha256: str, ts: str, verdict: str, raw: str,
                         model: str, version: str, method: str,
                         img_w: int, img_h: int, latency_ms: int) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO shot_verdicts
               (sha256, version, ts, verdict, raw, model, method, img_w, img_h, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sha256, version, ts, verdict, raw, model, method,
             int(img_w), int(img_h), int(latency_ms), iso()))
        self.conn.commit()

    def shot_verdicts(self, day: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM shot_verdicts"
        args: list = []
        if day:
            sql += " WHERE substr(ts, 1, 10) = ?"
            args.append(day)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args)]

    def verdict_stats(self, day: str | None = None) -> dict:
        where, args = ("WHERE substr(ts, 1, 10) = ?", [day]) if day else ("", [])
        by = {r[0]: int(r[1]) for r in self.conn.execute(
            f"SELECT verdict, COUNT(*) FROM shot_verdicts {where} GROUP BY verdict", args)}
        total = int(self.conn.execute(
            f"SELECT COUNT(*) FROM shot_verdicts {where}", args).fetchone()[0])
        unverdict = int(self.conn.execute(
            """SELECT COUNT(*) FROM events AS e
                 LEFT JOIN shot_verdicts AS v
                        ON v.sha256 = json_extract(e.payload, '$.sha256')
                WHERE e.source = 'screenshot'
                  AND json_extract(e.payload, '$.sha256') IS NOT NULL
                  AND v.sha256 IS NULL""").fetchone()[0])
        return {"total": total, "by_verdict": by, "pending": unverdict,
                "model": (self.conn.execute(
                    "SELECT model FROM shot_verdicts ORDER BY created_at DESC LIMIT 1").fetchone() or [None])[0]}

    # ---- M16：行为特征 --------------------------------------------------------
    def features_replace(self, day: str, version: str, rows: list[dict]) -> int:
        """整天的特征行先删后插（同输入两次逐字节相同）。"""
        cols = ("day", "hour", "feat_version", "presence_ratio", "facing_ratio", "switch_count", "keys", "clicks", "dist_px", "straightness", "iki_median_ms", "iki_cv_ms", "pauses", "burst_max", "bs_ratio", "phone_notif", "phone_notif_min", "vision_cat", "focus", "coverage", "phone_day_min", "phone_day_top", "phone_game_min",)
        sql = f"INSERT INTO features ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        with self.conn:
            self.conn.execute("DELETE FROM features WHERE day = ? AND feat_version = ?", (day, version))
            self.conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
        return len(rows)

    def features_day(self, day: str, version: str | None = None) -> list[dict]:
        sql = "SELECT * FROM features WHERE day = ?"
        args: list = [day]
        if version:
            sql += " AND feat_version = ?"
            args.append(version)
        sql += " ORDER BY hour"
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ---- 汇总 -------------------------------------------------------------
    def stats(self) -> dict:
        per_source = {r["source"]: int(r["n"]) for r in self.conn.execute(
            "SELECT source, COUNT(*) AS n FROM events GROUP BY source")}
        row = self.conn.execute("SELECT COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi FROM events").fetchone()
        state = [dict(r) for r in self.conn.execute(
            "SELECT key, last_ok_at, last_error FROM collector_state ORDER BY key")]
        return {
            "db": str(self.path),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "events_total": int(row["n"]),
            "events_first": row["lo"],
            "events_last": row["hi"],
            "per_source": per_source,
            "collector_state": state,
        }


    # ---- 投递台账（S4，INTERFACE.md §11）-------------------------------------
    def add_delivery(self, record: dict) -> dict:
        """插入一行投递；`options`/`layout` 传 Python 对象（内部存 canonical JSON）。

        列语义见 INTERFACE.md §11.1。id 由调用方给（重复 ⇒ StoreError），时间戳缺省取当前时刻。
        """
        row = {col: record.get(col) for col in DELIVERY_COLUMNS}
        now = iso()
        row["created_at"] = row["created_at"] or now
        row["updated_at"] = row["updated_at"] or row["created_at"]
        row["status"] = row["status"] or "pending"
        row["body"] = row["body"] if row["body"] is not None else ""
        row["attempts"] = int(row["attempts"] or 0)
        for key in ("options", "layout", "attachments"):
            if isinstance(row[key], (dict, list)):
                row[key] = canonical(row[key])
        try:
            self.conn.execute(
                f"INSERT INTO deliveries ({', '.join(DELIVERY_COLUMNS)})"
                f" VALUES ({', '.join(['?'] * len(DELIVERY_COLUMNS))})",
                tuple(row[col] for col in DELIVERY_COLUMNS))
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"投递 id 已存在: {row['id']}") from exc
        return self.delivery(str(row["id"])) or {}

    def delivery(self, delivery_id: str) -> dict | None:
        """取一行（`options`/`layout` 已解析成 JSON 值）；没有 ⇒ None。"""
        row = self.conn.execute("SELECT * FROM deliveries WHERE id = ?",
                                (str(delivery_id),)).fetchone()
        return None if row is None else _delivery_row(row)

    def deliveries(self, status: str | None = None, channel: str | None = None,
                   limit: int = 50, archived: bool | None = None) -> list[dict]:
        """按 `created_at` 倒序列出（`limit<=0` = 全部）；过滤条件都是精确相等。

        `archived`：None=不过滤，False=只看未归档，True=只看归档（语义见 INTERFACE.md §11.9）。
        """
        sql = "SELECT * FROM deliveries WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(str(status))
        if channel:
            sql += " AND channel = ?"
            args.append(str(channel))
        if archived is True:
            sql += " AND archived_at IS NOT NULL"
        elif archived is False:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY created_at DESC, id DESC"
        if limit and int(limit) > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [_delivery_row(r) for r in self.conn.execute(sql, args)]

    def archive_delivery(self, delivery_id: str) -> dict | None:
        """归档一行：写 archived_at（幂等，已归档的保持首次时间）；不存在 ⇒ None。"""
        now = iso()
        cur = self.conn.execute(
            "UPDATE deliveries SET archived_at = COALESCE(archived_at, ?), updated_at = ? "
            "WHERE id = ?", (now, now, str(delivery_id)))
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.delivery(str(delivery_id))

    def ack_delivery(self, delivery_id: str, action: str | None = None,
                     when: str | None = None) -> dict | None:
        """ack = 「看见了」：写 `acked_at` + `status=acked`，**不改待办状态**（INTERFACE.md §11.4）。

        幂等（§11.3）：该行 `acked_at` 已非空 ⇒ 一个字都不改，返回 `changed=False`。
        已经 reply/suppressed/expired 的行只补 `acked_at`，`status` 不被降回 `acked`。
        """
        row = self.delivery(delivery_id)
        if row is None:
            return None
        if row.get("acked_at"):
            item = dict(row)
            item["changed"] = False
            return item
        stamp = when or iso()
        cur = self.conn.execute(
            "UPDATE deliveries SET"
            " status = CASE WHEN status IN ('replied', 'suppressed', 'expired')"
            "               THEN status ELSE 'acked' END,"
            " action = COALESCE(?, action), acked_at = ?, updated_at = ?"
            " WHERE id = ? AND acked_at IS NULL",
            (action, stamp, stamp, str(delivery_id)))
        self.conn.commit()
        item = dict(self.delivery(delivery_id) or row)
        item["changed"] = bool(cur.rowcount)
        return item

    def reply_delivery(self, delivery_id: str, text: str, when: str | None = None) -> dict | None:
        """reply = 自由文本**原样**留档 + `status=replied`，不改待办状态（INTERFACE.md §11.4）。

        空文本的判定在 `deliver.py`（空 ⇒ 退出码 1 且不写库）；这里只负责写。
        重复 reply 覆盖旧文本、刷新 `reply_at`（§11.3）。
        """
        if self.delivery(delivery_id) is None:
            return None
        stamp = when or iso()
        self.conn.execute(
            "UPDATE deliveries SET reply = ?, reply_at = ?, status = 'replied', updated_at = ?"
            " WHERE id = ?", (text, stamp, stamp, str(delivery_id)))
        self.conn.commit()
        return self.delivery(delivery_id)

    def expire_delivery(self, delivery_id: str, when: str | None = None) -> dict | None:
        """作废一行：`status=expired` + 清空租约（INTERFACE.md §11.4/§11.5）。

        已是 acked/replied/suppressed 的行保留原终态（"一次终态"），但租约照样清空。
        不存在 ⇒ None。
        """
        if self.delivery(delivery_id) is None:
            return None
        stamp = when or iso()
        self.conn.execute(
            "UPDATE deliveries SET"
            " status = CASE WHEN status IN ('acked', 'replied', 'suppressed')"
            "               THEN status ELSE 'expired' END,"
            " lease_owner = NULL, lease_until = NULL, updated_at = ?"
            " WHERE id = ?", (stamp, str(delivery_id)))
        self.conn.commit()
        return self.delivery(delivery_id)

    # ---- 认领租约（S5，INTERFACE.md §11.5）----------------------------------
    def pending_deliveries(self, channel: str | None = None, limit: int = 50,
                           now: str | None = None) -> list[dict]:
        """待认领的行：status=pending 且租约为空或已过期；按 created_at 倒序（§11.5）。"""
        sql = ("SELECT * FROM deliveries WHERE status = 'pending' AND archived_at IS NULL"
               " AND (lease_owner IS NULL OR lease_until IS NULL OR lease_until <= ?)")
        args: list[Any] = [now or iso()]
        if channel:
            sql += " AND channel = ?"
            args.append(str(channel))
        sql += " ORDER BY created_at DESC, id DESC"
        if limit and int(limit) > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [_delivery_row(r) for r in self.conn.execute(sql, args)]

    def claim_delivery(self, delivery_id: str, owner: str, ttl_s: int,
                       when: str | None = None) -> dict | None:
        """把 lease_owner / lease_until 写进同一行；重复 claim ⇒ 最后一次的 owner 覆盖（§11.5）。

        只写租约三列（lease_owner / lease_until / updated_at），不动 status / acked_at / reply。
        """
        if self.delivery(delivery_id) is None:
            return None
        now_epoch = time.time() if when is None else datetime.fromisoformat(when).timestamp()
        stamp = when or iso()
        until = iso_from_epoch(now_epoch + int(ttl_s))
        cur = self.conn.execute(
            "UPDATE deliveries SET lease_owner = ?, lease_until = ?, updated_at = ?"
            " WHERE id = ?", (str(owner), until, stamp, str(delivery_id)))
        self.conn.commit()
        item = dict(self.delivery(delivery_id) or {})
        item["changed"] = bool(cur.rowcount)
        return item
