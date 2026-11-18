"""Windows 环境探测：完整性级别、会话号、前台进程。

为什么单独成模块：按键采集能不能工作，取决于进程的完整性级别（G21）。
Low 完整性下 `WH_KEYBOARD_LL` 装得上但收不到任何硬件事件，
`GetAsyncKeyState` 也恒为 0 —— 必须先知道自己在哪一档，否则会把环境限制当成代码 bug 排查半天。

正常桌面进程是 `Medium(0x2000)`；沙箱/受限令牌是 `Low(0x1000)`。
"""

from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"

LEVELS = {
    0x0000: "Untrusted",
    0x1000: "Low",
    0x2000: "Medium",
    0x2100: "Medium+",
    0x3000: "High",
    0x4000: "System",
}

if IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    )
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
    _advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    _advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    _advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    TOKEN_QUERY = 0x0008
    TokenIntegrityLevel = 25   # TOKEN_INFORMATION_CLASS
else:  # pragma: no cover - 非 Windows 只保证 import 成功
    pass


def _sid_rid() -> int | None:
    """进程令牌完整性级别的 RID（0x1000=Low，0x2000=Medium，0x3000=High）。"""
    if not IS_WINDOWS:
        return None
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        return None
    try:
        size = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(
            token, TokenIntegrityLevel, buf, size, ctypes.byref(size)
        ):
            return None
        # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; } }
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        count = int(_advapi32.GetSidSubAuthorityCount(sid)[0])
        return int(_advapi32.GetSidSubAuthority(sid, count - 1)[0])
    finally:
        _kernel32.CloseHandle(token)


def integrity_level() -> str:
    """`Medium(0x2000)` 这种可读形式；读不到时返回 `读不到`。"""
    rid = _sid_rid()
    if rid is None:
        return "读不到"
    return f"{LEVELS.get(rid, 'Unknown')}(0x{rid:04X})"


def is_low_integrity() -> bool:
    """Low/Untrusted：全局钩子与 `GetAsyncKeyState` 都不可用（G21）。"""
    rid = _sid_rid()
    return rid is not None and rid < 0x2000


def session_id(pid: int | None = None) -> int:
    if not IS_WINDOWS:
        return -1
    if pid is None:
        pid = int(_kernel32.GetCurrentProcessId())
    out = wintypes.DWORD()
    if _kernel32.ProcessIdToSessionId(pid, ctypes.byref(out)):
        return int(out.value)
    return -1


def foreground_pid() -> int | None:
    """前台窗口所属进程；读不到前台返回 None。"""
    if not IS_WINDOWS:
        return None
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def describe() -> str:
    """一行诊断摘要，给 CLI 与日志用。"""
    if not IS_WINDOWS:
        return f"非 Windows（platform={sys.platform}）"
    return f"会话={session_id()} 完整性={integrity_level()}"
