"""手机通道 token：显式配置 > $HUB_ROOT/phone_api.token > 首次自动生成（0600）。

为什么：token 是每台机器自己的东西，不该写死在仓库 / APK / 文档里。
优先级（INTERFACE §11.10）：
1. `phone_api.token`（或旧键 `phone_remind.helper_token`）非空 ⇒ 用它（手动覆盖）；
2. `$HUB_ROOT/phone_api.token` 存在且非空 ⇒ 用它（上次生成的）；
3. 都没有 ⇒ 生成 `hub-<32 hex>` 写入该文件（0600）并返回。
"""

from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path

FILE_NAME = "phone_api.token"


def token_file(config) -> Path:
    return Path(config.root) / FILE_NAME


def get_token(config, explicit: str | None = None) -> str:
    """取 token；没有就生成并落盘。`explicit` 是配置里的值（可空）。"""
    value = str(explicit or "").strip()
    if value:
        return value
    path = token_file(config)
    try:
        if path.is_file():
            saved = path.read_text(encoding="utf-8").strip()
            if saved:
                return saved
    except OSError:
        pass
    token = "hub-" + _secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return token