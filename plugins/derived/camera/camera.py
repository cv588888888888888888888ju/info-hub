"""M14 在座状态：摄像头每秒看一眼「有没有人、脸朝哪边」。

契约见 INTERFACE.md §4.19 / §5.13。

**硬边界（比功能更重要）**：
- 只在内存里取一帧，**判定完立刻丢弃，不落盘、不落库、不压缩、不上传**；
- 只输出三档状态 `away / present / facing` + 人脸数 + 人脸占比 + 偏航量；
- **不做表情、不做情绪、不做身份识别**（同一张脸不建 ID，跨帧不关联）。

为什么要它：M10 只有「键鼠在不在动」，人坐在那儿看屏幕不动 ≠ 走神；
这一层补「人在不在、脸朝不朝屏幕」。用的是 YuNet（232 KB，12 ms/帧），
比 2B VLM 便宜一个量级，也避免把摄像头画面上传给任何模型。
"""

from __future__ import annotations

from pathlib import Path

VERSION = "cam-1"
STATES = ("away", "present", "facing")     # 没脸 / 有脸但不朝屏幕 / 朝屏幕


def _detector(config):
    import cv2

    try:                                   # OpenCV 的 target 警告会污染 stderr，压掉
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except Exception:
        pass
    model = Path(str(config.get("collect.camera.model", "models/yunet-2023mar.onnx")))
    model = model if model.is_absolute() else Path(config.root) / model
    if not model.exists():
        raise FileNotFoundError(f"缺人脸模型（{model}）")
    return cv2.FaceDetectorYN_create(str(model), "", (320, 320), 0.6, 0.3, 500)


def classify(faces, frame_w: int, frame_h: int,
             min_face_ratio: float = 0.05, yaw_max: float = 0.25) -> dict:
    """把检测结果压成三档状态。纯函数，便于测试。

    faces 每行 15 个数：[x, y, w, h, 右眼(x,y), 左眼(x,y), 鼻尖(x,y), 右嘴角(x,y), 左嘴角(x,y), score]
    """
    if faces is None or len(faces) == 0:
        return {"state": "away", "faces": 0, "face_ratio": 0.0, "yaw": 0.0}

    best = None
    for f in faces:
        fw = float(f[2])
        ratio = fw / float(frame_w or 1)
        kp = [(float(f[4 + 2 * i]), float(f[5 + 2 * i])) for i in range(5)]
        eye_mid_x = (kp[0][0] + kp[1][0]) / 2.0
        eye_dx = abs(kp[1][0] - kp[0][0])
        yaw = (kp[2][0] - eye_mid_x) / eye_dx if eye_dx > 1e-6 else 0.0
        cand = {"face_ratio": round(ratio, 3), "yaw": round(yaw, 3), "score": float(f[-1])}
        if best is None or cand["face_ratio"] > best["face_ratio"]:
            best = cand
    assert best is not None
    facing = best["face_ratio"] >= min_face_ratio and abs(best["yaw"]) <= yaw_max
    return {"state": "facing" if facing else "present", "faces": len(faces),
            "face_ratio": best["face_ratio"], "yaw": best["yaw"]}


def sample(config) -> dict:
    """取一帧 → 判定 → 丢帧。**这个函数不写任何文件。**"""
    import cv2

    det = _detector(config)
    index = int(config.get("collect.camera.index", 0) or 0)
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise OSError("摄像头打不开")
    try:
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok or frame is None:
        raise OSError("摄像头取帧失败")

    h, w = frame.shape[:2]
    det.setInputSize((w, h))
    import time
    t0 = time.time()
    _n, faces = det.detect(frame)
    got = classify(faces, w, h,
                   min_face_ratio=float(config.get("collect.camera.min_face_ratio", 0.05) or 0.05),
                   yaw_max=float(config.get("collect.camera.yaw_max", 0.25) or 0.25))
    got.update({"latency_ms": int((time.time() - t0) * 1000), "w": w, "h": h,
                "version": VERSION})
    del frame                              # 显式丢帧：函数返回后内存里不留图像
    return got
