#!/usr/bin/env python3
"""M4b 假加工层：读 stdin 上的提示词，按 `--mode` 产出不同质量的 JSON，用来机械验审计逻辑。

模式：
  ok        从提示词的事件流水里挑出真实 id，产出 2 条带证据的条目
  bogus     一条真证据 + 一条**不存在的事件 id**（必须被丢弃）
  nojson    输出一句自然语言（必须致命失败、不写库）
  empty     合法 JSON 但没有条目
  fail      退出码 3（模拟加工层崩了）
"""
from __future__ import annotations

import json
import re
import sys

MODE = "ok"
for i, arg in enumerate(sys.argv):
    if arg == "--mode" and i + 1 < len(sys.argv):
        MODE = sys.argv[i + 1]

prompt = sys.stdin.read()
ids = [int(x) for x in re.findall(r"^- (\d+) \|", prompt, flags=re.M)]

if MODE == "fail":
    print("假加工层故意失败", file=sys.stderr)
    raise SystemExit(3)
if MODE == "nojson":
    print("这是自然语言，不是 JSON。")
    raise SystemExit(0)
if MODE == "empty":
    print(json.dumps({"items": []}, ensure_ascii=False))
    raise SystemExit(0)

first = ids[0] if ids else 1
second = ids[1] if len(ids) > 1 else first
items = [{"text": "测试归纳：窗口内有事件流水", "evidence": [first]}]
if MODE == "bogus":
    items.append({"text": "这条挂着不存在的事件 id", "evidence": [99999999]})
else:
    items.append({"text": "测试归纳：第二条依据另一条事件", "evidence": [second, first]})
print(json.dumps({"items": items}, ensure_ascii=False))
