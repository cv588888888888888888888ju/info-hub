#!/usr/bin/env python3
"""MCP tools/call 替身（S9 验收用）：initialize → session → tools/call；把每次工具调用的
{name, args} 追加到日志文件。不绑定真手机、不执行任何手机动作。

用法: python3 tests/stubs/mcp_stub.py <port> <logfile>
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
LOG = sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload=None, sid=None):
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        if data:
            self.send_header("Content-Type", "application/json")
        if sid:
            self.send_header("mcp-session-id", sid)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, {"error": "bad json"})
            return
        method = req.get("method")
        if method == "initialize":
            self._send(200, {"jsonrpc": "2.0", "id": req.get("id"),
                             "result": {"protocolVersion": "2025-03-26", "capabilities": {},
                                        "serverInfo": {"name": "mcp-stub", "version": "1"}}},
                       sid="stub-session")
            return
        if method == "notifications/initialized":
            self._send(202)
            return
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tool": name, "args": args}, ensure_ascii=False) + "\n")
            calls = sum(1 for _ in open(LOG, encoding="utf-8"))
            result = {"ok": True, "ref": f"ref-{calls}"}
            self._send(200, {"jsonrpc": "2.0", "id": req.get("id"),
                             "result": {"content": [{"type": "text",
                                                     "text": json.dumps(result)}]}})
            return
        self._send(404, {"error": f"unknown method {method}"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
