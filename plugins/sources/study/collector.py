"""学习日志采集器：读 `learning_log.md` 的 `## 记录` 区，每天投一条学习脉搏事件。

契约见 INTERFACE.md §4 采集器表的 `study` 行。
纪律：
- **只读**学习日志；只写自己的游标（`collector_state` 的 `study:*`）。
- 解析失败（字段缺失、时间戳读不出）⇒ 抛 `CollectorError`，**半解析数据一条都不写**。
- 每天一条 `study.pulse`：payload 里的 `date`（运行日）保证跨天各一条。
- **幂等靠自己的游标**：游标记住「今天 + 上次 payload 指纹」；同一天再用同一份日志跑
  ⇒ 直接返回空。为什么不能只靠 `events` 的唯一索引：去重索引是
  `(source, fingerprint, ts)`，而 `ts` 每次取当前时刻 —— 同一 payload 换一秒就会被当成新行。
"""

from __future__ import annotations

import re
import signal
import threading
from datetime import date, datetime
from pathlib import Path

from infohub import events as E
from infohub.sources import CollectorError

NODE = "study"
KIND = "study.pulse"

# 记录行的第一格是 8 位十六进制 ID（learning_log.md §格式：`| 8位hex | YYYY-MM-DD HH:mm | …`）
ROW_RE = re.compile(r"^\|\s*[0-9a-fA-F]{8}\s*\|")
# 知识点（KP）形如 `高数-1.1`；学科/讲次列里出现的其它写法一律不算
KP_RE = re.compile(r"^[\w\u4e00-\u9fff]+-\d+\.\d+$")
TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _read_text(path: Path, timeout_s) -> str:
    """读日志；超过 `collect.study.timeout_s` 就抛错（契约：采集器不阻塞 > timeout_s）。"""
    if not timeout_s or threading.current_thread() is not threading.main_thread():
        return path.read_text(encoding="utf-8")

    def _on_alarm(signum, frame):
        raise CollectorError(f"读学习日志超过 collect.study.timeout_s={timeout_s}s: {path}")

    old = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, float(timeout_s))
    try:
        return path.read_text(encoding="utf-8")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _parse_ts(text: str, where: str) -> datetime:
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise CollectorError(f"{where} 的时间戳读不出（{text!r}），拒绝写入半解析数据")


def _find_kp(cells: list[str], where: str) -> str:
    for cell in cells[2:]:
        if KP_RE.match(cell):
            return cell
    raise CollectorError(f"{where} 里找不到形如 `高数-1.1` 的知识点（字段可能已变更），拒绝写入半解析数据")


def parse_records(text: str, where: str) -> list[dict]:
    """只认 `## 记录` 区里第一格是 8 位十六进制 ID 的行。"""
    rows: list[dict] = []
    in_record = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            in_record = line.strip() == "## 记录"
            continue
        if not in_record or not ROW_RE.match(line):
            continue
        cells = _cells(line)
        if len(cells) < 5:
            raise CollectorError(f"{where}:{lineno} 列数不足（{len(cells)} < 5），字段可能已变更")
        ts = _parse_ts(cells[1], f"{where}:{lineno}")
        kp = _find_kp(cells, f"{where}:{lineno}")
        rows.append({"id": cells[0], "ts": ts, "kp": kp,
                     "status": cells[4] if len(cells) > 4 else "", "line": lineno})
    return rows


def collect(config, store) -> list[dict]:
    raw_path = config.collector_get(NODE, "log_path")
    if not raw_path:
        raise CollectorError("缺配置 collect.study.log_path")
    path = Path(str(raw_path)).expanduser()
    key = f"{NODE}:{path}"

    if not path.is_file():
        detail = f"学习日志不存在: {path}"
        store.state_put(key, ok=False, error=detail)
        raise CollectorError(detail)
    timeout_s = config.collector_get(NODE, "timeout_s")
    try:
        text = _read_text(path, timeout_s)
    except (OSError, UnicodeDecodeError) as exc:
        detail = f"学习日志读不出: {path} ({type(exc).__name__}: {exc})"
        store.state_put(key, ok=False, error=detail)
        raise CollectorError(detail) from exc

    records = parse_records(text, str(path))
    today = date.today()
    latest = max(records, key=lambda r: r["ts"]) if records else None
    payload = {
        "log": str(path),
        "date": today.isoformat(),
        "records": len(records),
        "latest_kp": latest["kp"] if latest else None,
        "latest_ts": latest["ts"].strftime("%Y-%m-%d %H:%M") if latest else None,
        "days_since": (today - latest["ts"].date()).days if latest else None,
    }
    state = store.state_get(key) or {}
    cursor = state.get("cursor") or {}
    digest = E.fingerprint(NODE, KIND, payload)
    if cursor.get("last_day") == payload["date"] and cursor.get("fingerprint") == digest:
        store.state_put(key, ok=True)         # 今天已经投过同一份快照：幂等返回
        return []
    store.state_put(key, cursor={"last_day": payload["date"], "fingerprint": digest}, ok=True)
    return [E.make_event(NODE, KIND, payload, ts=E.iso(), privacy="normal")]
