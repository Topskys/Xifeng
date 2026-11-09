"""Windows 环境探测：完整性级别是本项目最关键的环境前提（G21）。

Low 完整性下全局钩子收不到硬件输入，`GetAsyncKeyState` 恒 0 ——
把这档信息打进 CLI 输出，能省掉一轮"按键失灵"的误判。
"""

from __future__ import annotations

import pytest

from voiceime_spike import winenv


@pytest.mark.skipif(not winenv.IS_WINDOWS, reason="只有 Windows 有令牌")
def test_integrity_level_is_readable() -> None:
    level = winenv.integrity_level()
    assert "(" in level and level.endswith(")")
    assert any(name in level for name in ("Low", "Medium", "High", "System", "Unknown"))


@pytest.mark.skipif(not winenv.IS_WINDOWS, reason="只有 Windows 有会话")
def test_session_id_is_positive() -> None:
    assert winenv.session_id() >= 0


def test_describe_is_one_line() -> None:
    line = winenv.describe()
    assert "\n" not in line and line


def test_is_low_integrity_returns_bool() -> None:
    assert isinstance(winenv.is_low_integrity(), bool)


@pytest.mark.skipif(not winenv.IS_WINDOWS, reason="只有 Windows 有前台窗口")
def test_foreground_pid_is_none_or_positive() -> None:
    pid = winenv.foreground_pid()
    assert pid is None or pid > 0
