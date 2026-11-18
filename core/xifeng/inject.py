"""光标注入：Windows SendInput + KEYEVENTF_UNICODE，剪贴板兜底。

契约与 spec.md §4.4 一致：
    inject(text, snap, cfg) -> InjectOutcome
    inject_ex(text, snap, cfg) -> InjectResult（带实际用的策略，写 Trace 用）

为什么默认用 UNICODE 而不是剪贴板：
- 中文不需要切换输入法，不受输入法状态影响
- 天然落在当前 caret，不覆盖用户剪贴板
代价：部分应用（提权窗口、DirectInput 游戏、少数终端）会静默吞掉按键 —— 所以
注入前必须校验焦点与提权状态，失败要显式返回，禁止"看起来成功了"。

剪贴板兜底（`strategy="clipboard"` / `"auto"`）：
- 保存**整个剪贴板**（能复制的 HGLOBAL 型格式全部快照）→ 写入文本 → Ctrl+V → 恢复
- 粘贴后如果发现剪贴板已被别的程序改写，就放弃恢复，绝不覆盖用户新复制的内容
- `auto`：先发 Unicode；只有焦点控件**可被探测**（Edit/RichEdit/Scintilla）且文本长度
  确实没变时，才改走剪贴板。探测不到时保持 Unicode —— 盲目兜底会把同一句话打两遍

纯逻辑部分（build_events / definitely_unchanged）不依赖 Win32，可在任何平台单测。
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

IS_WINDOWS = sys.platform == "win32"

VK_RETURN = 0x0D
VK_MENU = 0x12
VK_CONTROL = 0x11
VK_V = 0x56
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

WM_GETTEXTLENGTH = 0x000E     # WinForms 的 Edit 不认 EM_GETTEXTLENGTHEX，统一用这个
WM_GETTEXT = 0x000D
SMTO_ABORTIFHUNG = 0x0002

CHUNK_UNITS = 32       # 每批 32 个 UTF-16 码元（spec §4.4）
CHUNK_DELAY_MS = 5

# 注入事件的签名，写进 KEYBDINPUT.dwExtraInfo。
# 用途：低级键盘钩子能从 KBDLLHOOKSTRUCT.dwExtraInfo 读回它，从而精确忽略"自己打的字"，
# 而不是一律忽略 LLKHF_INJECTED —— 键盘重映射软件（PowerToys / 厂商驱动）产生的
# 输入同样是 injected，一律忽略会导致 PTT 在那些机器上完全失灵（G20）。
# 'VIME' 的 ASCII 值，别改，改了要和 hotkey.py 同步。
EXTRA_INFO_MAGIC = 0x56494D45


class InjectOutcome(str, Enum):
    INJECTED = "Injected"
    FOCUS_LOST = "FocusLost"
    ELEVATED_TARGET = "ElevatedTarget"
    FAILED = "Failed"
    NO_TARGET = "NoTarget"


Event = tuple[Literal["text"], int] | tuple[Literal["key"], int]


@dataclass(frozen=True)
class FocusSnapshot:
    hwnd: int
    pid: int
    class_name: str
    elevated: bool | None      # None = 读不到令牌（受保护进程），见 docs/05-gotchas.md G13
    is_console: bool


@dataclass(frozen=True)
class InjectConfig:
    strategy: str = "auto"           # unicode | clipboard | auto
    restore_focus: bool = False      # 焦点丢失时是否用 ALT 技巧抢回
    chunk_units: int = CHUNK_UNITS
    chunk_delay_ms: int = CHUNK_DELAY_MS
    verify_focus: bool = True
    # 粘完多久再恢复剪贴板。G3：恢复太早，慢应用会把"原内容"粘出来。
    clipboard_restore_ms: int = 500


@dataclass(frozen=True)
class InjectResult:
    outcome: "InjectOutcome"
    strategy: str = "unicode"        # 实际使用的策略：unicode | clipboard | none


@dataclass
class ClipboardSnapshot:
    """剪贴板快照。只保存能用 HGLOBAL 复制的格式（文本/HTML/DIB/私有格式）。"""

    formats: dict[int, bytes] = field(default_factory=dict)
    skipped: list[int] = field(default_factory=list)   # CF_BITMAP 等句柄型格式，无法复制


@dataclass(frozen=True)
class FocusText:
    """焦点控件文本快照。text=None 表示太长只读了长度。"""

    length: int
    text: str | None = None


# 能被 WM_GETTEXTLENGTH 读出长度的控件。WinForms 的 Edit 类名是
# `WindowsForms10.EDIT.app.0.xxx`，不是裸 "Edit"，只做等值匹配会永远探测不到。
EDIT_CLASSES = {"Edit", "RICHEDIT", "RichEdit20W", "RichEdit50W", "Scintilla"}
EDIT_CLASS_PREFIXES = ("WindowsForms10.EDIT", "WindowsForms10.RichEdit")


def is_edit_class(class_name: str) -> bool:
    return class_name in EDIT_CLASSES or class_name.startswith(EDIT_CLASS_PREFIXES)


# ---------------------------------------------------------------- 纯逻辑


def build_events(text: str) -> list[Event]:
    """把文本转成事件序列。

    - 按 UTF-16 码元发送：emoji（😀 = 2 码元）用 chars() 拆会乱码（G2）
    - 换行 MUST 用 VK_RETURN，MUST NOT 发 Unicode '\\n'（部分应用不认）
    - '\\r\\n' 归一成单个换行，避免敲出两个回车
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    events: list[Event] = []
    units = memoryview(normalized.encode("utf-16-le", "surrogatepass")).cast("H")
    for unit in units:
        code = int(unit)
        if code == 0x0A:
            events.append(("key", VK_RETURN))
        elif code == 0x09:
            events.append(("key", 0x09))  # VK_TAB
        else:
            events.append(("text", code))
    return events


