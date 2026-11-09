"""按住说话（PTT）：全局按键采集 + 录音。

为什么不用 `RegisterHotKey`：它只报"按下"，拿不到"松开"，而 PTT 必须要 KeyUp。

采集方式两级：
1. `WH_KEYBOARD_LL` 低级键盘钩子（默认）—— 装在有消息泵的守护线程上，
   事件驱动，松开即时感知。本机实测 `GetAsyncKeyState` 读不到按键（G19），所以它是主力。
2. `GetAsyncKeyState` 轮询（兜底）—— 钩子装不上时自动退回，10ms 周期，只观察不吞键。

钩子会看到所有按键，但代码只把 vkCode 与白名单比对，不记录、不落盘、不外发。

键位约定（默认 Ctrl 长按）：
- `ctrl`  左/右 Ctrl 任意一个（默认）
- `lctrl` / `rctrl`  单独一侧
- `rshift` / `lshift` / `shift`  备用

风险：Ctrl 是高频修饰键，`Ctrl+C` 这类组合会触发一次极短录音。
对策是 `min_hold_ms`（默认 250ms）：按住时间不足直接丢弃，不进入 ASR。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .inject import EXTRA_INFO_MAGIC

IS_WINDOWS = sys.platform == "win32"

VK_ESCAPE = 0x1B
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3

KEYS: dict[str, tuple[int, ...]] = {
    "ctrl": (VK_LCONTROL, VK_RCONTROL),
    "lctrl": (VK_LCONTROL,),
    "rctrl": (VK_RCONTROL,),
    "shift": (VK_LSHIFT, VK_RSHIFT),
    "lshift": (VK_LSHIFT,),
    "rshift": (VK_RSHIFT,),
}

DEFAULT_KEY = "ctrl"
DEFAULT_MIN_HOLD_MS = 250
DEFAULT_MAX_SECONDS = 60.0
DEFAULT_POLL_MS = 10
DEFAULT_DRAIN_MS = 120     # 松手后继续收流的时长，捞回宿主缓冲里的尾音（G23）
DEFAULT_PRE_MS = 300       # 常驻流的前置缓冲：抵消开流静默期 + 抓按键前的话头（G23）

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 必须用 c_short：按键按下时返回负数，用 c_long 会在 64 位上出错
    _user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    _user32.GetAsyncKeyState.restype = ctypes.c_short

WH_KEYBOARD_LL = 13
PM_NOREMOVE = 0x0000
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10


@dataclass
class PttResult:
    pcm: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int16))
    seconds: float = 0.0
    cancelled: bool = False       # 录音中被 Esc 取消
    too_short: bool = False       # 按住时间 < min_hold_ms，按误触处理
    combo: bool = False           # 按住期间出现其它按键（Ctrl+C 之类），整段丢弃
    truncated: bool = False       # 超过 max_seconds 被截断
    stopped: bool = False         # 等待按键时收到退出信号（托盘「退出」）
    error: str = ""               # 麦克风打不开等硬错误（不静默吞掉）

    @property
    def ok(self) -> bool:
        return (
            not (self.cancelled or self.combo or self.too_short or self.stopped or self.error)
            and self.pcm.size > 0
        )


VK_NAMES: dict[int, str] = {
    VK_ESCAPE: "Esc",
    VK_LSHIFT: "左 Shift",
    VK_RSHIFT: "右 Shift",
    VK_LCONTROL: "左 Ctrl",
    VK_RCONTROL: "右 Ctrl",
    0x10: "Shift(通用)",
    0x11: "Ctrl(通用)",
    0x12: "Alt(通用)",
    0x20: "Space",
    0x0D: "Enter",
    0x09: "Tab",
    0x14: "CapsLock",
}


def key_name(vk: int) -> str:
    return VK_NAMES.get(vk, f"VK 0x{vk:02X}")


# ---------------------------------------------------------------- 低级键盘钩子


class LowLevelHook:
    """`WH_KEYBOARD_LL` 钩子：拿真实 KeyDown/KeyUp。

    为什么需要它：本机实测 `GetAsyncKeyState` 读不到任何按键（G18/G19），
    而 PTT 必须要可靠的"按下/松开"。

    线程模型：钩子必须装在**有消息泵**的线程上，所以单开一个守护线程跑
    `GetMessageW` 循环；回调只往 `_down` 集合里增删，不做任何耗时操作。
    回调跑在钩子线程上下文里，因此不涉及跨进程 DLL。

    隐私：钩子会看到**所有**按键，但代码只把 vkCode 与白名单比对，
    不记录、不落盘、不外发。这与最终 Rust 版 rdev 的信任模型一致。

    只忽略带 `EXTRA_INFO_MAGIC` 的注入事件（即本程序自己打的字），
    其它 injected 事件照收 —— 键盘重映射软件产生的真实按键也是 injected（G20）。
    """

    def __init__(self, allow_injected: bool = True) -> None:
        self._down: set[int] = set()
        self._new: list[int] = []
        self._total = 0
        self._injected = 0
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc = None
        self._hook = None
        self._tid = 0
        self._active = False
        self._error = 0
        self._allow_injected = allow_injected

    # -- 生命周期

    @property
    def active(self) -> bool:
        return self._active

    @property
    def error(self) -> int:
        return self._error

    def start(self, timeout: float = 2.0) -> bool:
        if not IS_WINDOWS:
            self._error = -1
            return False
        if self._thread is not None:
            return self._active
        self._thread = threading.Thread(target=self._run, name="ime-keyhook", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._active

    def stop(self) -> None:
        if self._tid and IS_WINDOWS:
            _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    # -- 查询

    def pressed(self, vks: tuple[int, ...]) -> bool:
        return any(vk in self._down for vk in vks)

    def down_keys(self) -> list[int]:
        return sorted(self._down)

    def event_count(self) -> int:
        """收到的按键事件总数（诊断用，能区分"没按键"与"收不到事件"）。"""
        return self._total

    def injected_count(self) -> int:
        """其中来自注入（LLKHF_INJECTED）的事件数。

        键盘重映射软件（PowerToys / 厂商驱动）产生的真实按键也是 injected，
        所以这个数字不为 0 不等于"没有物理按键"（G20）。
        """
        return self._injected

    def drain_presses(self) -> list[int]:
        """取走自上次调用以来新按下的 vk 列表（不漏极短的敲击）。"""
        out, self._new = self._new, []
        return out

    # -- 内部

    def _run(self) -> None:
        self._proc = HOOKPROC(self._callback)
        self._tid = _kernel32.GetCurrentThreadId()
        # 先强制创建本线程的消息队列，否则 LL 钩子可能一个事件都收不到
        _user32.PeekMessageW(ctypes.byref(MSG()), None, 0, 0, PM_NOREMOVE)
        # LL 钩子的回调在本进程内执行，hMod 必须传 NULL（传 EXE 句柄会 ERROR_MOD_NOT_FOUND）
        self._hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self._error = ctypes.get_last_error()
            self._ready.set()
            return

        self._active = True
        self._ready.set()
        msg = MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        _user32.UnhookWindowsHookEx(self._hook)
        self._active = False

    def _callback(self, n_code, w_param, l_param):  # noqa: ANN001
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            injected = bool(info.flags & LLKHF_INJECTED)
            # 只忽略**自己**注入的事件（带 MAGIC），而不是所有 injected 事件：
            # 键盘重映射软件产生的真实按键也是 injected，一律忽略会让 PTT 失灵（G20）
            ours = int(info.dwExtraInfo) == EXTRA_INFO_MAGIC
            if not ours and (self._allow_injected or not injected):
                vk = int(info.vkCode)
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self._down.add(vk)
                    self._new.append(vk)
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    self._down.discard(vk)
                self._total += 1
                if injected:
                    self._injected += 1
        return _user32.CallNextHookEx(0, n_code, w_param, l_param)


if IS_WINDOWS:
    from ctypes import wintypes

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

    _user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
    _user32.SetWindowsHookExW.restype = wintypes.HHOOK
    _user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    _user32.CallNextHookEx.restype = ctypes.c_ssize_t
    _user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
    _user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.PeekMessageW.argtypes = (
        ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT
    )
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.PostThreadMessageW.restype = wintypes.BOOL


# ---------------------------------------------------------------- 键状态

_hook: "LowLevelHook | None" = None
_hook_tried = False


def ensure_hook() -> bool:
    """确保低级钩子已就绪。失败则退回 `GetAsyncKeyState` 轮询。"""
    global _hook, _hook_tried
    if not IS_WINDOWS:
        return False
    if _hook is not None:
        return _hook.active
    if _hook_tried:
        return False
    _hook_tried = True
    candidate = LowLevelHook()
    if candidate.start():
        _hook = candidate
        return True
    return False


def input_backend() -> str:
    """当前按键采集方式：`hook` / `polling` / `none`。"""
    if _hook is not None and _hook.active:
        return "hook"
    if not IS_WINDOWS:
        return "none"
    return "polling"


def event_count() -> int:
    """钩子已收到的事件数（轮询模式恒 0）。诊断用。"""
    return _hook.event_count() if _hook is not None else 0


def injected_count() -> int:
    """其中来自注入的事件数（键盘重映射软件也算，见 G20）。"""
    return _hook.injected_count() if _hook is not None else 0


def pressed(vks: tuple[int, ...]) -> bool:
    """任意一个 vk 处于按下状态。"""
    if _hook is not None and _hook.active:
        return _hook.pressed(vks)
    if not IS_WINDOWS:
        return False
    return any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks)


def escape_pressed() -> bool:
    return pressed((VK_ESCAPE,))


def watch_keys(seconds: float = 15.0, poll_ms: int = 20,
               emit: Callable[[str], None] = print) -> list[int]:
    """诊断：打印这段时间内所有被按下的 VK，用来确认真实键位与采集方式。

    为什么要它：`GetAsyncKeyState` 只反映物理按键，`SendInput` 自测无效（G18）；
    而钩子在某些环境下装得上却收不到事件（G19）。所以必须让用户按一次、看报告。
    """
    if not IS_WINDOWS:
        emit("非 Windows，无法读取按键")
        return []

    ensure_hook()
    backend = input_backend()
    emit(f"采集方式={backend}；请在 {seconds:.0f} 秒内依次按下 Ctrl / 右 Ctrl / 右 Shift / 一个字母…")

    seen: list[int] = []
    t0 = time.perf_counter()
    next_beat = 2.0
    while time.perf_counter() - t0 < seconds:
        if backend == "hook" and _hook is not None:
            fresh = _hook.drain_presses()
        else:
            fresh = []
            for vk in range(0x01, 0xFF):
                if vk not in seen and pressed((vk,)):
                    fresh.append(vk)
        for vk in fresh:
            if vk not in seen:
                seen.append(vk)
                emit(f"  检测到 {key_name(vk)}（VK=0x{vk:02X}）")
        elapsed = time.perf_counter() - t0
        if elapsed >= next_beat:
            if backend == "hook" and _hook is not None:
                emit(f"  [{elapsed:.0f}s] 已收到按键事件 {_hook.event_count()} 个"
                     f"（其中注入 {_hook.injected_count()} 个），识别出 {len(seen)} 个不同键")
            else:
                emit(f"  [{elapsed:.0f}s] 已识别 {len(seen)} 个不同键（轮询模式无事件计数）")
            next_beat += 2.0
        time.sleep(poll_ms / 1000)

    if not seen:
        if backend == "hook":
            emit("  没识别到按键。若你确实按了，说明钩子收不到事件：")
            emit("    · 试过以管理员身份运行？某些安全软件会拦全局钩子")
            emit("    · 换一个键试（--key rshift）")
        else:
            emit("  没识别到按键，且钩子未安装成功 —— 该环境读不到键状态")
    return seen


def wait_for_press(key: str = DEFAULT_KEY, poll_ms: int = DEFAULT_POLL_MS,
                   check: Callable[[], bool] | None = None,
                   warn_after: float | None = None,
                   on_warn: Callable[[], None] | None = None,
                   stop: Callable[[], bool] | None = None) -> bool:
    """阻塞到按键按下，返回是否成功（False 表示收到退出信号）。

    `warn_after` + `on_warn`：等了这么久还没按到就调一次回调，避免"卡住没提示"。
    该回调返回后继续等（不中断），因为它只负责提示，不负责决策。

    `stop`：每轮检查一次退出信号。托盘「退出」要在**等按键**的时候也能生效，
    否则用户点了菜单主线程还卡在轮询里，进程不会退。
    """
    vks = KEYS[key]
    check = check or (lambda: pressed(vks))
    warned = False
    t0 = time.perf_counter()
    while not check():
        if stop is not None and stop():
            return False
        if warn_after is not None and not warned and on_warn is not None:
            if time.perf_counter() - t0 >= warn_after:
                warned = True
                on_warn()
        time.sleep(poll_ms / 1000)
    return True


def wait_for_double_tap(
    key: str = DEFAULT_KEY,
    poll_ms: int = DEFAULT_POLL_MS,
    check: Callable[[], bool] | None = None,
    other_keys: Callable[[], list[int]] | None = None,
    *,
    tap_ms: int = 300,
    gap_ms: int = 500,
    stop: Callable[[], bool] | None = None,
    clock: Callable[[], float] | None = None,
) -> str:
    """阻塞到「双击 key 且第二击保持按住」，返回 "ok"；收到退出信号返回 "stopped"。

    状态机：单击 tap1（按住 ≤ tap_ms 即松开）→ gap ≤ gap_ms 内再次按下 →
    第二击保持按住 → 录音开始。任何其它键落下都作废当前进度重来
    （单击组合键如 Ctrl+C 天然不会进入录音）；tap1 按住超 tap_ms 也作废。

    `clock` 可注入（单测不依赖真实时间）。
    """
    vks = KEYS[key]
    check = check or (lambda: pressed(vks))
    now = clock or time.perf_counter

    def others() -> bool:
        return any(vk not in vks and vk != VK_ESCAPE for vk in other_keys())

    while True:
        # -- 阶段 1：等 tap1 按下（now() 推进可注入时钟，测试依赖）
        while not check():
            now()
            if stop is not None and stop():
                return "stopped"
            if other_keys is not None:
                other_keys()                       # 丢弃（等待期组合无意义）
            time.sleep(poll_ms / 1000)
        t_down = now()
        held_long = False
        # -- 阶段 2：tap1 按住中 —— 必须在 tap_ms 内松开才是单击
        while check():
            if stop is not None and stop():
                return "stopped"
            if other_keys is not None and others():
                held_long = True                   # 组合键 → 作废
            if now() - t_down > tap_ms / 1000:
                held_long = True                   # 按住太久 → 不是单击
            time.sleep(poll_ms / 1000)
        if held_long:
            continue                               # 等松开已由上面循环完成，重新等 tap1

        # -- 阶段 3：gap 内等第二次按下
        deadline = now() + gap_ms / 1000
        while now() < deadline:
            if stop is not None and stop():
                return "stopped"
            if other_keys is not None and others():
                break                              # 组合 → 作废，重来
            if check():
                return "ok"                        # tap2 按下且保持 → 录音开始
            time.sleep(poll_ms / 1000)
        # gap 超时/组合 → 回到阶段 1


def describe_key(key: str) -> str:
    label = {
        "ctrl": "Ctrl（左或右）",
        "lctrl": "左 Ctrl",
        "rctrl": "右 Ctrl",
        "shift": "Shift（左或右）",
        "lshift": "左 Shift",
        "rshift": "右 Shift",
    }.get(key, key)
    return f"{label} 长按"


# ---------------------------------------------------------------- 录音


def _open_stream(sample_rate: int, callback: Callable) -> object:
    import sounddevice as sd

    # blocksize=0（交给宿主决定）+ latency="low"：
    # 本机实测首包 1024→376ms / 0→95ms，固定块会白白吃掉句首（G23）；
    # 松手后还要再收 drain_ms 才停，否则宿主缓冲里的尾音被丢掉（同样见 G23）。
    return sd.InputStream(
        samplerate=sample_rate, channels=1, dtype="int16",
        callback=callback, blocksize=0, latency="low",
    )


class LiveMic:
    """一次录音对应一条流：按下才开、松开就关。

    为什么不再常驻：常驻流会在不录音时一直占用麦克风（系统"正在使用麦克风"
    图标常亮），用户明确要求只在按住 Ctrl 时采集。代价是 `sd.InputStream.start()`
    到第一个回调之间有 100–400ms 静默期（G23）——按下热键后不要抢话，
    准备提示消失后再开口。

    仍保留 `pre_ms`：开流到 `begin()` 之间的头几块会被并入本次录音，
    把它们丢掉只会让句首更短。回调里只做 copy + append（不分配大对象、不加锁）；
    `begin()/end()` 由主线程调用。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        pre_ms: int = DEFAULT_PRE_MS,
        opener: Callable[[Callable], object] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self._cap = max(1, int(sample_rate * pre_ms / 1000))
        self._pre: deque[np.ndarray] = deque()
        self._pre_total = 0
        self._chunks: list[np.ndarray] | None = None
        self._stream: object | None = None
        # opener 契约：吃一个回调，返回带 start/stop/close 的对象（单测注入替身用）
        self._opener = opener or (lambda cb: _open_stream(sample_rate, cb))
        # 最近一块的引用（浮窗音量用）；回调里只存引用，RMS 由消费线程算
        self.last_chunk: np.ndarray | None = None

    # -- 生命周期

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        stream = self._opener(self._cb)
        stream.start()  # type: ignore[attr-defined]
        self._stream = stream

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()  # type: ignore[attr-defined]
            stream.close()  # type: ignore[attr-defined]

    # -- 录音切片

    def begin(self) -> None:
        """开始本次录音：把开流后已经到达的头几块并入，之后逐块全收。"""
        chunks = [np.concatenate(self._pre)] if self._pre else []
        self._pre.clear()
        self._pre_total = 0
        self._chunks = chunks

    def end(self) -> np.ndarray:
        """松开：取走本次录音。"""
        chunks, self._chunks = self._chunks or [], None
        return np.concatenate(chunks).astype(np.int16) if chunks else np.zeros(0, dtype=np.int16)

    def _cb(self, indata, frames_, time_, status) -> None:  # noqa: ANN001
        block = np.ascontiguousarray(indata).reshape(-1).astype(np.int16, copy=True)
        self.last_chunk = block
        if self._chunks is not None:
            self._chunks.append(block)
            return
        self._pre.append(block)
        self._pre_total += block.size
        # 空闲时只留最近 pre_ms，避免无限增长
        while len(self._pre) > 1 and self._pre_total - self._pre[0].size >= self._cap:
            self._pre_total -= self._pre.popleft().size


