#!/usr/bin/env python3
"""bilibili 取数桩（M5 验收用）：按 --mode 吐不同的东西，模拟「接进来的来源」。

契约同 INTERFACE.md §4.11：stdout 必须是 {"items":[{id,title,url,desc}]}。
"""
from __future__ import annotations

import argparse
import json
import sys

ITEMS = [
    {"id": "b1", "title": "整合过的：ownerA/present-tool 讲得很好", "url": "https://www.bilibili.com/video/BV1", "desc": ""},
    {"id": "b2", "title": "在讲 ownerB/absent-tool 这个神器", "url": "https://www.bilibili.com/video/BV2", "desc": ""},
    {"id": "b3", "title": "只用名字提到的 nameonly-tool", "url": "https://www.bilibili.com/video/BV3", "desc": ""},
    {"id": "b4", "title": "又刷到 ownerB/absent-tool（同一条工具的第二个视频）", "url": "https://www.bilibili.com/video/BV4", "desc": ""},
    {"id": "b5", "title": "完全没有工具引用的普通视频", "url": "https://www.bilibili.com/video/BV5", "desc": ""},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok")
    args = ap.parse_args()
    if args.mode == "fail":
        print("boom", file=sys.stderr)
        return 3
    if args.mode == "badjson":
        print("not json at all")
        return 0
    if args.mode == "empty":
        print(json.dumps({"items": []}))
        return 0
    print(json.dumps({"items": ITEMS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
