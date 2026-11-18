"""看板写入（M1 出口 A）。协议见 skill `dsh-task-board`：POST /api/task-board/action，`Origin` 头必带。

只实现"建卡"这一条路径（M1 够用）；正文按 AGENTS.md §二 放 `prompt`，不放 `description`。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class BoardError(Exception):
    pass


class Board:
    def __init__(self, base_url: str, endpoint: str = "/api/task-board/action",
                 origin: str | None = None, timeout: float = 10.0,
                 column: str = "backlog", permission: str = "read-only"):
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.origin = (origin or self.base_url).rstrip("/")
        self.timeout = timeout
        self.column = column
        self.permission = permission

    @classmethod
    def from_config(cls, config) -> "Board":
        base = (config.get("board.base_url") or os.environ.get("DSH_WEB_URL")
                or "http://127.0.0.1:3080")
        return cls(base_url=base,
                   endpoint=config.get("board.endpoint") or "/api/task-board/action",
                   origin=config.get("board.origin"),
                   timeout=float(config.get("board.timeout_s") or 10.0),
                   column=config.get("board.column") or "backlog",
                   permission=config.get("board.permission") or "read-only")

    def post(self, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + self.endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json", "Origin": self.origin})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise BoardError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc
        except OSError as exc:
            raise BoardError(f"{type(exc).__name__}: {exc}") from exc
        try:
            return json.loads(body) if body.strip() else {}
        except ValueError as exc:
            raise BoardError(f"响应不是 JSON: {body[:200]}") from exc

    def create_card(self, title: str, body: str, column: str | None = None) -> str:
        """建卡并返回卡片 id。找不到 id 即视为失败（宁可不记，也不留半条指针）。"""
        response = self.post({"action": "create", "title": title, "prompt": body,
                              "column": column or self.column, "permission": self.permission})
        card_id = _find_id(response)
        if not card_id:
            raise BoardError(f"响应里没有卡片 id: {json.dumps(response, ensure_ascii=False)[:200]}")
        return card_id


def _find_id(response: dict) -> str | None:
    for key in ("id", "cardId", "card_id"):
        if isinstance(response.get(key), (str, int)):
            return str(response[key])
    for key in ("card", "data", "result"):
        node = response.get(key)
        if isinstance(node, dict):
            found = _find_id(node)
            if found:
                return found
    return None


def sync(store, config, dry_run: bool = False, limit: int = 50) -> dict:
    """把还没有卡片指针的 open 待办推到看板。幂等靠 todos.board_card_id。"""
    if not config.get("board.enabled", False) and not dry_run:
        return {"enabled": False, "pushed": [], "errors": [], "dry_run": dry_run}

    board = Board.from_config(config)
    pushed, errors = [], []
    for todo in store.todos(needing_board=True, limit=limit):
        if dry_run:
            pushed.append({"id": todo["id"], "title": todo["title"], "dry_run": True})
            continue
        try:
            card_id = board.create_card(todo["title"], _body(todo))
        except BoardError as exc:
            errors.append({"todo": todo["id"], "error": str(exc)})
            continue
        store.set_todo_board_card(todo["id"], card_id)
        pushed.append({"id": todo["id"], "card": card_id})
    return {"enabled": True, "pushed": pushed, "errors": errors, "dry_run": dry_run}


def _body(todo: dict) -> str:
    lines = [todo.get("title") or "", "", f"依据：{todo.get('why') or '-'}",
             f"置信度：{todo.get('confidence')}", f"证据事件：{todo.get('source_event_ids')}",
             f"规则键：{todo.get('dedupe_key')}"]
    return "\n".join(lines)