_live: LiveMic | None = None


def start_mic(sample_rate: int = 16000) -> bool:
    """启动常驻麦克风（可选，默认不用）。返回是否可用。

    只有显式 `--resident-mic` 才需要：常驻能消掉开流静默期，但会一直占用麦克风。
    """
    global _live
    if _live is not None and _live.running:
        return True
    try:
        mic = LiveMic(sample_rate)
        mic.start()
    except Exception:  # noqa: BLE001 - 没麦克风时退回"按下才开流"
        _live = None
        return False
    _live = mic
    return True


def stop_mic() -> None:
    global _live
    if _live is not None:
        _live.stop()
        _live = None


def mic_running() -> bool:
    return _live is not None and _live.running


def live_mic() -> LiveMic | None:
    """当前正在录音的流（浮窗取音量用）；未录音返回 None。"""
    return _live


def record_ptt(
    key: str = DEFAULT_KEY,
    sample_rate: int = 16000,
    min_hold_ms: int = DEFAULT_MIN_HOLD_MS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    poll_ms: int = DEFAULT_POLL_MS,
    drain_ms: int = DEFAULT_DRAIN_MS,
    pre_ms: int = DEFAULT_PRE_MS,
    *,
    opener: Callable[[Callable], object] | None = None,
    is_pressed: Callable[[], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_start: Callable[[], None] | None = None,
    on_wait: Callable[[], None] | None = None,
    on_poll: Callable[[], None] | None = None,
    other_keys: Callable[[], list[int]] | None = None,
    trigger: str = "double",
    tap_ms: int = 300,
    gap_ms: int = 500,
    clock: Callable[[], float] | None = None,
    warn_after: float | None = 5.0,
) -> PttResult:
    """按住 key 录音，松开结束。

    时序（默认双击触发）：单击（≤300ms）→ 500ms 内再次按下并**保持长按** → 打开麦克风 →
    打切片标记 → 每 10ms 查一次状态 → 按住期间其它键落下判定组合键并整段丢弃。
    按住期间若出现其它按键（Ctrl+C/V 之类），判定组合键并整段丢弃。
    松开/取消/超时 → 再收 drain_ms 取走宿主缓冲里的尾音 → 关流。
    也就是说，不按键时麦克风完全关闭，不会一直占用设备。

    双击触发下，单击组合键（Ctrl+C 等）天然不会进入录音。代价：`InputStream.start()`
    有 100–400ms 静默期（G23），第二次按下后先停顿半拍再说话。

    依赖全部可注入（opener / is_pressed / is_cancelled），便于单测不碰真麦克风与键盘。
    """
    if key not in KEYS:
        raise ValueError(f"未知按键: {key}（可选 {sorted(KEYS)}）")

    global _live
    vks = KEYS[key]
    if other_keys is None:
        # 默认从钩子取新按键（组合键中止）；测试注入替身
        other_keys = lambda: (_hook.drain_presses() if _hook is not None else [])
    if is_pressed is None:
        ensure_hook()          # 钩子优先；失败自动退回轮询
    check = is_pressed or (lambda: pressed(vks))
    cancel = is_cancelled or escape_pressed

    # 常驻流是显式可选项；默认每次录音新建一条流，结束后立刻关闭
    resident = _live if (_live is not None and _live.running) else None
    own_mic = resident is None
    mic = resident or LiveMic(sample_rate, pre_ms=pre_ms, opener=opener)
    result = PttResult()
    t0: float | None = None
    try:
        if trigger == "double":
            state = wait_for_double_tap(
                key, poll_ms, check, other_keys,
                tap_ms=tap_ms, gap_ms=gap_ms,
                stop=should_stop, clock=clock,
            )
        else:
            ok = wait_for_press(
                key, poll_ms, check,
                warn_after=warn_after if on_wait else None,
                on_warn=on_wait,
                stop=should_stop,
            )
            state = "ok" if ok else "stopped"
        if state != "ok":
            # 还没开始录就收到退出信号：不要开麦，直接收尾
            result.stopped = True
            return result

        if own_mic:
            try:
                mic.start()  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - 设备被独占/拔掉
                result.error = f"麦克风打开失败：{exc}"
                return result
            _live = mic  # 浮窗通过 live_mic() 读音量

        mic.begin()  # type: ignore[union-attr]
        t0 = time.perf_counter()
        if on_start is not None:
            on_start()
        combo_base = other_keys() if other_keys is not None else []
        while True:
            if cancel():
                result.cancelled = True
                break
            if other_keys is not None:
                others = [vk for vk in other_keys()
                          if vk not in vks and vk != VK_ESCAPE]
                if len(others) > len([vk for vk in combo_base
                                      if vk not in vks and vk != VK_ESCAPE]):
                    result.combo = True
                    break
            if not check():
                break
            if time.perf_counter() - t0 >= max_seconds:
                result.truncated = True
                break
            if on_poll is not None:
                on_poll()
            time.sleep(poll_ms / 1000)
    finally:
        # 先记按住时长（不含 drain），再补收尾音
        result.seconds = 0.0 if t0 is None else time.perf_counter() - t0
        # 取消时立刻收工；正常结束时多收一会儿，把宿主缓冲里的尾音捞回来（G23）
        if drain_ms > 0 and not result.cancelled and t0 is not None:
            time.sleep(drain_ms / 1000)
        pcm = mic.end()
        if own_mic:
            mic.stop()  # type: ignore[union-attr]
            if _live is mic:
                _live = None
        if pcm.size:
            result.pcm = pcm

    # 取消优先：Esc 取消常发生在开头几百毫秒内，若同时标 too_short，
    # 上游按"误触"统计会掩盖真实的取消行为（两者互斥，见 ptt_selftest 用例 4）。
    result.too_short = (not result.cancelled) and result.seconds * 1000 < min_hold_ms
    return result
