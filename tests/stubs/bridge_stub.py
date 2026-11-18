#!/usr/bin/env python3
"""最小 MCP streamable-http 替身（W3，纯标准库）。

给 info-hub 的 phone_remind.bridge_call / 未来的 phone-mcp 通道插件一个可控的「手机 MCP 服务器」：
POST /mcp 上处理 MCP JSON-RPC 的 initialize / notifications/initialized / tools/list / tools/call，
把**每一次 tools/call 的 params** 追加进 JSONL，并可按回放文件返回预设回执。

协议形状以 info-hub/src/infohub/phone_remind.py 的 client 为准：
- 先 POST initialize，从响应头取 Mcp-Session-Id（替身不校验它）；
- 再 POST notifications/initialized（替身回 202 + 空体，client 会吞掉这步的失败）；
- 再 POST tools/call；响应既可以是裸 JSON，也可以是一条 SSE（data: {...}）——由 BRIDGE_STUB_SSE 控制；
- 回执文本取 result.content[0].text，client 再 json.loads 它。

环境变量与已知限制见同目录 README.md。启动时会打印：
  BRIDGE_STUB_LISTENING http://127.0.0.1:<port>/mcp
  BRIDGE_STUB_LOG <jsonl 路径>
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = "2025-03-26"
DEFAULT_TOOLS = ("hub_deliver", "hub_dismiss", "device_action")
TOOL_HINTS = {
    "hub_deliver": "推送一条手机投递卡片（single/status/question）",
    "hub_dismiss": "收回一张已推送的卡片",
    "device_action": "旧动作接口（reminder_show / hub_sync / reminder_ack_drain ...）",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_replay(path: str | None):
    """回放文件：{tool: 回执} / {tool: [回执, ...]} / [回执, ...] / 带 "__default__" 的兜底。"""
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def default_receipt(name: str, arguments: dict) -> dict:
    """没给回放时按工具名编一个「成功」回执，保证 info-hub 侧能走通成功分支。"""
    if name == "hub_deliver":
        return {"ok": True, "delivery_id": arguments.get("delivery_id"),
                "mode": arguments.get("mode"), "shown": True}
    if name == "hub_dismiss":
        return {"ok": True, "dismissed": arguments.get("delivery_id")}
    return {"ok": True, "data": {"success": True, "shown": True}}


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seq = 0
        self.fail = (os.environ.get("BRIDGE_STUB_FAIL") or "").strip().lower()
        self.sse = (os.environ.get("BRIDGE_STUB_SSE") or "").strip() not in ("", "0", "false", "no")
        self.verbose = (os.environ.get("BRIDGE_STUB_VERBOSE") or "").strip() not in ("", "0", "false", "no")
        self.session_id = os.environ.get("BRIDGE_STUB_SESSION_ID", "stub-session-1").strip() or None
        self.log_path = (os.environ.get("BRIDGE_STUB_LOG") or "").strip() or None
        self.replay = _load_replay((os.environ.get("BRIDGE_STUB_REPLAY") or "").strip() or None)
        raw_tools = (os.environ.get("BRIDGE_STUB_TOOLS") or "").strip()
        self.tools = [t.strip() for t in raw_tools.split(",") if t.strip()] or list(DEFAULT_TOOLS)
        if self.log_path:
            parent = os.path.dirname(os.path.abspath(self.log_path))
            os.makedirs(parent, exist_ok=True)

    def tools_list(self) -> list[dict]:
        return [{
            "name": name,
            "description": TOOL_HINTS.get(name, "stub tool"),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
        } for name in self.tools]

    def record(self, params: dict) -> None:
        """一行一条 tools/call；initialize / tools/list 不落盘。"""
        if not self.log_path:
            return
        with self.lock:
            self.seq += 1
            rec = {
                "ts": _now(),
                "seq": self.seq,
                "method": "tools/call",
                "session": self.session_id,
                "name": params.get("name"),
                "params": params,
                "arguments": params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
            }
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def receipt(self, params: dict) -> dict:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        with self.lock:
            source = self.replay
            if isinstance(source, list):
                if source:
                    return source.pop(0)
            elif isinstance(source, dict):
                value = source.get(name)
                if isinstance(value, list):
                    if value:
                        return value.pop(0)
                elif isinstance(value, dict):
                    return value
                fallback = source.get("__default__")
                if isinstance(fallback, dict):
                    return fallback
        return default_receipt(name, arguments)


class Handler(BaseHTTPRequestHandler):
    server_version = "bridge-stub/0.1"
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 -------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - 标准库签名
        if getattr(self.server, "state", None) and self.server.state.verbose:
            super().log_message(fmt, *args)

    def _send(self, code: int, payload, ctype: str, extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        session_id = getattr(self.server, "state", None) and self.server.state.session_id
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _emit(self, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        if self.server.state.sse:
            self._send(200, "event: message\ndata: " + text + "\n\n",
                       "text/event-stream; charset=utf-8")
        else:
            self._send(200, text, "application/json; charset=utf-8")

    def _jsonrpc_error(self, message_id, code: int, message: str) -> None:
        self._emit({"jsonrpc": "2.0", "id": message_id,
                    "error": {"code": code, "message": message}})

    # ---- 路由 -----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - 标准库签名
        if self.path.split("?", 1)[0] == "/healthz":
            self._send(200, "ok\n", "text/plain; charset=utf-8")
            return
        self._send(405, "MCP streamable-http 用 POST /mcp（替身不支持 GET 流）\n",
                   "text/plain; charset=utf-8", {"Allow": "POST"})

    def do_POST(self):  # noqa: N802 - 标准库签名
        state: State = self.server.state
        if state.fail == "http500":
            self._send(500, '{"error":"stub forced http500"}', "application/json; charset=utf-8")
            return
        if state.fail == "http400":
            self._send(400, '{"error":"stub forced http400"}', "application/json; charset=utf-8")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._jsonrpc_error(None, -32700, f"parse error: {exc}")
            return
        if not isinstance(message, dict):
            self._jsonrpc_error(None, -32600, "invalid request")
            return

        method = str(message.get("method") or "")
        message_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if state.fail == "jsonrpc":
            self._jsonrpc_error(message_id, -32603, "stub forced jsonrpc error")
            return

        # 通知（无 id）：MCP 客户端发 notifications/initialized 时期望 202 + 空体
        if message_id is None and method.startswith("notifications/"):
            self._send(202, b"", "text/plain; charset=utf-8")
            return

        if method == "initialize":
            self._emit({"jsonrpc": "2.0", "id": message_id, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "bridge-stub", "version": "0.1.0"},
            }})
            return
        if method == "ping":
            self._emit({"jsonrpc": "2.0", "id": message_id, "result": {}})
            return
        if method == "tools/list":
            self._emit({"jsonrpc": "2.0", "id": message_id,
                        "result": {"tools": state.tools_list()}})
            return
        if method == "tools/call":
            state.record(params)
            receipt = state.receipt(params)
            is_error = bool(isinstance(receipt, dict) and receipt.get("isError"))
            self._emit({"jsonrpc": "2.0", "id": message_id, "result": {
                "content": [{"type": "text", "text": json.dumps(receipt, ensure_ascii=False)}],
                "isError": is_error,
            }})
            return

        self._jsonrpc_error(message_id, -32601, f"method not found: {method}")


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def main() -> int:
    host = (os.environ.get("BRIDGE_STUB_HOST") or "127.0.0.1").strip()
    if not _is_loopback(host) and (os.environ.get("BRIDGE_STUB_ALLOW_REMOTE") or "").strip() in ("", "0"):
        print("bridge_stub: 默认只绑 loopback；要绑别的地址请设 BRIDGE_STUB_ALLOW_REMOTE=1",
              file=sys.stderr)
        return 2
    try:
        port = int(os.environ.get("BRIDGE_STUB_PORT") or 0)
    except ValueError:
        print("bridge_stub: BRIDGE_STUB_PORT 不是整数", file=sys.stderr)
        return 2

    state = State()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.state = state
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{bound_host}:{bound_port}/mcp"

    print(f"BRIDGE_STUB_LISTENING {url}", flush=True)
    if state.log_path:
        print(f"BRIDGE_STUB_LOG {state.log_path}", flush=True)
    port_file = (os.environ.get("BRIDGE_STUB_PORT_FILE") or "").strip()
    if port_file:
        with open(port_file, "w", encoding="utf-8") as fh:
            fh.write(f"{bound_port}\n")

    def _bye(*_args):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    try:
        server.serve_forever(poll_interval=0.2)
    except SystemExit:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
