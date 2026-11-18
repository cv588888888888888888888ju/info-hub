"""脱敏：任何**离开本机**的文本（目前只有 M4b 的 LLM 归纳提示词）先过这里。

口径（INTERFACE.md §4.9）：只做机械替换，不改结构、不做语义判断；命中几处就报几处。
- 键值形态的凭据：`token=` / `password:` / `api_key =` …
- DSH 自铸 cookie：`dsh-auth-…`
- 长随机串：≥32 位 hex、≥32 位 base64url（含 `-`/`_`）
- 常见私钥头与 URL 里的 `?key=` 一类查询参数
"""
from __future__ import annotations

import re

MASK = "«已脱敏»"

_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # 键值形态：把值（非空白、非引号结尾）整段吃掉
    (re.compile(r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|"
                r"authorization|bearer|refresh[_-]?token)\b\s*[:=]\s*[^\s,;&\"'}\]]+"),
     rf"\1={MASK}"),
    # URL 查询参数里的 key/token/signature
    (re.compile(r"(?i)([?&](?:key|token|sig(?:nature)?|access_token|auth)=)[^&\s]+"),
     rf"\1{MASK}"),
    # DSH 自铸 cookie 名/值
    (re.compile(r"dsh-auth-[A-Za-z0-9._~+/=-]+"), MASK),
    # 私钥块整体
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     MASK),
    # 长 hex（≥32）
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), MASK),
    # 长 base64url（≥32，且要含数字与字母，避免误伤普通长单词）
    (re.compile(r"\b(?=[A-Za-z0-9_-]{32,}\b)(?=[^\s]*[0-9])(?=[^\s]*[A-Za-z])[A-Za-z0-9_-]{32,}={0,2}"),
     MASK),
)


def redact(text: str | None) -> tuple[str, int]:
    """返回 `(脱敏后文本, 命中处数)`。`None` / 空串原样返回 0 处。"""
    if not text:
        return ("", 0) if text is None else (text, 0)
    out = str(text)
    hits = 0
    for pattern, repl in _PATTERNS:
        out, n = pattern.subn(repl, out)
        hits += n
    return out, hits


def redact_obj(obj, _depth: int = 0) -> tuple[object, int]:
    """递归脱敏 JSON 可序列化结构里的**所有字符串**（键名不动）。返回 `(新对象, 命中处数)`。"""
    if _depth > 12:
        return obj, 0
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out, hits = {}, 0
        for key, value in obj.items():
            # 键名里可能直接写着凭据（如 "Bearer eyJ…"），但键名有语义，只脱敏可疑键名
            new_key, n = redact(key) if isinstance(key, str) else (key, 0)
            hits += n
            new_value, n = redact_obj(value, _depth + 1)
            hits += n
            out[new_key] = new_value
        return out, hits
    if isinstance(obj, (list, tuple)):
        items, hits = [], 0
        for item in obj:
            new_item, n = redact_obj(item, _depth + 1)
            hits += n
            items.append(new_item)
        return items, hits
    return obj, 0
