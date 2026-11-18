"""测试辅助（S17）：按路径加载 plugins/derived/ 下的派生插件模块。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_FILES = {
    "activity": "activity/activity.py",
    "vision": "vision/vision.py",
    "calib": "calib/calib.py",
    "features": "features/features.py",
    "input": "input/inputs.py",
    "camera": "camera/camera.py",
    "screencast": "screencast/screencast.py",
}
_CACHE = {}


def load(name):
    if name in _CACHE:
        return _CACHE[name]
    path = _ROOT / "plugins" / "derived" / _FILES[name]
    spec = importlib.util.spec_from_file_location("_derived_test_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _CACHE[name] = mod
    return mod


def install():
    """把 7 个派生模块注册成 `_d_<name>`，让测试用 `import _d_activity as A` 这种无引号写法。"""
    for name in _FILES:
        sys.modules.setdefault("_d_" + name, load(name))
