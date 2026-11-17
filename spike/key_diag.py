r"""按键链路诊断：一次跑完，定性"为什么读不到键"。

背景：本机 `GetAsyncKeyState` 对所有 VK 恒为 0，`WH_KEYBOARD_LL` 装得上但收不到事件
（见 docs/05-gotchas.md G19/G21）。自动化 shell 与用户终端都可能是受限令牌，
所以需要把"代码问题""安全软件拦截""环境隔离"分开。

实测结论：

- 2026-10-28：**Low 完整性(0x1000)** 下钩子装得上但**一个硬件事件都收不到**，
  而普通窗口照样能收到 `Control_R` —— Windows 对低级键盘钩子做完整性检查，对窗口消息不做。
- 2026-10-29：定位到 Low 的来源是**目录标签**（`D:\project\workspace` 带 Low 完整性标签，
  子目录继承），不是终端也不是输入法。进程完整性由 exe 所在位置决定，与 CWD 无关。

本脚本依次测：
1. 环境：会话号、完整性级别、前台归属
2. 方案 A：主线程 + PeekMessage 泵
3. 方案 B：守护线程 + GetMessage 泵
4. 方案 C：Tk 窗口收 `<Key>`（判据：进程到底能不能看到输入）
5. 方案 D：Tk 窗口里按住 Ctrl，量真实按住时长（验证误触门槛与松开延迟）

跑法（不需要麦克风、不加载模型）：

    uv run python key_diag.py
    uv run python key_diag.py --hook-seconds 6 --tk-seconds 8

若第 1 节显示 Low，换成目录外的解释器（进程完整性只看 exe，不看 CWD）：

    uv venv %TEMP%\vi-venv --python 3.11
    set PYTHONPATH=.venv\Lib\site-packages
    %TEMP%\vi-venv\Scripts\python.exe key_diag.py

第 1b 节是**自注入探针**：自己发 F13 看钩子回调是否被调用，不需要人按。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes

from xifeng import winenv

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
PM_NOREMOVE = 0x0000
PM_REMOVE = 0x0001
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10

# inject.EXTRA_INFO_MAGIC（'VIME'）：本程序自有注入的签名
EXTRA_INFO_MAGIC = 0x56494D45

# Tk 的 keysym → 键位名
KEYSYM = {
    "Control_L": "左 Ctrl", "Control_R": "右 Ctrl",
    "Shift_L": "左 Shift", "Shift_R": "右 Shift",
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = (
    ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT
)
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SendInput.argtypes = (wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

VK_NAMES = {
    0x1B: "Esc", 0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt", 0x20: "Space",
    0x0D: "Enter", 0x09: "Tab", 0x14: "CapsLock",
    0xA0: "左 Shift", 0xA1: "右 Shift", 0xA2: "左 Ctrl", 0xA3: "右 Ctrl",
}

# 各方案的结果：(事件总数, 其中注入的事件数)
STATS: dict[str, tuple[int, int]] = {}


def key_name(vk: int) -> str:
    return VK_NAMES.get(vk, f"VK 0x{vk:02X}")


# ---------------------------------------------------------------- 1. 环境

def report_env(emit=print) -> None:
    pid = int(kernel32.GetCurrentProcessId())
    fg = winenv.foreground_pid()
    emit(f"  PID={pid}  会话={winenv.session_id()}  完整性={winenv.integrity_level()}")
    if fg is None:
        emit("  前台窗口：读不到")
    else:
        emit(f"  前台窗口属于 PID={fg}（会话={winenv.session_id(fg)}）")
    emit(f"  python={sys.version.split()[0]}  platform={sys.platform}")
    if winenv.is_low_integrity():
        emit("  ⚠ Low 完整性：全局钩子不会收到硬件事件，GetAsyncKeyState 也恒为 0（G21）")


# ---------------------------------------------------------------- 1b. 自注入探针

def probe_self_inject(emit=print) -> bool:
    """自己发一个 F13，看钩子回调是否被调用 —— 不需要人按，也能判"链路死活"。

    F13 没有文本副作用，不会往任何窗口打字。事件不带 MAGIC，所以会命中钩子的正常路径。

    结论：
      收得到 → 钩子链路正常；物理键仍收不到就是完整性/安全软件在过滤物理输入
      收不到 → 钩子回调压根没被调用（Low 完整性 G21，或安装失败）
    """
    state: dict = {"total": 0, "proc": None, "hook": None, "ready": threading.Event()}
    tid = {"v": 0}

    def _cb(n_code, w_param, l_param):  # noqa: ANN001
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if info.flags & LLKHF_INJECTED and int(info.dwExtraInfo) != EXTRA_INFO_MAGIC:
                state["total"] += 1
        return user32.CallNextHookEx(0, n_code, w_param, l_param)

    def _run() -> None:
        state["proc"] = HOOKPROC(_cb)
        tid["v"] = int(kernel32.GetCurrentThreadId())
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        state["hook"] = user32.SetWindowsHookExW(WH_KEYBOARD_LL, state["proc"], None, 0)
        state["ready"].set()
        if not state["hook"]:
            return
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(state["hook"])

    th = threading.Thread(target=_run, name="probe-hook", daemon=True)
    th.start()
    state["ready"].wait(3.0)
    if not state["hook"]:
        emit(f"  钩子安装失败 err={ctypes.get_last_error()}")
        return False

    time.sleep(0.2)
    _send_key(0x7C)                       # F13 down
    time.sleep(0.05)
    _send_key(0x7C, up=True)              # F13 up
    time.sleep(0.3)
    user32.PostThreadMessageW(tid["v"], WM_QUIT, 0, 0)

    ok = state["total"] >= 2
    emit(f"  自发 F13 按下+松开 → 钩子收到 {state['total']} 个事件 → "
         f"{'PASS 钩子回调被调用' if ok else 'FAIL 回调没被调用'}")
    return ok


# ---------------------------------------------------------------- 2. 钩子：主线程泵

def hook_main_thread(seconds: float, emit=print) -> int:
    state = {"total": 0, "inj": 0, "magic": 0, "down": set(), "new": []}

    def _cb(n_code, w_param, l_param):  # noqa: ANN001
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)
            injected = bool(info.flags & LLKHF_INJECTED)
            if int(info.dwExtraInfo) == EXTRA_INFO_MAGIC:
                state["magic"] += 1
            else:
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    state["down"].add(vk)
                    state["new"].append(vk)
                else:
                    state["down"].discard(vk)
                state["total"] += 1
                if injected:
                    state["inj"] += 1
        return user32.CallNextHookEx(0, n_code, w_param, l_param)

    proc = HOOKPROC(_cb)          # 必须保持引用，否则被 GC
    msg = MSG()
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)   # 强制建队列
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, None, 0)
    if not hook:
        emit(f"  安装失败 err={ctypes.get_last_error()}")
        return 0
    emit(f"  已安装 hwnd=0x{int(hook):X}；请在 {seconds:.0f} 秒内按 Ctrl / a（可多次）")

    t0 = time.perf_counter()
    beat = 0
    while time.perf_counter() - t0 < seconds:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        seen_new, state["new"] = state["new"], []
        for vk in seen_new:
            emit(f"    检测到 {key_name(vk)}")
        elapsed = time.perf_counter() - t0
        if elapsed >= beat + 2.0:
            beat = int(elapsed)
            emit(f"    [{elapsed:.0f}s] 事件 {state['total']} 个（其中注入 {state['inj']} 个）")
        time.sleep(0.004)

    user32.UnhookWindowsHookEx(hook)
    emit(f"  结果：事件 {state['total']} 个（其中注入 {state['inj']} 个，"
         f"自己的 MAGIC 事件 {state['magic']} 个）")
    STATS["main"] = (state["total"], state["inj"])
    return state["total"]


# ---------------------------------------------------------------- 3. 钩子：守护线程泵

def hook_daemon_thread(seconds: float, emit=print) -> int:
    state = {"total": 0, "inj": 0, "new": [], "ready": threading.Event(), "hook": None, "proc": None}
    tid_box = {"tid": 0}

    def _cb(n_code, w_param, l_param):  # noqa: ANN001
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)
            if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                state["new"].append(vk)
            state["total"] += 1
            if info.flags & LLKHF_INJECTED:
                state["inj"] += 1
        return user32.CallNextHookEx(0, n_code, w_param, l_param)

    def _run() -> None:
        state["proc"] = HOOKPROC(_cb)
        tid_box["tid"] = int(kernel32.GetCurrentThreadId())
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        state["hook"] = user32.SetWindowsHookExW(WH_KEYBOARD_LL, state["proc"], None, 0)
        state["ready"].set()
        if not state["hook"]:
            return
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(state["hook"])

    th = threading.Thread(target=_run, name="hook-daemon", daemon=True)
    th.start()
    state["ready"].wait(3.0)
    if not state["hook"]:
        emit(f"  安装失败 err={ctypes.get_last_error()}")
        return 0
    emit(f"  已安装 tid={tid_box['tid']}；请在 {seconds:.0f} 秒内按 Ctrl / a（可多次）")

    t0 = time.perf_counter()
    beat = 0
    while time.perf_counter() - t0 < seconds:
        seen_new, state["new"] = state["new"], []
        for vk in seen_new:
            emit(f"    检测到 {key_name(vk)}")
        elapsed = time.perf_counter() - t0
        if elapsed >= beat + 2.0:
            beat = int(elapsed)
            emit(f"    [{elapsed:.0f}s] 事件 {state['total']} 个（其中注入 {state['inj']} 个）")
        time.sleep(0.02)

    user32.PostThreadMessageW(tid_box["tid"], WM_QUIT, 0, 0)
    emit(f"  结果：事件 {state['total']} 个（其中注入 {state['inj']} 个）")
    STATS["daemon"] = (state["total"], state["inj"])
    return state["total"]


# ---------------------------------------------------------------- 4. Tk 收键

class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        class _KI(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        _fields_ = [("ki", _KI), ("padding", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send_key(vk: int, up: bool = False) -> None:
    KEYEVENTF_KEYUP = 0x0002
    inp = _INPUT()
    inp.type = 1
    inp.ki.wVk = vk
    inp.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def tk_probe(seconds: float, emit=print) -> tuple[list[str], bool]:
    """Tk 窗口能否收到按键 + 注入的键能否到达。返回 (keysym 列表, 注入是否可见)。"""
    try:
        import tkinter as tk
    except Exception as exc:                      # noqa: BLE001
        emit(f"  Tk 不可用：{exc}")
        return [], False

    got: list[str] = []
    injected_seen = {"v": False}
    fg_box = {"v": False}

    root = tk.Tk()
    root.title("Xifeng 按键诊断")
    root.geometry("460x170+120+120")
    root.attributes("-topmost", True)
    label = tk.Label(
        root,
        text="① 点一下这个窗口\n② 然后按 Ctrl / 右 Ctrl / 右 Shift / 字母 a",
        font=("Microsoft YaHei UI", 12),
        justify="center",
    )
    label.pack(expand=True)

    def on_key(event):                            # noqa: ANN001
        got.append(event.keysym)
        label.config(text=f"已收到：{', '.join(got[-8:])}")

    root.bind_all("<KeyPress>", on_key)
    root.lift()
    root.focus_force()

    def check_foreground() -> None:
        fg_box["v"] = winenv.foreground_pid() == int(kernel32.GetCurrentProcessId())

    root.after(800, check_foreground)
    emit(f"  窗口已弹出；请在 {seconds:.0f} 秒内点它一下再按键")

    def inject_probe() -> None:
        before = len(got)
        _send_key(0x41)                            # 'A'
        root.after(60, lambda: _send_key(0x41, up=True))
        root.after(400, lambda: injected_seen.update(v=len(got) > before))

    root.after(2000, inject_probe)
    root.after(int(seconds * 1000), root.destroy)
    try:
        root.mainloop()
    except Exception as exc:                       # noqa: BLE001
        emit(f"  Tk 循环异常：{exc}")

    emit(f"  本进程窗口是否拿到前台：{fg_box['v']}")
    emit(f"  结果：Tk 收到 {len(got)} 个键" + (f" → {got}" if got else ""))
    emit(f"  注入键（SendInput 'A'）是否被窗口收到：{injected_seen['v']}")
    return got, injected_seen["v"]


# ---------------------------------------------------------------- 5. Tk 按住时长

def tk_hold(seconds: float, min_hold_ms: int = 250, emit=print) -> list[float]:
    """在 Tk 窗口里按住 Ctrl，量真实按住时长。

    为什么需要：本环境钩子不可用时，唯一能验证"按下→松开"时序语义的办法。
    `record_ptt` 用的 min_hold_ms / 松开延迟都可以在这里用真实手速校验。
    """
    try:
        import tkinter as tk
    except Exception as exc:                      # noqa: BLE001
        emit(f"  Tk 不可用：{exc}")
        return []

    holds: list[float] = []
    pressed_at: dict[str, float] = {}

    root = tk.Tk()
    root.title("按住时长测试")
    root.geometry("440x150+140+140")
    root.attributes("-topmost", True)
    label = tk.Label(
        root,
        text="点一下窗口，然后按住 Ctrl 约 1 秒再松开（连做 3 次）",
        font=("Microsoft YaHei UI", 12),
        wraplength=400,
        justify="center",
    )
    label.pack(expand=True)

    def on_down(event):                           # noqa: ANN001
        if event.keysym in KEYSYM:
            pressed_at.setdefault(event.keysym, time.perf_counter())

    def on_up(event):                             # noqa: ANN001
        t0 = pressed_at.pop(event.keysym, None)
        if t0 is None:
            return
        ms = (time.perf_counter() - t0) * 1000
        holds.append(ms)
        verdict = "通过" if ms >= min_hold_ms else "丢弃（误触）"
        label.config(text=f"{KEYSYM[event.keysym]} 按住 {ms:.0f}ms → {verdict}")
        emit(f"  {KEYSYM[event.keysym]} 按住 {ms:.0f}ms → {verdict}")

    root.bind_all("<KeyPress>", on_down)
    root.bind_all("<KeyRelease>", on_up)
    root.lift()
    root.focus_force()
    emit(f"  窗口已弹出；请在 {seconds:.0f} 秒内按住 Ctrl 1 秒左右再松开（做 2–3 次）")
    root.after(int(seconds * 1000), root.destroy)
    try:
        root.mainloop()
    except Exception as exc:                      # noqa: BLE001
        emit(f"  Tk 循环异常：{exc}")

    if not holds:
        emit("  没测到任何完整的按下-松开")
    else:
        emit(f"  结果：{len(holds)} 次，最短 {min(holds):.0f}ms 最长 {max(holds):.0f}ms")
    return holds


# ---------------------------------------------------------------- 汇总

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按键链路诊断")
    ap.add_argument("--hook-seconds", type=float, default=8.0, help="每个钩子方案测多久")
    ap.add_argument("--tk-seconds", type=float, default=10.0, help="Tk 收键测多久")
    ap.add_argument("--hold-seconds", type=float, default=10.0, help="按住时长测多久")
    ap.add_argument("--min-hold-ms", type=int, default=250, help="误触门槛，判读用")
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        print("仅 Windows 可用")
        return 2

    print("\n=== 1. 环境 ===")
    report_env()

    print("\n=== 1b. 自注入探针（不需要人按键） ===")
    probe_ok = probe_self_inject()

    print("\n=== 2. 钩子：主线程 + PeekMessage 泵 ===")
    a = hook_main_thread(args.hook_seconds)

    print("\n=== 3. 钩子：守护线程 + GetMessage 泵 ===")
    b = hook_daemon_thread(args.hook_seconds)

    print("\n=== 4. Tk 窗口收键（判据） ===")
    keys, injected_ok = tk_probe(args.tk_seconds)

    print("\n=== 5. Tk 窗口按住时长（时序语义） ===")
    holds = tk_hold(args.hold_seconds, args.min_hold_ms)

    print("\n=== 结论 ===")
    total = a + b
    injected = sum(v[1] for v in STATS.values())
    low = winenv.is_low_integrity()

    if total:
        print("  钩子可用。spike 已默认走钩子（`input_backend()` 返回 hook）。")
        if injected:
            print(f"  注意：{injected}/{total} 个事件来自注入（LLKHF_INJECTED）→")
            print("  键盘很可能经过重映射软件（PowerToys / 厂商驱动 / 宏工具）。")
            print("  spike 只忽略带 MAGIC 签名的自有注入，所以不影响使用（G20）。")
    elif low:
        print("  钩子 0 事件，且完整性=Low → 这是环境限制，不是代码问题（G21）。")
        print("  Windows 对低级键盘钩子做完整性检查：Low 进程收不到硬件输入事件；")
        print("  但普通窗口消息不受限（所以上面 Tk 能收到键）。")
        print("  原因：本目录（或其上级）带 Low 完整性标签，从这里启动的 exe 都拿到 Low 令牌。")
        print("  核对：icacls . | findstr /i \"Mandatory Label\"")
        print("  对策 A（推荐）：用目录外的解释器 + 一个新 venv（进程完整性只看 exe）：")
        print("      uv venv %TEMP%\\vi-venv --python 3.11")
        print("      set PYTHONPATH=.venv\\Lib\\site-packages")
        print("      %TEMP%\\vi-venv\\Scripts\\python.exe key_diag.py")
        print("      %TEMP%\\vi-venv\\Scripts\\python.exe ptt_selftest.py   # PTT 时序自测 4/4")
        print("  对策 B：把 spike\\ 复制到未打标的目录（如 %TEMP%\\vi-run）后重跑")
        print("  对策 C：管理员去掉标签，需要写 SACL 权限：")
        print("      icacls . /setintegritylevel (OI)(CI)Medium")
    elif probe_ok:
        print("  自注入探针 PASS，但物理按键 0 事件 → 钩子活着，输入被上游过滤。")
        print("  常见原因：安全软件/EDR 拦物理键、远程桌面或虚拟机隔离输入流。")
        print("  对策：临时退出输入防护软件重试；仍不行则改用 Raw Input + 隐藏窗口。")
    elif keys:
        print("  完整性正常但钩子 0 事件 → 全局钩子被安全软件/EDR 拦了。")
        print("  对策：以管理员身份运行本脚本重试；仍不行则改用窗口化 + Raw Input。")
    elif injected_ok:
        print("  只收到注入的键、收不到物理键 → 输入流被隔离（远程桌面/沙箱/虚拟机？）。")
    else:
        print("  三种方式全部收不到任何键 → 该进程看不到输入流。")
        print("  先看第 1 节的前台归属与完整性级别。")
    return 0 if (total or keys) else 1


if __name__ == "__main__":
    raise SystemExit(main())
