"""pytest 全局夹具。

最重要的一条：**配置目录隔离**。设置现在外置成 JSON 文件
（`%LOCALAPPDATA%\\Xifeng\\config.json`），如果不隔离，测试会读写用户真实
的配置文件——既污染用户环境，也让用例之间互相污染（一个用例改了热键，
下一个用例就读到改过的值）。

这里把 `IME_DATA_DIR` 指到每个用例自己的 tmp 目录：配置、数据库、日志
全落在临时目录，用例跑完自动清掉。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """每个用例一个独立的数据目录（配置 JSON / 数据库都在里面）。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("IME_DATA_DIR", str(data_dir))
    return data_dir
