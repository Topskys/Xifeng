"""开机自启：写当前用户的 `Run` 键。

为什么用注册表而不是「启动」文件夹：
- 不往磁盘放 `.lnk`，卸载/关闭时删一个值就干净
- `HKCU` 下写入不需要管理员，也不触发 UAC
- 命令行可带参数（`--tray`），启动即托盘，不弹控制台

命令形态（`--tray` 会自己隐藏控制台窗口）：

- 冻结 exe：`"C:\\...\\VoiceIME.exe" --tray`
- 源码运行：`"C:\\...\\python.exe" "C:\\...\\run_spike.py" --tray`
"""

from __future__ import annotations

import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winreg
else:  # pragma: no cover - 本项目只交付 Windows
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "VoiceIME"


def app_command(exe: str | None = None) -> str:
    """自启要执行的命令行（带引号，路径含空格也不会断）。"""
    if getattr(sys, "frozen", False):
        target = exe or sys.executable
        return f'"{target}" --tray'
    entry = Path(__file__).resolve().parents[1] / "run_spike.py"
    return f'"{sys.executable}" "{entry}" --tray'


def is_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable(command: str | None = None) -> str:
    """写入自启并返回写入的命令；已存在则覆盖。"""
    if not IS_WINDOWS:
        raise RuntimeError("开机自启只支持 Windows")
    cmd = command or app_command()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
    return cmd


def disable() -> bool:
    """删除自启项；不存在返回 False。"""
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        return True
    except FileNotFoundError:
        return False


def toggle() -> bool:
    """切换并返回切换后的状态。"""
    if is_enabled():
        disable()
        return False
    enable()
    return True


def status_text() -> str:
    if not IS_WINDOWS:
        return "开机自启：不支持（非 Windows）"
    if not is_enabled():
        return "开机自启：关闭"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
    except OSError:
        value = "（读不到命令）"
    return f"开机自启：开启 → {value}"
