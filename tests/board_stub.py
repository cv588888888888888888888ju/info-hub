#!/usr/bin/env python3
"""看板写入的假服务器（M1 验收用）：把每次 POST 追加到日志文件，返回 {"ok":true,"id":"card-N"}。"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
LOG = sys.argv[2]
STATE = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw or "{}")
        except ValueError:
            body = {"_raw": raw}
        STATE["n"] += 1
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": self.path, "origin": self.headers.get("Origin"),
                                 "body": body}, ensure_ascii=False) + "\n")
        payload = json.dumps({"ok": True, "id": f"card-{STATE['n']}"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 别把访问日志混进 stdout
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
