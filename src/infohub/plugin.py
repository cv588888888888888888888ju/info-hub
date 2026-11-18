"""插件宿主：发现 plugin.yaml、按清单加载 entry、隔离坏插件（契约 INTERFACE.md §10）。

本文件只做「发现 / 校验 / 导入」三件事，不认识任何具体采集器 / 规则 / 视图实现；
调用方拿 `load_enabled()` 的结果自己决定怎么用。
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

import yaml

from .config import Config, PROJECT_DIR

KINDS = ("source", "processor", "view", "derived", "channel")
API = 1
MANIFEST = "plugin.yaml"
REQUIRED = ("name", "kind", "api", "entry")


class PluginError(Exception):
    """单个插件坏了（清单非法 / 入口不存在 / 导入抛异常）；不冒泡到整个扫描。"""


def _roots(config: Config) -> list[Path]:
    """发现根按序：项目 plugins/ → $HUB_ROOT/plugins → plugins.paths（去重保序）。"""
    roots = [PROJECT_DIR / "plugins", config.root / "plugins"]
    extra = config.get("plugins.paths")
    if extra is not None and not isinstance(extra, list):
        raise PluginError("plugins.paths 必须是数组")
    for item in extra or []:
        roots.append(Path(str(item)).expanduser())
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def _check_requires(raw: dict) -> None:
    requires = raw.get("requires") or {}
    if not isinstance(requires, dict):
        raise PluginError("requires 必须是映射")
    for module in requires.get("python") or []:
        if importlib.util.find_spec(str(module)) is None:
            raise PluginError(f"缺 python 依赖: {module}")
    for command in requires.get("commands") or []:
        if shutil.which(str(command)) is None:
            raise PluginError(f"缺命令: {command}")


def _module_python(base: Path, entry: str, plugin_name: str):
    """按 entry 导入插件模块；返回 (module, rel, func)。清单非法 ⇒ PluginError。"""
    rel, sep, func = entry.partition(":")
    if not sep or not rel or not func:
        raise PluginError(f"entry 必须写成 相对路径:函数名，实际: {entry}")
    target = (base / rel).resolve()
    if not target.is_file():
        raise PluginError(f"entry 文件不存在: {target}")
    digest = hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:10]
    modname = f"infohub_plugin_{plugin_name.replace('-', '_')}_{digest}"
    spec = importlib.util.spec_from_file_location(modname, target)
    if spec is None or spec.loader is None:
        raise PluginError(f"entry 加载不了: {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module, rel, func


def _load_python(base: Path, entry: str, plugin_name: str):
    module, rel, func = _module_python(base, entry, plugin_name)
    fn = getattr(module, func, None)
    if not callable(fn):
        raise PluginError(f"entry 函数不存在或不可调用: {rel}:{func}")
    return fn


def _one(path: Path, config: Config) -> dict:
    base = path.parent
    row = {
        "name": base.name,
        "kind": None,
        "api": None,
        "entry": None,
        "runtime": "python",
        "path": str(base),
        "status": "error",
        "enabled": False,
        "reason": None,
        "shadowed": False,
        "config_key": None,
    }
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise PluginError("plugin.yaml 根必须是映射")
        missing = [k for k in REQUIRED if raw.get(k) in (None, "")]
        if missing:
            raise PluginError("缺少键: " + ", ".join(missing))
        name = str(raw["name"]).strip()
        if not name:
            raise PluginError("name 不能为空")
        row["name"] = name
        if raw.get("config_key"):
            row["config_key"] = str(raw["config_key"])
        kind = str(raw["kind"]).strip()
        if kind not in KINDS:
            raise PluginError(f"kind 不认识: {kind}（只认 {', '.join(KINDS)}）")
        row["kind"] = kind
        try:
            api = int(raw["api"])
        except (TypeError, ValueError):
            raise PluginError(f"api 必须是整数: {raw['api']!r}")
        row["api"] = api
        if api != API:
            raise PluginError(f"api 不支持: {api}（当前只认 {API}）")
        runtime = str(raw.get("runtime") or "python").strip()
        if runtime not in ("python", "node"):
            raise PluginError(f"runtime 不认识: {runtime}")
        row["runtime"] = runtime
        entry = str(raw["entry"]).strip()
        row["entry"] = entry
        _check_requires(raw)
        if runtime == "python":
            _load_python(base, entry, name)
        else:
            rel = entry.partition(":")[0]
            if not (base / rel).is_file():
                raise PluginError(f"entry 文件不存在: {(base / rel).resolve()}")
        enabled = bool(config.get(f"plugins.{name}.enabled", raw.get("default_enabled", False)))
        row["enabled"] = enabled
        row["status"] = "ok" if enabled else "disabled"
    except PluginError as exc:
        row["reason"] = str(exc)
    except Exception as exc:  # 导入期任意异常；原文留档，不吞
        row["reason"] = f"{type(exc).__name__}: {exc}"
    return row


def list_plugins(config: Config) -> list[dict]:
    """扫描全部发现根，返回逐插件状态（坏插件不抛，只记 reason）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for root in _roots(config):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(MANIFEST)):
            row = _one(path, config)
            if row["name"] in seen:
                row["shadowed"] = True
            else:
                seen.add(row["name"])
            out.append(row)
    out.sort(key=lambda r: (r["name"], str(r["path"])))
    return out


def load_enabled(config: Config, kind: str | None = None) -> list[dict]:
    """只要 status=ok 且未被 shadowed 的插件；kind 给了就按阶段过滤。"""
    rows = [r for r in list_plugins(config) if r["status"] == "ok" and not r["shadowed"]]
    if kind is not None:
        rows = [r for r in rows if r["kind"] == kind]
    return rows


def load_entry(row: dict):
    """按清单行取入口函数（只有 runtime=python 的插件有）；调用方负责只对 status=ok 的行调用。"""
    if row.get("runtime", "python") != "python":
        raise PluginError(f"runtime={row.get('runtime')} 没有 python 入口: {row.get('name')}")
    return _load_python(Path(str(row["path"])), str(row["entry"]), str(row["name"]))


def load_module(row: dict):
    """按清单行取入口所在模块（加载 / 隔离规则与 load_entry 同一套）。

    给单元测试与派生插件复用「采集器里的纯函数」用；不看 kind / enabled 开关，
    调用方自己按清单行判断要不要用。
    """
    if row.get("runtime", "python") != "python":
        raise PluginError(f"runtime={row.get('runtime')} 没有 python 模块: {row.get('name')}")
    module, _rel, _func = _module_python(Path(str(row["path"])), str(row["entry"]),
                                        str(row["name"]))
    return module
