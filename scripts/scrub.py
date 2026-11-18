#!/usr/bin/env python3
"""把 info-hub 树脱敏成可公开发布的样子：路径 / 真实 IP / 设备号 / token → 占位符。

用法：python3 scripts/scrub.py <导出目录>

只改文本文件（.md/.py/.sh/.yaml/.java/.mjs/.json/.service/.timer/.txt）；不碰二进制。
不删除任何文件——要排除的文件由调用方在导出时就不拷（如 *.apk / *.idsig / *.keystore / hub.db）。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".java", ".mjs", ".json",
            ".service", ".timer", ".txt", ".example", ".cfg", ".ini"}

# 顺序敏感：长的先替换（否则父目录先吃掉子路径）。
REPLACEMENTS = [
    # 示例形态（本机真实映射表不进公开仓库）：长的先替换，否则父路径会先吃掉子路径。
    ("/home/<user>/<workspace>/<project>", "$PROJECT_ROOT"),
    ("/home/<user>", "~"),
    ("<lan-ip>", "<lan-ip>"),
    ("<public-ip>", "<public-ip>"),
    ("<device-serial>", "<serial>"),
]

KEEP_IPS = {"127.0.0.1", "0.0.0.0", "192.0.2.1", "8.2.1.1", "255.255.255.255"}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SECRET_PATTERNS = [
    re.compile(r"\bhub-[0-9a-f]{32}\b"),          # 本项目的访问 token
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub token
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),        # 厂商 API key
]


def _local_pairs() -> list[tuple[str, str]]:
    """本机私有的补充规则（不进仓库、不进发布树）：HUB_SCRUB_LOCAL 指向的制表符分隔文件。

    一行一条 `旧串<TAB>新串`，# 开头是注释。给「只在本机出现、不能写进公开脚本」的秘密用。
    """
    path = os.environ.get("HUB_SCRUB_LOCAL", "")
    if not path or not Path(path).is_file():
        return []
    pairs: list[tuple[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        old, _, new = line.partition("\t")
        if old and new:
            pairs.append((old, new))
    return pairs


LOCAL_PAIRS = _local_pairs()


def scrub_text(text: str) -> str:
    for old, new in LOCAL_PAIRS:
        text = text.replace(old, new)
    for old, new in REPLACEMENTS:
        text = text.replace(old, new)
    text = IPV4.sub(lambda m: m.group(0) if m.group(0) in KEEP_IPS else "<ip>", text)
    for pat in SECRET_PATTERNS:
        text = pat.sub("<secret>", text)
    text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                  "<private-key>", text, flags=re.S)
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python3 scripts/scrub.py <导出目录>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    changed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "scrub.py":
            continue  # 工具自己不参与替换（否则表会被自己改写）
        if path.suffix not in SUFFIXES and not (path.suffix == "" and path.parent.name in {"bin", "scripts"}):
            continue  # bin/ 下的无后缀入口脚本也要扫
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new = scrub_text(text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed += 1
    print(f"scrubbed {changed} files under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
