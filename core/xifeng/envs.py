"""环境变量兼容层（D21 改名）。

新名 `XIFENG_*` 优先；旧名 `VOICEIME_*` 只读兼容（方便旧脚本/排障命令）。
写入方 MUST 用新名，旧名不保证长期存在。
"""

from __future__ import annotations

import os

NEW_PREFIX = "XIFENG_"
LEGACY_PREFIX = "VOICEIME_"


def get(name: str, default: str | None = None) -> str | None:
    """读 `XIFENG_<name>`，没有则读 `VOICEIME_<name>`，都没有返回 `default`。"""
    for prefix in (NEW_PREFIX, LEGACY_PREFIX):
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default
