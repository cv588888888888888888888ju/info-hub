"""画面语义层（M11）：把已经存下来的截屏读成「这是哪一类画面」。

**这是派生层，不是采集层**：图由 M7 的 screencast 截屏采集器落盘，本模块只在
`events(source='screenshot')` 上做重算，永远可以从原图重新跑一遍得出同样结果。

三条边界（用户 2026-10-24 定 "走一个简单 ocr，接入信息中枢统一管理"）：
① **只读已经存下来的图**，不新增任何取帧动作，不影响 screenshot 的节流与隐私边界；
② **默认不留原文**（`vision.keep_raw=false`）：只落「类别 + 命中词计数 + 置信度 + 文本指纹」，
   原文只有显式打开 keep_raw 才写，且按 `vision.retention_text_days` 过期；
③ **判不出来就判 unknown**：命中不足 `vision.min_hits` 个词就认输，不硬猜 —— 类别错比没有更坏。

类别词表在 `vision_keywords.py`（数据，可改不用改算法）。改算法 ⇒ 换 `VERSION`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from infohub import vision_keywords as KW

VERSION = "ocr-2"          # 算法版本：改分类/取词逻辑就要换，历史标签才能区分口径
DEFAULT_MIN_HITS = 2

_WS = re.compile(r"[ \t\u3000]+")


class VisionUnavailable(Exception):
    """OCR 引擎不可用（没装依赖 / 模型缺失）。属于可降级失败，不阻断 derive。"""


def normalize(text: str) -> str:
    """把 OCR 行拼成一行可匹配的文本：压空白、去空行。"""
    text = _WS.sub(" ", text.replace("\r", " ").replace("\n", " "))
    return text.strip()


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def classify(text: str, min_hits: int = DEFAULT_MIN_HITS,
             keywords: dict[str, list[str]] | None = None,
             strong: dict[str, list[str]] | None = None) -> dict:
    """加权关键词分类。返回 {category, score, hits}，hits 是各类别**加权**得分。

    弱词 1 分、强词 2 分：浏览器标签栏里的应用名（淘宝/bilibili）会出现在每张图上，
    只值 1 分；真正在购物车/弹幕页才会出现的词值 2 分。
    """
    table = keywords if keywords is not None else KW.CATEGORIES
    strong = strong if strong is not None else KW.STRONG
    low = text.lower()
    hits: dict[str, int] = {}
    for cat, tokens in table.items():
        hard = set(strong.get(cat, ()))
        n = sum(2 if tok in hard else 1 for tok in tokens if tok.lower() in low)
        if n:
            hits[cat] = n
    if not hits:
        return {"category": "unknown", "score": 0.0, "hits": {}}
    best = max(hits.values())
    cats = [c for c, n in hits.items() if n == best]
    # 并列时按词表声明顺序取第一个，保证同输入同输出
    cat = sorted(cats, key=lambda c: list(table).index(c))[0]
    if best < min_hits:
        return {"category": "unknown", "score": round(best / (best + 2), 3), "hits": hits}
    return {"category": cat, "score": round(best / (best + 2), 3), "hits": hits}


def _limit_session_threads(n: int) -> None:
    """把 onnxruntime 的线程池摁到 n 个。

    实测（本机 32 核）：环境变量压不住；`sched_setaffinity(0, {...})` 也压不住 ——
    OpenMP/onnxruntime 会给自己的线程**重设**亲和性（采样到 4-5/6-7 这种掩码），
    一张 1280×800 图能烧掉 50 秒 CPU。唯一可靠的办法是在造会话前把
    `SessionOptions.intra_op_num_threads` 改掉，所以这里包一层 InferenceSession。
    """
    if n <= 0:
        return
    try:
        import onnxruntime as ort
    except Exception:                       # onnxruntime 还没装：不用管，后面导入会报
        return
    if getattr(ort.InferenceSession, "_infohub_limited", False):
        return
    orig = ort.InferenceSession

    def wrapped(*args, **kwargs):
        so = kwargs.get("sess_options") or ort.SessionOptions()
        so.intra_op_num_threads = n
        so.inter_op_num_threads = 1
        kwargs["sess_options"] = so
        return orig(*args, **kwargs)

    wrapped._infohub_limited = True         # type: ignore[attr-defined]
    ort.InferenceSession = wrapped


def engine(config):
    """返回 (path -> list[str]) 的 OCR 函数。引擎可用性在调用时才检查。"""
    name = str(config.get("vision.engine", "rapidocr") or "rapidocr")
    if name != "rapidocr":
        raise VisionUnavailable(f"未知引擎 {name}")

    # 后台监工工具不许吃满工作站：onnxruntime 默认按核数开线程，32 核上一张图能烧
    # 几十秒 CPU。这里在导入前把它摁到 vision.threads（默认 4）。
    n = int(config.get("vision.threads", 4) or 4)
    os.environ.setdefault("OMP_NUM_THREADS", str(n))
    _limit_session_threads(n)

    try:                                    # 延迟导入：没装依赖不影响 hub 其余部分
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception as exc:                # pragma: no cover - 依赖缺失
        raise VisionUnavailable(
            "缺 rapidocr（装法：python3 -m pip install --user rapidocr-onnxruntime）"
        ) from exc

    shared: dict[str, object] = {}

    def read(path: str) -> list[str]:
        if "e" not in shared:               # 模型加载一次，本轮复用
            shared["e"] = RapidOCR()
        res, _ = shared["e"](path)          # type: ignore[operator]
        return [r[1] for r in (res or [])]

    return read


def run(config, store, *, limit: int | None = None, engine_fn=None,
        dry_run: bool = False) -> dict:
    """给「还没读过」的截屏补上类别。返回摘要 dict（绝不抛业务异常）。"""
    if not bool(config.get("vision.enabled", False)):
        return {"skipped": "vision.enabled=false"}
    if limit is None:
        limit = int(config.get("vision.max_per_run", 8) or 8)
    min_hits = int(config.get("vision.min_hits", DEFAULT_MIN_HITS) or DEFAULT_MIN_HITS)
    keep_raw = bool(config.get("vision.keep_raw", False))

    pending = store.unlabeled_shots(limit)
    out = {"pending": len(pending), "labeled": 0, "missing": 0,
           "categories": {}, "version": VERSION}
    if not pending:
        return out

    if engine_fn is None:
        try:
            engine_fn = engine(config)
        except VisionUnavailable as exc:
            out["error"] = str(exc)         # fail-soft：derive 照常跑完
            return out

    t0 = time.time()
    for item in pending:
        path = item.get("path") or ""
        if not path or not Path(path).exists():
            out["missing"] += 1
            store.shot_label_put(item["sha256"], item["ts"], "unknown", 0.0, {},
                                 "missing", VERSION)
            continue
        try:
            lines = engine_fn(path)
        except Exception as exc:            # 单张失败 → 记 missing，不拖垮整轮
            out["missing"] += 1
            store.shot_label_put(item["sha256"], item["ts"], "unknown", 0.0, {},
                                 f"error:{type(exc).__name__}", VERSION)
            continue
        text = normalize(" ".join(lines))
        got = classify(text, min_hits=min_hits)
        if dry_run:
            out["labeled"] += 0                   # dry-run 只算不写
            continue
        store.shot_text_put(item["sha256"], item["ts"], len(text), len(lines),
                            text if keep_raw else None, text_sha(text),
                            str(config.get("vision.engine", "rapidocr") or "rapidocr"), VERSION)
        store.shot_label_put(item["sha256"], item["ts"], got["category"],
                             got["score"], got["hits"], "rules", VERSION)
        out["labeled"] += 1
        out["categories"][got["category"]] = out["categories"].get(got["category"], 0) + 1

    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


def summary_line(result: dict) -> str:
    """一行白话：本轮读了几张、各类别几张、有没有降级。"""
    if "skipped" in result:
        return f"画面语义：跳过（{result['skipped']}）"
    if "error" in result:
        return f"画面语义：降级（{result['error']}）"
    if not result.get("pending"):
        return "画面语义：没有新画面要读"
    cats = "，".join(f"{k} {v} 张" for k, v in sorted(result.get("categories", {}).items()))
    extra = f"，{result['missing']} 张读失败" if result.get("missing") else ""
    return f"画面语义：读 {result['labeled']} 张（{cats or '全部 unknown'}）{extra}"


def dumps_hits(hits: dict) -> str:
    return json.dumps(hits, ensure_ascii=False, sort_keys=True)
