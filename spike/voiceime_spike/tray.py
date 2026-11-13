"""托盘图标：常驻但不打扰。

为什么要托盘而不是"控制台窗口一直开着"：控制台会被误关（一关就退出）、
占任务栏、看起来像没做完。托盘是 Windows 常驻程序的标准形态。

实现选择 `pystray`（MIT，纯 ctypes 调 Win32 Shell_NotifyIcon，不需要 pywin32）：
自己写 `Shell_NotifyIcon` + 隐藏消息窗口需要 200+ 行 ctypes，收益不大。

约束：
- 托盘菜单动作 MUST 短：打开窗口是**另起进程**（`--web --tab ...`），不在托盘进程里跑
  tkinter（tkinter 要求主线程，而主线程要给 PTT 录循环）；`--gui` 只作旧版兜底
- 「退出」先关数据库再 `os._exit`，避免 WAL 留脏
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from . import autostart

CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183

_mutex = None


def acquire_single_instance(name: str = "Local\\VoiceIME-Tray") -> bool:
    """托盘只允许一个实例：两个实例会各自装钩子、各录一份音。

    用命名 Mutex（会话内 `Local\\`，不需要管理员）。进程退出时内核自动释放，
    所以崩溃后不会留下"锁死"的假状态。返回值只表示"我是不是第一个"。
    """
    global _mutex
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.SetLastError(0)
        _mutex = kernel32.CreateMutexW(None, False, name)
        return int(kernel32.GetLastError()) != ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 - 拿不到锁时宁可放行，也不要打不开程序
        return True


def _image():
    """托盘图标：黑底白色麦克风（与主界面品牌一致；不依赖外部图片资源）。

    深色任务栏下黑底不可见时靠 1px 浅灰描边区分。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 61, 61), radius=14, fill=(24, 24, 27, 255), outline=(212, 212, 216, 255), width=2)
    # 麦克风头
    d.rounded_rectangle((25, 13, 39, 38), radius=7, fill=(255, 255, 255, 255))
    # 支架
    d.arc((17, 24, 47, 50), start=0, end=180, fill=(255, 255, 255, 255), width=4)
    d.line((32, 48, 32, 55), fill=(255, 255, 255, 255), width=4)
    d.line((23, 55, 41, 55), fill=(255, 255, 255, 255), width=4)
    return img


def gui_command(tab: str = "stats") -> list[str]:
    """打开主窗口的命令（子进程，独立于托盘/PTT 进程）。

    Tauri 交付形态（D14）：优先打开壳 exe（voiceime-tauri.exe，GUI 窗口），
    `tab` 参数由前端 hash 决定，这里带不上（壳默认落在统计页）；
    找不到壳时退回 Web GUI（`--web --tab`，Edge --app 旧方案，仍是可用兜底）。
    """
    if getattr(sys, "frozen", False):
        # sidecar 在 `<安装根>\core\VoiceIME-Core\VoiceIME-Core.exe`，壳在安装根。
        shell = Path(sys.executable).resolve().parents[2] / "voiceime-tauri.exe"
        if shell.is_file():
            return [str(shell)]
        return [sys.executable, "--web", "--tab", tab]
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_spike.py")
    return [sys.executable, entry, "--web", "--tab", tab]


def launch_gui(tab: str = "stats") -> None:
    """另起一个进程开窗口；失败只打日志，不让托盘崩。"""
    try:
        kw: dict = {}
        if sys.platform == "win32":
            kw["creationflags"] = CREATE_NO_WINDOW
        subprocess.Popen(gui_command(tab), close_fds=True, **kw)
    except Exception as exc:  # noqa: BLE001
        print(f"[tray] 打开窗口失败：{exc}", flush=True)


class Tray:
    """托盘封装：菜单交给调用方，本模块只管图标与生命周期。"""

    def __init__(
        self,
        on_quit: Callable[[], None],
        *,
        title: str = "VoiceIME —— 按住 Ctrl 说话",
        on_open: Callable[[], None] | None = None,
    ) -> None:
        self._on_quit = on_quit
        self._on_open = on_open or (lambda: launch_gui("stats"))
        self._title = title
        self._icon = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 菜单

    def _menu(self):
        import pystray

        # 两项即可（2026-11-11 用户决策）：显示主界面 / 退出。
        # 页面切换、开机自启都在主窗口里做，托盘不再承担二级菜单。
        return pystray.Menu(
            pystray.MenuItem("显示主界面", lambda icon, item: self._on_open(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda icon, item: self._quit()),
        )

    def _quit(self) -> None:
        self.stop()
        self._on_quit()

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """启动托盘。优先 `run_detached`，不支持时退到后台线程。"""
        import pystray

        self._icon = pystray.Icon("VoiceIME", _image(), self._title, self._menu())
        try:
            self._icon.run_detached()
        except NotImplementedError:
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
        print("[tray] 图标已加载（后台线程负责显示）", flush=True)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None

    def notify(self, title: str, message: str) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception:  # noqa: BLE001
            pass


def hide_console() -> bool:
    """把本进程的控制台窗口藏起来（`--tray` 用）。

    打包成 console 程序后无法在运行期变 windowed；藏窗口是单份产物里最省事的做法。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        return True
    except Exception:  # noqa: BLE001
        return False
