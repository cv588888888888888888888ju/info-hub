"""来源注册表（S3）：来源只剩插件，本体不认识任何具体采集器（INTERFACE.md §10）。

注册表 = plugins/**/plugin.yaml 里 kind=source 的插件，dict[name]->collect；
collect 统一签名 collect(config, store) -> list[event]（INTERFACE.md §4）。
无配置上下文时返回空 dict（没有插件根就没法发现来源）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class CollectorError(Exception):
    """单个采集器失败（不影响其它采集器，见 INTERFACE.md §6）。"""


def _manifest_default_enabled(base: Path) -> bool:
    """plugin.yaml 的 default_enabled；读不到就当 false（坏清单由 plugin.py 记 error）。"""
    try:
        import yaml
        raw = yaml.safe_load((base / "plugin.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    return bool(raw.get("default_enabled", False)) if isinstance(raw, dict) else False


def registry(config=None, only_enabled: bool = True) -> dict[str, Callable]:
    """把 plugins/**/plugin.yaml 里 kind=source 的插件并进注册表（INTERFACE.md §4.4 / §10）。

    两道开关（INTERFACE.md §10）：
    - 装没装：plugins.<name>.enabled（缺省 true；显式 false 才跳过）—— 对 --source 也生效；
    - 这轮跑不跑：collect.<name>.enabled（缺省 = 清单 default_enabled）—— only_enabled=False
      （即 --source 显式点名）时不看。
    """
    if config is None:                       # 无配置上下文时没有来源（selftest 的合成注册表也走这里）
        return {}
    from . import plugin as plugins

    out: dict[str, Callable] = {}
    try:
        rows = plugins.list_plugins(config)
    except Exception:                        # 宿主本身坏了也不能拖垮采集轮次
        return out
    for row in rows:
        if row["kind"] != "source" or row["shadowed"] or row["status"] != "ok":
            continue
        name = row["name"]
        base = Path(row["path"])
        if config.get(f"plugins.{name}.enabled") is False:
            continue
        if only_enabled:
            run = config.get(f"collect.{name}.enabled")
            if run is None:
                run = _manifest_default_enabled(base)
            if not run:
                continue
        try:
            out[name] = plugins.load_entry(row)
        except Exception:                    # 入口导入失败：跳过该插件，其它来源照跑
            continue
    return out


def load_module(name: str, config=None):
    """取某个来源插件的模块对象（给单元测试 / 派生层复用纯函数）。

    不看 collect.<name>.enabled（那是「这一轮采不采」的开关），只看插件装没装上、清单可不可读。
    """
    if config is None:
        from .config import load as _load
        config = _load()
    from . import plugin as plugins

    row = next((r for r in plugins.list_plugins(config)
                if r["name"] == name and r["kind"] == "source" and not r["shadowed"]), None)
    if row is None:
        raise LookupError(f"没有这个来源插件: {name}")
    return plugins.load_module(row)