def chunked(events: list[Event], size: int) -> list[list[Event]]:
    if size <= 0:
        return [events] if events else []
    return [events[i : i + size] for i in range(0, len(events), size)]


# ---------------------------------------------------------------- Win32

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_QUERY_INFORMATION = 0x0400
    TOKEN_QUERY = 0x0008
    TokenElevation = 20

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL

    # 剪贴板
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.EnumClipboardFormats.argtypes = (wintypes.UINT,)
    user32.EnumClipboardFormats.restype = wintypes.UINT
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)

    # 焦点控件文本长度探测（auto 兜底的判据）
    user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.POINTER(GUITHREADINFO))
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    )
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM

    CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty"}


def _elevation(pid: int) -> bool | None:
    """查询目标进程是否提权。

    返回 None 表示**查不到**（OpenProcess 被拒 = 目标受保护或更高完整性级别）。
    None 必须按"有风险"处理：把 None 当 False 会让提权窗口静默吞键（G7）。
    """
    # OpenProcessToken 需要 PROCESS_QUERY_INFORMATION；只用 LIMITED 会拿到句柄但查不了令牌
    access = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            info = TOKEN_ELEVATION()
            size = wintypes.DWORD()
            ok = advapi32.GetTokenInformation(
                token, TokenElevation, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(size)
            )
            if not ok:
                return None
            return bool(info.TokenIsElevated)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def self_elevated() -> bool:
    if not IS_WINDOWS:
        return False
    return _elevation(kernel32.GetCurrentProcessId()) is True


def snapshot_focus() -> FocusSnapshot | None:
    if not IS_WINDOWS:
        return None
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return FocusSnapshot(
        hwnd=int(hwnd),
        pid=int(pid.value),
        class_name=buf.value,
        elevated=_elevation(int(pid.value)),
        is_console=buf.value in CONSOLE_CLASSES,
    )


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0, extra: int = EXTRA_INFO_MAGIC) -> "INPUT":
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=extra)
    return inp


def _send(events: list[Event]) -> int:
    """发送一批事件，返回实际发送的 INPUT 数量（0 表示被拦）。"""
    inputs: list[INPUT] = []
    for kind, value in events:
        if kind == "text":
            inputs.append(_key_input(scan=value, flags=KEYEVENTF_UNICODE))
            inputs.append(_key_input(scan=value, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        else:
            inputs.append(_key_input(vk=value))
            inputs.append(_key_input(vk=value, flags=KEYEVENTF_KEYUP))

    if not inputs:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    return int(user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)))


def _restore_focus(hwnd: int) -> bool:
    """ALT 技巧：直接 SetForegroundWindow 会随机失败（G6）。

    顺序必须是 ALT 按下 → SetForegroundWindow → ALT 抬起。
    先抬起 ALT 就等于没按，系统仍然拒绝把前台给后台进程。
    """
    if not IS_WINDOWS:
        return False

    alt_down = _key_input(vk=VK_MENU)
    alt_up = _key_input(vk=VK_MENU, flags=KEYEVENTF_KEYUP)

    down = (INPUT * 1)(alt_down)
    up = (INPUT * 1)(alt_up)
    user32.SendInput(1, down, ctypes.sizeof(INPUT))
    ok = bool(user32.SetForegroundWindow(hwnd))
    user32.SendInput(1, up, ctypes.sizeof(INPUT))
    return ok


