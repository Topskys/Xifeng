"""PTT 时序自测：用 `SendInput` 模拟按键，不需要人按。

为什么需要它：`05-gotchas.md` G21 说 Low 完整性下全局钩子收不到硬件事件，
PTT 只能人肉测。但在 **Medium** 进程里可以用自注入按键驱动 `record_ptt`，
把"钩子收键 → 松开检测 → 误触门槛 → Esc 取消"整条链路跑一遍，得到可复现的 PASS/FAIL。

它测不到的：物理按键本身的采集。物理键与自注入键走同一个钩子回调，
真正差异只在 UIPI 的完整性检查，所以 Medium 下自注入通过后，
剩下唯一的未知项是"你的键盘有没有被安全软件拦"——那只能真人按一次。

用法（MUST 在 Medium 完整性进程里）：

    uv run python ptt_selftest.py        # 若目录带 Low 标签，脚本会直接说明并退出

目录带 Low 标签时（本机 `D:\\project\\workspace` 就是这样），改用目录外的解释器：

    uv run python -c "import tempfile,subprocess,sys;..."   # 或见 key_diag.py 的说明

副作用：真的按下/松开 Ctrl 与 Esc（不打字）。前台程序可能看到修饰键事件。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

import numpy as np

from xifeng import hotkey, winenv

VK_LCTRL = 0xA2
VK_RCTRL = 0xA3
VK_ESCAPE = 0x1B
VK_F13 = 0x7C          # 无文本副作用，用来探针

KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT


def send_key(vk: int, up: bool) -> bool:
    """发一个按键事件。`dwExtraInfo` 留空 —— 钩子把它当外部按键（不会命中 MAGIC 过滤）。"""
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = 0
    inp.u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = None
    return _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1


class FakeStream:
    """替身麦克风：`start()` 时喂一块静音，满足 `PttResult.ok` 的 `pcm.size > 0`。"""

    def __init__(self, callback: Callable) -> None:
        self._cb = callback
        self.started = False

    def start(self) -> None:
        self.started = True
        self._cb(np.zeros((1024, 1), dtype=np.int16), 1024, None, None)

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        pass


@dataclass
class Case:
    name: str
    hold_ms: int
    vk: int = VK_LCTRL
    escape_after_ms: int = 0
    expect_ok: bool = True
    expect_too_short: bool = False
    expect_cancelled: bool = False


CASES = [
    Case("左 Ctrl 按住 700ms", 700),
    Case("左 Ctrl 短按 100ms（模拟 Ctrl+C）", 100, expect_ok=False, expect_too_short=True),
    Case("右 Ctrl 按住 500ms", 500, vk=VK_RCTRL),
    Case("按住 400ms 后按 Esc", 400, escape_after_ms=150, expect_ok=False, expect_cancelled=True),
]


def _press_in_background(case: Case) -> threading.Thread:
    def run() -> None:
        time.sleep(0.25)                       # 让 record_ptt 先进入等待
        send_key(case.vk, up=False)
        if case.escape_after_ms:
            time.sleep(case.escape_after_ms / 1000)
            # 真人按 Esc 至少几十毫秒；这里按 120ms，避免 10ms 轮询漏采
            send_key(VK_ESCAPE, up=False)
            time.sleep(0.12)
            send_key(VK_ESCAPE, up=True)
            time.sleep(max(0, (case.hold_ms - case.escape_after_ms - 120) / 1000))
        else:
            time.sleep(case.hold_ms / 1000)
        send_key(case.vk, up=True)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def probe() -> bool:
    """钩子能不能收到自注入事件（判据：F13 按下/松开 → 事件数 +2）。"""
    before = hotkey.event_count()
    send_key(VK_F13, up=False)
    send_key(VK_F13, up=True)
    time.sleep(0.3)
    return hotkey.event_count() > before


def run() -> int:
    if sys.platform != "win32":
        print("仅 Windows 可用")
        return 2

    print(f"[env] {winenv.describe()}")
    if winenv.is_low_integrity():
        print("[env] 进程是 Low 完整性 → 钩子收不到按键，自测无法进行（G21）。")
        print("[env] 用目录外的解释器重跑：")
        print("[env]   uv venv %TEMP%\\vi-venv --python 3.11")
        print("[env]   set PYTHONPATH=.venv\\Lib\\site-packages")
        print("[env]   %TEMP%\\vi-venv\\Scripts\\python.exe ptt_selftest.py")
        return 2

    hotkey.ensure_hook()
    if hotkey.input_backend() != "hook":
        print(f"[hook] 采集方式={hotkey.input_backend()}（钩子不可用，自测无意义）")
        return 2
    if not probe():
        print("[hook] 自注入探针失败：钩子连自己发的事件都收不到。")
        print("[hook] 先跑 `python key_diag.py` 看第 2/3 节（钩子两种线程模型）。")
        return 2
    print("[hook] 采集方式=hook，自注入探针 PASS")

    failures = 0
    for case in CASES:
        _press_in_background(case)
        res = hotkey.record_ptt(
            "ctrl", min_hold_ms=250, opener=FakeStream, max_seconds=5.0,
        )
        measured = res.seconds * 1000
        problems: list[str] = []
        if res.cancelled != case.expect_cancelled:
            problems.append(f"cancelled={res.cancelled} 期望 {case.expect_cancelled}")
        if res.too_short != case.expect_too_short:
            problems.append(f"too_short={res.too_short} 期望 {case.expect_too_short}")
        if not case.expect_cancelled and res.ok != case.expect_ok:
            problems.append(f"ok={res.ok} 期望 {case.expect_ok}")
        if case.expect_cancelled and res.cancelled and res.ok:
            problems.append("已取消却判 ok")
        # 松开检测：实测按住时长应贴近目标（容差 150ms，含调度与注入开销）
        if case.escape_after_ms == 0 and abs(measured - case.hold_ms) > 150:
            problems.append(f"实测 {measured:.0f}ms 与目标 {case.hold_ms}ms 相差过大")

        status = "PASS" if not problems else "FAIL"
        if problems:
            failures += 1
        print(f"[{status}] {case.name} → 实测 {measured:.0f}ms"
              + (f"；{'; '.join(problems)}" if problems else ""))

    print()
    print(f"结果：{len(CASES) - failures}/{len(CASES)} 通过")
    print("说明：物理按键未参与本次自测（自注入与物理键走同一钩子回调）。")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
