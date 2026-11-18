"""M12 画面粗判：本地小 VLM 看图判「工作 / 玩」。

契约见 INTERFACE.md §4.17 / §5.11，参数只放 hub.config.yaml 的 vision.vlm.*。

为什么单独一层：M11 的 OCR 词表分得清「bilibili 页面」，分不清「这个视频是教程还是娱乐」；
画面整体布局（IDE 三栏 / 视频全屏 / 游戏）是 VLM 的强项。两套并存，互不覆盖：
shot_labels 记细类，shot_verdicts 记粗判。

模型跑在 llama.cpp 的 llama-mtmd-cli 上（CPU，GGUF）。不新增常驻进程，也不新增定时器：
挂在 hub-rules derive 里顺带跑。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from infohub.vision_keywords import VERDICTS

VERSION = "vlm-1"                      # 提示词/映射逻辑的版本；换提示词必须换号
PROMPT = (
    "下面是一张电脑屏幕截图。请只回答一个词：\n"
    "- 如果是在工作（写代码、看文档、办公、学习、开终端、跟 AI 干活），回「工作」\n"
    "- 如果是在娱乐（看视频、游戏、购物、刷聊天），回「玩」\n"
    "只输出一个词。"
)


class VlmUnavailable(Exception):
    """缺少 llama.cpp 二进制或模型文件。"""


def resolve(config) -> tuple[Path, Path, Path]:
    """返回 (二进制, 模型, mmproj)；缺任何一个都抛 VlmUnavailable。"""
    root = Path(config.root)
    names = [str(config.get("vision.vlm.model", "") or ""), str(config.get("vision.vlm.mmproj", "") or "")]
    paths = []
    for name in names:
        p = Path(name).expanduser()
        paths.append(p if p.is_absolute() else root / p)
    model, mmproj = paths

    bin_dir = Path(str(config.get("vision.vlm.bin_dir", "vendor/llama-b11840")))
    bin_dir = bin_dir if bin_dir.is_absolute() else root / bin_dir
    exe = bin_dir / "llama-mtmd-cli"
    if not exe.exists():                # 允许 vendored 目录带版本号
        found = sorted(bin_dir.parent.glob("llama-*/llama-mtmd-cli"))
        exe = found[-1] if found else exe

    if not exe.exists():
        raise VlmUnavailable(f"缺 llama.cpp 二进制（{exe}）")
    if not model.exists():
        raise VlmUnavailable(f"缺模型文件（{model}）")
    if not mmproj.exists():
        raise VlmUnavailable(f"缺 mmproj（{mmproj}）")
    return exe, model, mmproj


def downscale(src: Path, dst: Path, max_width: int, quality: int) -> tuple[int, int]:
    """等比缩到 max_width 宽再存 JPEG（1280×800 原图直接喂既慢又没收益）。"""
    import cv2                          # 本机装了 opencv（rapidocr 的依赖）

    img = cv2.imread(str(src))
    if img is None:
        raise ValueError("读图失败")
    h, w = img.shape[:2]
    if w > max_width:
        nh = max(1, int(round(h * max_width / w)))
        img = cv2.resize(img, (max_width, nh), interpolation=cv2.INTER_AREA)
    out_h, out_w = img.shape[:2]
    if not cv2.imwrite(str(dst), img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]):
        raise ValueError("写缩图失败")
    return out_w, out_h


def verdict_of(raw: str) -> str:
    """把模型原话映射成 work/play/unknown；表外词一律 unknown（不许猜）。"""
    text = (raw or "").strip().lower()
    if not text:
        return "unknown"
    hits = {name: sum(1 for w in words if w.lower() in text) for name, words in VERDICTS.items()}
    best = max(hits.values())
    if best == 0:
        return "unknown"
    winners = [k for k, v in hits.items() if v == best]
    return winners[0] if len(winners) == 1 else "unknown"


def _default_runner(config):
    """真实推理：每张图起一个 llama-mtmd-cli 进程（0.5B/2B 都是秒级，够用且不常驻）。"""
    exe, model, mmproj = resolve(config)
    threads = int(config.get("vision.vlm.threads", 2) or 2)
    ctx = int(config.get("vision.vlm.ctx_size", 512) or 512)
    timeout = float(config.get("vision.vlm.timeout_s", 60) or 60)
    env = dict(os.environ, LLAMA_LOG_LEVEL="error")

    def run_one(image: Path) -> str:
        cmd = [
            str(exe), "-m", str(model), "--mmproj", str(mmproj),
            "-p", PROMPT, "--image", str(image),
            "-c", str(ctx), "-n", "16", "--temp", "0", "-t", str(threads),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"rc={proc.returncode}")
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        return lines[-1] if lines else ""

    return run_one


def run(config, store, limit: int | None = None, runner=None, dry_run: bool = False) -> dict:
    """给「还没粗判过」的截屏补 verdict。失败只返回 error，不抛给调用方。"""
    enabled = bool(config.get("vision.vlm.enabled", False))
    if not enabled:
        return {"skipped": "vision.vlm.enabled=false"}

    n = int(limit if limit is not None else config.get("vision.vlm.max_per_run", 8) or 8)
    max_width = int(config.get("vision.vlm.max_width", 512) or 512)
    quality = int(config.get("vision.vlm.jpeg_quality", 75) or 75)
    model_name = Path(str(config.get("vision.vlm.model", ""))).name

    try:
        run_one = runner or _default_runner(config)
    except VlmUnavailable as exc:
        return {"error": str(exc)}

    rows = store.unverdict_shots(limit=n, version=VERSION)
    tmp_dir = Path(config.state_dir) / "vlm-tmp"
    result = {"pending": len(rows), "labeled": 0, "failed": 0,
              "verdicts": {"work": 0, "play": 0, "unknown": 0}, "version": VERSION,
              "model": model_name}
    if not rows:
        result["elapsed_s"] = 0.0
        return result

    tmp_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for row in rows:
        src = Path(row["path"])
        sha = row["sha256"]
        small = tmp_dir / f"{sha[:16]}.jpg"
        try:
            if not src.exists():
                raise FileNotFoundError("原图不在")
            w, h = downscale(src, small, max_width, quality)
            t = time.time()
            raw = run_one(small)
            latency = int((time.time() - t) * 1000)
            verdict = verdict_of(raw)
            method = "vlm"
        except Exception as exc:        # 单张失败不中断
            w = h = 0
            latency = 0
            raw = ""
            verdict = "unknown"
            method = f"error:{type(exc).__name__}"
            result["failed"] += 1
        finally:
            small.unlink(missing_ok=True)

        if not dry_run:
            store.shot_verdict_put(sha256=sha, ts=row["ts"], verdict=verdict, raw=raw[:32],
                                   model=model_name, version=VERSION, method=method,
                                   img_w=w, img_h=h, latency_ms=latency)
        result["labeled"] += 1
        result["verdicts"][verdict] += 1

    result["elapsed_s"] = round(time.time() - t0, 2)
    if tmp_dir.exists() and not any(tmp_dir.iterdir()):
        tmp_dir.rmdir()
    return result


def summary_line(result: dict) -> str:
    if "error" in result:
        return f"画面粗判：跳过（{result['error']}）"
    if "skipped" in result:
        return f"画面粗判：跳过（{result['skipped']}）"
    if not result.get("pending"):
        return "画面粗判：没有待判的截屏"
    v = result["verdicts"]
    return (f"画面粗判：判 {result['labeled']} 张（工作 {v['work']} / 玩 {v['play']} /"
            f" 判不出 {v['unknown']}），{result['elapsed_s']}s")