# ---------------------------------------------------------------- 焦点控件探测

# 读文本的缓冲上限：超过就只比长度（大文档不值当为一次探测拷几 MB）
TEXT_PROBE_LIMIT = 4096


def _focused_edit() -> int | None:
    """前台窗口里正在输入的 Edit/RichEdit 子窗口；不是可读控件就返回 None。"""
    if not IS_WINDOWS:
        return None
    gti = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(0, ctypes.byref(gti)) or not gti.hwndFocus:
        return None
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(gti.hwndFocus, buf, 256)
    if not is_edit_class(buf.value):
        return None
    return int(gti.hwndFocus)


def _send_timeout(hwnd: int, msg: int, wparam: int, lparam: int = 0) -> int | None:
    out = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(
        hwnd, msg, wparam, wintypes.LPARAM(lparam), SMTO_ABORTIFHUNG, 50, ctypes.byref(out)
    )
    return int(out.value) if ok else None


def probe_focus_text(limit: int = TEXT_PROBE_LIMIT) -> "FocusText | None":
    """读焦点控件当前的文本快照；读不到返回 None（控制台/自绘/游戏）。"""
    try:
        hwnd = _focused_edit()
        if hwnd is None:
            return None
        length = _send_timeout(hwnd, WM_GETTEXTLENGTH, 0)
        if length is None:
            return None
        text = None
        if length <= limit:
            buf = ctypes.create_unicode_buffer(limit + 1)
            if _send_timeout(hwnd, WM_GETTEXT, limit + 1, ctypes.addressof(buf)) is None:
                return None
            text = buf.value
        return FocusText(length=length, text=text)
    except Exception:  # noqa: BLE001 - 探测失败只意味着"不走 auto 兜底"
        return None


def definitely_unchanged(before: "FocusText | None", after: "FocusText | None") -> bool:
    """两次快照能否断定"什么都没变"。

    能读到全文时比全文（粘贴替换等长选区也能识别出来）；太长只比长度。
    任一端读不到就返回 False —— 探测不到不能兜底，否则同一句话会打两遍。
    """
    if before is None or after is None:
        return False
    if before.text is not None and after.text is not None:
        return before.text == after.text
    return before.length == after.length



# ---------------------------------------------------------------- 剪贴板


def _utf16_bytes(text: str) -> bytes:
    return text.encode("utf-16-le", "surrogatepass") + b"\x00\x00"


class Clipboard:
    """剪贴板读写。每个方法失败都返回 None/False，绝不抛异常打断输入。

    单独成类是为了能替换成假实现做单测 —— 测试里绝不去碰用户真实剪贴板。
    """

    def open(self, retries: int = 5, delay_ms: int = 20) -> bool:
        for _ in range(max(1, retries)):
            if user32.OpenClipboard(None):
                return True
            time.sleep(delay_ms / 1000)
        return False

    def close(self) -> None:
        user32.CloseClipboard()

    def _read(self, fmt: int) -> bytes | None:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return None
        size = int(kernel32.GlobalSize(handle))
        if size <= 0:
            return None          # CF_BITMAP / CF_ENHMETAFILE 等句柄型格式，复制不了
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)

    def _set(self, fmt: int, data: bytes) -> bool:
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)
            return False
        return True          # 所有权已交给系统，不能再 Free

    def snapshot(self) -> ClipboardSnapshot | None:
        if not self.open():
            return None
        try:
            snap = ClipboardSnapshot()
            fmt = 0
            while True:
                fmt = int(user32.EnumClipboardFormats(fmt))
                if fmt == 0:
                    break
                data = self._read(fmt)
                if data is None:
                    snap.skipped.append(fmt)
                else:
                    snap.formats[fmt] = data
            return snap
        except Exception:  # noqa: BLE001
            return None
        finally:
            self.close()

    def text(self) -> str | None:
        if not IS_WINDOWS or not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        if not self.open():
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        except Exception:  # noqa: BLE001
            return None
        finally:
            self.close()

    def write(self, text: str) -> bool:
        if not IS_WINDOWS or not self.open():
            return False
        try:
            return bool(user32.EmptyClipboard()) and self._set(CF_UNICODETEXT, _utf16_bytes(text))
        except Exception:  # noqa: BLE001
            return False
        finally:
            self.close()

    def restore(self, snap: ClipboardSnapshot) -> bool:
        if not IS_WINDOWS or not self.open():
            return False
        try:
            if not user32.EmptyClipboard():
                return False
            ok = True
            for fmt, data in snap.formats.items():
                ok = self._set(fmt, data) and ok
            return ok
        except Exception:  # noqa: BLE001
            return False
        finally:
            self.close()


