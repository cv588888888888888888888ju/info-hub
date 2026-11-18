"""验证码脱敏（M15）：落库前把验证码类通知的正文换成占位符。

契约见 INTERFACE.md §4.21（规则与误杀口径）/ §5.14（`redact` 字段与历史清洗）。
`find` / `apply` 是纯函数（无 IO、不联网）；可调参数只从 `hub.config.yaml` 的 `redact.*` 读，
这里的词表与 App 白名单是**数据**（同 `vision_keywords.py` 的定位）。
"""

from __future__ import annotations

import re
from typing import Any

# ---- 词表与 App 白名单（数据） --------------------------------------------
CN_KEYWORDS = ("验证码", "校验码", "动态码", "动态密码", "短信登录", "一次性密码")
EN_CODE = re.compile(r"verification\s+code|\botp\b|one[-\s]?time\s+(?:password|passcode)", re.I)
# 凭据类：字样**后面紧跟**一段含数字的字母数字串（"耗10亿token建造"不算：数字在字样前面、后面没有值）
CRED_WORD = re.compile(
    r"(?<![A-Za-z])(?:password|passwd|pwd|token|密码|口令)\s*(?:[:：=]|为|是|is)?\s*([A-Za-z0-9]{4,32})",
    re.I)

# 弱证据的唯一语境判据：短信 App。pkg 缺失时从 payload.key 的第二段取（形状见 INTERFACE.md §5.2）。
SMS_PKGS = frozenset({
    "com.android.mms", "com.android.messaging", "com.google.android.apps.messaging",
    "com.samsung.android.messaging", "com.miui.smsextra",
})
SMS_APPS = frozenset({"短信", "信息", "讯息", "messages"})

# 数字串后面跟这些单位 ⇒ 是数量/日期/金额，不当验证码
_UNIT_AFTER = re.compile(
    r"^\s*(?:年|月|日|号|时|分|秒|元|块|个|次|人|条|万|亿|千|百|十|%|°|楼|岁|度|位|"
    r"GB|MB|KB|TB|ms|分钟|小时|天|周|公里|km|kg)", re.I)

DEFAULTS = {
    "enabled": False,
    "mode": "mask",
    "placeholder": "[验证码类]",
    "version": "rd-1",
    "window_chars": 24,
    "min_digits": 4,
    "max_digits": 10,
}


def settings(config=None) -> dict:
    """读出 `redact.*`；不传 config 就用默认配置，缺键取 DEFAULTS（语义见 §3）。"""
    if config is None:
        from .config import load

        config = load()
    cfg = {}
    for key, fallback in DEFAULTS.items():
        value = config.get(f"redact.{key}")
        cfg[key] = fallback if value is None else value
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["mode"] = str(cfg["mode"])
    cfg["placeholder"] = str(cfg["placeholder"])
    cfg["version"] = str(cfg["version"])
    for key in ("window_chars", "min_digits", "max_digits"):
        cfg[key] = int(cfg[key])
    return cfg


def enabled(config=None) -> bool:
    return bool(settings(config)["enabled"])


def version(config=None) -> str:
    return str(settings(config)["version"])


# ---- 内部工具 -------------------------------------------------------------
def _occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        pos = text.find(needle, start)
        if pos < 0:
            return spans
        spans.append((pos, pos + len(needle)))
        start = pos + len(needle)


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    if a[1] <= b[0]:
        return b[0] - a[1]
    if b[1] <= a[0]:
        return a[0] - b[1]
    return 0


def _code_spans(text: str, min_digits: int, max_digits: int) -> list[tuple[int, int]]:
    """像验证码的数字串：位宽在区间内、前后不粘字母/数字、后面不跟数量单位。"""
    pattern = re.compile(rf"(?<![\dA-Za-z])(\d{{{min_digits},{max_digits}}})(?![\d])")
    return [(m.start(), m.end()) for m in pattern.finditer(text)
            if not _UNIT_AFTER.match(text[m.end():])]


def _pkg_of(payload: dict) -> str:
    pkg = str(payload.get("pkg") or "").strip().lower()
    if pkg:
        return pkg
    parts = str(payload.get("key") or "").split("|")
    return parts[1].strip().lower() if len(parts) > 1 else ""


