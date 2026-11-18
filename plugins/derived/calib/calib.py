"""M14 校准：把「手上/眼睛/在场」三路特征拿去和你自己的自报对一遍，算 AUC。

契约见 INTERFACE.md §2.13 / §4.20。

为什么要有这一层：前面所有特征都是**猜的相关性**（文献结论 + 我的判断）。
只有跟你亲口说的「刚才专注/一般/走神」对齐过，才知道哪一路真有判别力。
没有这一步，专注度分数就只是我在自说自话。
"""

from __future__ import annotations

from datetime import datetime, timedelta

LEVELS = {1: "专注", 2: "一般", 3: "走神"}


def auc(scores: list[float], labels: list[int]) -> float:
    """AUC（Mann-Whitney U 形式）：labels=1 是正类。样本不足或无正负类返回 nan。"""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / (len(pos) * len(neg))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def features(store, start: str, end: str) -> dict:
    """某个时间窗内的三路特征（全部是「每窗口的速率」，不是累计值）。"""
    inputs = [e["payload"] for e in store.events(source="input", since=start, limit=20000)
              if e["ts"] <= end]
    cams = [e["payload"] for e in store.events(source="camera", since=start, limit=20000)
            if e["ts"] <= end]
    acts = [e["payload"] for e in store.events(source="activity", since=start, limit=20000)
            if e["ts"] <= end]

    n_in = max(1, len(inputs))
    keys = sum(int(p.get("keys", 0)) for p in inputs) / n_in
    clicks = sum(int(p.get("clicks", 0)) for p in inputs) / n_in
    dist = sum(int(p.get("dist_px", 0)) for p in inputs) / n_in
    straight = _mean([float(p.get("straightness", 0.0)) for p in inputs if p.get("path_px")])

    n_act = max(1, len(acts))
    presence = sum(1.0 for p in acts if p.get("active")) / n_act
    facing = (sum(1.0 for p in cams if p.get("state") == "facing") / len(cams)) if cams else 0.0
    return {"input_samples": len(inputs), "camera_samples": len(cams),
            "keys_per_win": round(keys, 1), "clicks_per_win": round(clicks, 1),
            "dist_per_win": round(dist, 0), "straightness": round(straight, 3),
            "presence": round(presence, 3), "facing": round(facing, 3)}


def report(store, days: int = 2, window_min: int = 60) -> dict:
    """按每条自报取「它之前 window_min 分钟」的特征，算各路 AUC。"""
    now = datetime.now().astimezone()
    since = (now - timedelta(days=int(days))).isoformat(timespec="seconds")
    reports = [e for e in store.events(source="self", since=since, limit=5000)]
    rows = []
    for ev in reports:
        level = int(ev["payload"].get("level", 0) or 0)
        end = ev["ts"]
        start = (datetime.fromisoformat(end) - timedelta(minutes=int(window_min))).isoformat(timespec="seconds")
        feat = features(store, start, end)
        feat.update({"ts": end, "level": level, "label": 1 if level == 1 else 0})
        rows.append(feat)

    feats = ["keys_per_win", "clicks_per_win", "dist_per_win", "straightness", "presence", "facing"]
    scores = {}
    for f in feats:
        vals = [(float(r[f]), int(r["label"])) for r in rows if r["input_samples"] or r["camera_samples"]]
        scores[f] = auc([v for v, _ in vals], [y for _, y in vals]) if vals else float("nan")
    return {"reports": len(rows), "days": days, "window_min": window_min,
            "auc": {k: (None if v != v else round(v, 3)) for k, v in scores.items()},
            "by_level": {LEVELS[k]: sum(1 for r in rows if r["level"] == k) for k in LEVELS},
            "detail": rows}
