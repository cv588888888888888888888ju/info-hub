"""视图插件解析（S15）：本体只按清单找**已启用**的 view 插件，不 import 任何报表/归纳/周报实现。

契约见 INTERFACE.md §10（清单 / 状态 / 隔离）。调用方拿到模块后自己用，本文件不做业务。

失败语义（调用方折成退出码 1，绝不静默什么都不做）：
- 清单里没有该名字的 view 插件 ⇒ ViewError「未安装」
- 有但 `plugins.<name>.enabled=false` / `default_enabled=false` ⇒ ViewError「未启用」
- 有且启用但清单非法 / entry 导不进来 ⇒ ViewError「加载失败」，reason 原文带出来

注意：`plugin.list_plugins()` 为了可机械验证会在 `list` 时导入插件（§10「加载副作用」），
与「启用」是两件事 —— 这里按**启用**判，禁用/报错的插件一律不许被调用。
"""

from __future__ import annotations

from . import plugin as plugins


class ViewError(RuntimeError):
    """视图插件缺位（未安装 / 未启用 / 加载失败）；调用方折成退出码 1。"""


def _row(config, name: str) -> dict:
    """按名字找 view 插件行；缺位直接抛 ViewError（三个原因分开说，别混成一句）。"""
    fallback: dict | None = None
    for row in plugins.list_plugins(config):
        if row.get("kind") != "view" or row.get("name") != name:
            continue
        if row.get("shadowed"):
            continue
        if row.get("status") == "ok":
            return row
        if row.get("status") == "disabled":
            raise ViewError(f"view 插件未启用: {name}（plugins.{name}.enabled=false）")
        fallback = row
    if fallback is not None:
        raise ViewError(f"view 插件加载失败: {name}（{fallback.get('reason')}）")
    raise ViewError(f"view 插件未安装: {name}（应在 plugins/views/{name}/plugin.yaml）")


def module(config, name: str):
    """取已启用 view 插件的模块（多个函数时用这个，例：report.generate / report.read_report）。"""
    return plugins.load_module(_row(config, name))


def entry(config, name: str):
    """取清单 `entry` 指向的那个函数（单入口插件用这个）。"""
    return plugins.load_entry(_row(config, name))