def _in_url(text: str, pos: int) -> bool:
    """`?token=…` 这种 URL 里的凭据不算「密码通知」：整段正文换成占位符会把报错信息也吃掉。

    判据只看这一段的起点：从最近的空白/引号切到字样位置，中间出现过 `://`，或者紧邻 `?`/`&`。
    """
    start = max(text.rfind(ch, 0, pos) for ch in (" ", "\n", "\t", "'", '"', "：", "，"))
    return "://" in text[start + 1:pos] or (pos > 0 and text[pos - 1] in "?&")


def is_sms(payload: dict) -> bool:
    """这条通知是不是短信 App 发的（弱证据的唯一语境判据，见 §4.21）。"""
    if _pkg_of(payload) in SMS_PKGS:
        return True
    return str(payload.get("app") or "").strip().lower() in SMS_APPS


def _already(payload: dict, placeholder: str) -> bool:
    """已经脱敏过：正文就是占位符，或已有 `redact.hit`（幂等判据，见 §4.21）。"""
    summary = payload.get("redact")
    if isinstance(summary, dict) and summary.get("hit"):
        return True
    text = payload.get("text")
    return isinstance(text, str) and text.strip() == placeholder


# ---- 契约入口 -------------------------------------------------------------
def find(text: str, config=None) -> tuple[str | None, tuple[int, int] | None]:
    """认一条正文里的验证码类线索：返回 (`kind`, 命中位置)，没有就是 `(None, None)`。

    kind 与判据见 INTERFACE.md §4.21 的表格；这里只做匹配，不决定要不要改写（那是 apply 的事）。
    """
    if not isinstance(text, str) or not text.strip():
        return None, None
    cfg = settings(config)
    window = cfg["window_chars"]
    codes = _code_spans(text, cfg["min_digits"], cfg["max_digits"])

    best: tuple[int, str, tuple[int, int]] | None = None      # (间隔, kind, span)

    def consider(kind: str, keyword_span: tuple[int, int]) -> None:
        nonlocal best
        for span in codes:
            gap = _gap(keyword_span, span)
            if gap <= window and (best is None or gap < best[0]):
                best = (gap, kind, span)

    for keyword in CN_KEYWORDS:
        for span in _occurrences(text, keyword):
            consider("cn-code", span)
    for m in EN_CODE.finditer(text):
        consider("en-code", (m.start(), m.end()))
    if best is not None:
        return best[1], best[2]

    m = CRED_WORD.search(text)
    while m is not None:
        if any(ch.isdigit() for ch in m.group(1)) and not _in_url(text, m.start()):
            return "cred", (m.start(1), m.end(1))
        m = CRED_WORD.search(text, m.end())

    for keyword in CN_KEYWORDS:
        spans = _occurrences(text, keyword)
        if spans:
            return "cn-keyword", spans[0]
    m = EN_CODE.search(text)
    if m:
        return "en-keyword", (m.start(), m.end())
    return None, None


def apply(payload: dict, config=None) -> tuple[dict, dict]:
    """按契约清洗一条 payload：返回 (清洗后的 payload, 摘要)。

    命中 ⇒ 浅拷贝里 `text` 换成占位符、其余字段保留、新增 `redact`；未命中 ⇒ 原样拷贝。
    `redact.enabled=false` ⇒ 完全跳过（§4.21）；`mode` 只认 `mask`。
    """
    cfg = settings(config)
    summary: dict = {"hit": False, "kind": "", "version": cfg["version"]}
    if cfg["mode"] != "mask":
        raise ValueError(f"不支持的 redact.mode: {cfg['mode']!r}（只认 mask）")

    out = dict(payload) if isinstance(payload, dict) else {}
    if not cfg["enabled"] or _already(out, cfg["placeholder"]):
        return out, summary

    text = out.get("text")
    if not isinstance(text, str) or not text.strip():
        return out, summary

    kind, _span = find(text, config)
    if not kind:
        return out, summary
    if kind.endswith("-keyword") and not is_sms(out):
        return out, summary

    out["text"] = cfg["placeholder"]
    summary = {"hit": True, "kind": kind, "version": cfg["version"]}
    out["redact"] = dict(summary)
    return out, summary
