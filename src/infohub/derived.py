"""派生层插件解析（S17）：本体只按清单找**已启用**的 derived 插件，不 import 任何派生实现。

契约见 INTERFACE.md §10（清单 / 状态 / 隔离）。调用方拿到模块后自己用，本文件不做业务。

失败语义（调用方折成退出码 1，绝不静默什么都不做）：
- 清单里没有该名字的 derived 插件 ⇒ DerivedError「未安装」
- 有但 `plugins.<name>.enabled=false` / `default_enabled=false` ⇒ DerivedError「未启用」
- 有且启用但清单非法 / entry 导不进来 ⇒ DerivedError「加载失败」，reason 原文带出来

与 views.py 同一套三分语义，只是 kind 不同：view 出报表，derived 出派生计算。
"""

from __future__ import annotations

from . import plugin as plugins


class DerivedError(RuntimeError):
    """派生插件缺位（未安装 / 未启用 / 加载失败）；调用方折成退出码 1。"""


def _row(config, name: str) -> dict:
    """按名字找 derived 插件行；缺位直接抛 DerivedError（三个原因分开说）。"""
    fallback: dict | None = None
    for row in plugins.list_plugins(config):
        if row.get("kind") != "derived" or row.get("name") != name:
            continue
        if row.get("shadowed"):
            continue
        if row.get("status") == "ok":
            return row
        if row.get("status") == "disabled":
            raise DerivedError(f"derived 插件未启用: {name}（plugins.{name}.enabled=false）")
        fallback = row
    if fallback is not None:
        raise DerivedError(f"derived 插件加载失败: {name}（{fallback.get('reason')}）")
    raise DerivedError(f"derived 插件未安装: {name}（应在 plugins/derived/{name}/plugin.yaml）")


def list(config):  # noqa: A001 —— 对外的第三种取法（明细），名字按契约就叫 list
    """列出全部 derived 插件行（含 disabled / error，按 name 升序），给自检与排障用。"""
    return sorted((row for row in plugins.list_plugins(config)
                   if row.get("kind") == "derived"), key=lambda row: row["name"])


def module(config, name: str):
    """取已启用 derived 插件的模块（多个函数时用这个，例：vision.run / features.build）。"""
    return plugins.load_module(_row(config, name))


def entry(config, name: str):
    """取清单 `entry` 指向的那个函数（单入口插件用这个）。"""
    return plugins.load_entry(_row(config, name))