clipboard = Clipboard()


def _send_unicode_text(text: str, cfg: InjectConfig) -> bool:
    events = build_events(text)
    total = 0
    for batch in chunked(events, cfg.chunk_units):
        if _send(batch) == 0:
            return False
        total += len(batch)
        if cfg.chunk_delay_ms:
            time.sleep(cfg.chunk_delay_ms / 1000)
    return total == len(events)


def _send_paste() -> bool:
    """Ctrl+V。事件带 MAGIC，PTT 钩子不会把它当成用户按了 Ctrl（G20）。"""
    inputs = [
        _key_input(vk=VK_CONTROL),
        _key_input(vk=VK_V),
        _key_input(vk=VK_V, flags=KEYEVENTF_KEYUP),
        _key_input(vk=VK_CONTROL, flags=KEYEVENTF_KEYUP),
    ]
    arr = (INPUT * len(inputs))(*inputs)
    return int(user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))) == len(inputs)


def _restore_unless_replaced(snap: ClipboardSnapshot, pasted: str) -> bool:
    """粘贴后恢复快照；但若剪贴板已被别的程序改写，就不动它。"""
    current = clipboard.text()
    if current != pasted:
        print("[inject] 剪贴板已被其它程序改写，跳过恢复", flush=True)
        return False
    if not clipboard.restore(snap):
        print("[inject] ⚠ 剪贴板恢复失败（原内容可能已丢失）", flush=True)
        return False
    return True


def _wait_paste_landed(before: "FocusText | None", timeout_ms: int) -> "FocusText | None":
    """等 Ctrl+V 真正生效再恢复剪贴板。

    读得到文本时轮询"内容是否变了"（最多 600ms）；读不到就按固定延时。
    之后仍等满 `timeout_ms`（默认 500ms，G3），让目标应用把剪贴板读完 ——
    恢复太早会把**原剪贴板内容**粘出来，比粘不出来更难排查。
    """
    if before is None:
        # 读不到目标文本时只能等固定时长；至少留 30ms 让目标读走剪贴板（G3）
        if timeout_ms is not None:
            time.sleep(max(timeout_ms, 30) / 1000)
        return None
    start = time.perf_counter()
    deadline = start + max(timeout_ms, 600) / 1000
    now = probe_focus_text()
    while time.perf_counter() < deadline:
        if not definitely_unchanged(before, now):
            break
        time.sleep(0.03)
        now = probe_focus_text()
    remaining = timeout_ms / 1000 - (time.perf_counter() - start)
    if remaining > 0:
        time.sleep(remaining)
    return now


def _wait_for_change(before: "FocusText | None", timeout_ms: int = 120) -> "FocusText | None":
    """Unicode 发完后等一下再判定"没变化"。

    `SendInput` 只是把 `WM_CHAR` 投进目标线程队列，我们立刻用 `SendMessage` 去读时
    目标可能还没处理完 —— 实测会读到只打了两个字。只有连续观察到"没变化"才敢兜底，
    否则会把同一句话打两遍。成功路径上第一次探测就变了，不付这个延时。
    """
    if before is None:
        return None
    deadline = time.perf_counter() + timeout_ms / 1000
    now = probe_focus_text()
    while definitely_unchanged(before, now) and time.perf_counter() < deadline:
        time.sleep(0.03)
        now = probe_focus_text()
    return now


