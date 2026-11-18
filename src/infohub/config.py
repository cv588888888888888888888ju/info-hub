"""配置加载。键语义见 INTERFACE.md §3；数值只在 hub.config.yaml 一处。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_DIR / "hub.config.yaml"


class ConfigError(Exception):
    """配置缺失或非法（对应 INTERFACE.md §6「库不可写 / 配置非法」）。"""


class Config:
    def __init__(self, raw: dict, path: Path):
        if not isinstance(raw, dict):
            raise ConfigError(f"配置根必须是映射: {path}")
        self.raw = raw
        self.path = path

    # ---- 取值 -------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def collector(self, name: str) -> dict:
        node = self.raw.get("collect") or {}
        cfg = node.get(name)
        return dict(cfg) if isinstance(cfg, dict) else {}

    def collector_get(self, name: str, key: str, default: Any = None) -> Any:
        return self.collector(name).get(key, default)

    def enabled_collectors(self) -> list[str]:
        node = self.raw.get("collect") or {}
        return [n for n, c in node.items() if isinstance(c, dict) and c.get("enabled")]

    # ---- 路径（root 可被 HUB_ROOT 覆盖，供测试） --------------------------
    @property
    def root(self) -> Path:
        return Path(os.environ.get("HUB_ROOT") or self.get("root") or "").expanduser()

    def under_root(self, key: str) -> Path:
        return self.root / (self.get(key) or "")

    @property
    def db_path(self) -> Path:
        return self.under_root("db")

    @property
    def bus_path(self) -> Path:
        return self.under_root("bus")

    @property
    def reports_dir(self) -> Path:
        return self.under_root("reports_dir")

    @property
    def log_path(self) -> Path:
        return self.under_root("log_dir") / (self.get("log_name") or "hub.log")

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    def ensure_dirs(self) -> None:
        for p in (self.root, self.db_path.parent, self.bus_path.parent,
                  self.reports_dir, self.log_path.parent, self.state_dir):
            p.mkdir(parents=True, exist_ok=True)


def load(path: str | os.PathLike | None = None) -> Config:
    """读配置：显式路径 > $HUB_CONFIG > 项目内 hub.config.yaml。"""
    resolved = Path(path or os.environ.get("HUB_CONFIG") or DEFAULT_CONFIG).expanduser()
    if not resolved.is_file():
        raise ConfigError(f"配置文件不存在: {resolved}")
    if yaml is None:  # pragma: no cover
        raise ConfigError("缺少 PyYAML")
    with resolved.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config(raw, resolved)