def _paste_via_clipboard(
    text: str,
    cfg: InjectConfig,
    before: "FocusText | None" = None,
) -> InjectResult:
    snap = clipboard.snapshot()
    if snap is None:
        print("[inject] 剪贴板被占用，无法兜底", flush=True)
        return InjectResult(InjectOutcome.FAILED, "clipboard")
    if snap.skipped:
        print(
            f"[inject] 剪贴板有 {len(snap.skipped)} 个格式无法快照（位图等），恢复时可能丢失",
            flush=True,
        )
    if not clipboard.write(text):
        return InjectResult(InjectOutcome.FAILED, "clipboard")

    # G3：`SetClipboardData` 之后必须留 ≥30ms 再发 Ctrl+V，
    # 否则慢应用可能读到**上一次**的剪贴板内容
    time.sleep(0.03)
    sent = _send_paste()
    landed = _wait_paste_landed(before, cfg.clipboard_restore_ms)
    _restore_unless_replaced(snap, text)

    if not sent:
        return InjectResult(InjectOutcome.FAILED, "clipboard")
    if definitely_unchanged(before, landed):
        # 可探测且确实没粘进去：不能报成功（G13 同类，禁止"看起来成功了"）
        print("[inject] Ctrl+V 已发送但焦点控件文本未变化", flush=True)
        return InjectResult(InjectOutcome.FAILED, "clipboard")
    return InjectResult(InjectOutcome.INJECTED, "clipboard")



# ---------------------------------------------------------------- 对外接口


def _focus_ok(current: FocusSnapshot, snap: FocusSnapshot | None, cfg: InjectConfig) -> bool:
    if not cfg.verify_focus or snap is None:
        return True
    if current.hwnd == snap.hwnd:
        return True
    return cfg.restore_focus and _restore_focus(snap.hwnd)


def inject_ex(
    text: str,
    snap: FocusSnapshot | None,
    cfg: InjectConfig | None = None,
) -> InjectResult:
    """把文本打到当前光标处，并告诉调用方实际用了哪种策略。不抛异常。"""
    cfg = cfg or InjectConfig()

    if not IS_WINDOWS:
        return InjectResult(InjectOutcome.FAILED, "none")
    if not text:
        return InjectResult(InjectOutcome.INJECTED, "none")

    current = snapshot_focus()
    if current is None:
        return InjectResult(InjectOutcome.NO_TARGET, "none")

    if not _focus_ok(current, snap, cfg):
        return InjectResult(InjectOutcome.FOCUS_LOST, "none")

    # 提权目标：SendInput 会被 UIPI 静默吞掉（G7），必须显式失败。
    # elevated=None 表示读不到令牌（受保护进程，如 Edge/Chrome 的 PPL）；
    # 这种情况下**不能**武断拒绝，否则浏览器输入框直接不可用。
    # 处理方式：按普通窗口尝试注入，失败由 SendInput 返回值暴露（见 G13）。
    if current.elevated is True and not self_elevated():
        return InjectResult(InjectOutcome.ELEVATED_TARGET, "none")

    strategy = (cfg.strategy or "unicode").strip().lower()
    if strategy == "clipboard":
        return _paste_via_clipboard(text, cfg, before=probe_focus_text())
    if strategy not in ("unicode", "auto"):
        # 配置错误必须显式失败，不能悄悄按 unicode 打进去
        print(f"[inject] 未知注入策略 {cfg.strategy!r}（unicode|clipboard|auto）", flush=True)
        return InjectResult(InjectOutcome.FAILED, "none")

    before = probe_focus_text() if strategy == "auto" else None
    ok = _send_unicode_text(text, cfg)
    result = InjectResult(InjectOutcome.INJECTED if ok else InjectOutcome.FAILED, "unicode")
    if strategy != "auto" or not ok:
        return result

    # auto：只有"探测得到、且内容确实没变"才兜底。探测不到就到此为止，
    # 否则会在控制台/游戏里把同一句话打两遍。
    after = _wait_for_change(before)
    if not definitely_unchanged(before, after):
        return result

    print("[inject] 焦点控件文本未变化 → 改走剪贴板兜底", flush=True)
    return _paste_via_clipboard(text, cfg, before=before)


def inject(text: str, snap: FocusSnapshot | None, cfg: InjectConfig | None = None) -> InjectOutcome:
    """兼容入口：只要结果，不要策略。"""
    return inject_ex(text, snap, cfg).outcome



def describe() -> str:
    if not IS_WINDOWS:
        return "inject: 非 Windows，注入不可用（返回 Failed）"
    s = snapshot_focus()
    if s is None:
        return "inject: 无可注入目标（前台窗口为空）"
    flag = {True: "提权", False: "普通", None: "未知/受保护"}[s.elevated]
    console = "，控制台类" if s.is_console else ""
    return f"inject: 目标 hwnd={s.hwnd} pid={s.pid} class={s.class_name}（{flag}{console}）"
