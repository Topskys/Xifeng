"""录音指示浮窗：PTT 按住期间显示"应用图标 + 实时音量动效"。

设计（2026-10-30 改版）：

- **左右布局**：左边应用图标（蓝底麦克风，与托盘图标同源），右侧占满剩余空间是
  声音动效（镜像音柱），说话时随实时音量跳动
- **毛玻璃**：`show()` 时截取窗口背后的屏幕 → 高斯模糊 → 按主题上色 → 圆角，
  作为画布底图（Tk 没有逐像素 alpha，圆角用 `-transparentcolor` 抠掉）。
  截图失败（无 Pillow / 远程桌面）就退回半透明纯色面板，绝不报错
- **主题跟随主界面**：颜色直接取 `ui.DARK` / `ui.LIGHT` 的设计令牌（与 Web GUI 同一套）

约束：

- 在 PTT 线程里创建/更新/隐藏 Tk（Windows 上允许，只要所有 Tk 调用留在同一线程）；
  窗口只建一次，两次按键之间 `withdraw()`（Tk 初始化 ~130ms，每按一次都重建会慢半拍）
- 音量来自 `LiveMic.last_chunk`：音频回调只存引用（零数学、零分配），
  RMS 由 tick() 在 PTT 线程算 —— 不违反"回调里只拷贝"的硬约束
- 任何失败都 MUST 静默降级为 no-op：浮窗坏了不能影响输入本身
"""

from __future__ import annotations

import ctypes
import math
import tkinter as tk
from collections import deque

import numpy as np

# 版式（px）：32px 高的细长条 —— 左边图标，右边整条音柱
W, H = 300, 32
PAD = 5
ICON = 22
WAVE_X = PAD + ICON + 10          # 动效区左边界
WAVE_W = W - WAVE_X - PAD - 2     # 动效区宽度
WAVE_CY = H / 2                   # 动效区垂直中心
BARS = 30
RADIUS = 10                       # 圆角半径（32px 高的条，10 刚好不显方）
# 抠掉圆角用的“魔术色”：窗口把这个颜色当透明（Tk 没有逐像素 alpha）
MAGIC = "#010203"
COLORS = {
    "red": {"icon": (239, 68, 68, 255), "icon_fallback": "#ef4444", "accent": "#22d3ee"},
    "cyan": {"icon": (34, 211, 238, 255), "icon_fallback": "#22d3ee", "accent": "#22d3ee"},
    "green": {"icon": (16, 185, 129, 255), "icon_fallback": "#10b981", "accent": "#34d399"},
    "white": {"icon": (250, 250, 250, 255), "icon_fallback": "#fafafa", "accent": "#e4e4e7"},
}
SHOW_DELAY_MS = 250
SPEECH_LEVEL = 0.15     # 按住 + 音量超过此阈值（检测到说话）才显示浮窗
SILENCE_GRACE_S = 2.0   # 一直没检测到说话，超这个时间也显示（兜底反馈）   # 按住满这么久才显示浮窗；组合键在此之前已中止，闪都不闪


class _Glass:
    """两套配色。面板/边框/音柱与主界面同源，图标蓝与托盘图标一致。"""

    def __init__(self, name: str = "dark") -> None:
        from .ui import DARK, LIGHT

        t = LIGHT if name == "light" else DARK
        self.name = t.name
        dark = t.name == "dark"
        self.panel = t.INPUT_BG if dark else "#f7f7f8"     # 兜底纯色（截图失败时）
        self.border = t.BORDER
        self.track = t.DISABLED_TRACK if dark else "#e4e4e7"
        self.accent = t.WAVE   # 音柱动效主色（电光青，主题令牌）
        self.tile = "#2563eb"          # 应用图标底色（托盘图标同色）
        # 毛玻璃上色层：深色面板压暗一点，浅色面板提亮一点（alpha 越低越透）
        self.tint = (17, 17, 19, 172) if dark else (250, 250, 250, 168)
        self.alpha = 0.93              # 只在没有毛玻璃底图时用（纯色兜底）


def envelope(i: int, n: int = BARS) -> float:
    """音柱的高度包络：中间高、两头低，看起来像一段波形而不是一堵墙。"""
    if n <= 1:
        return 1.0
    return 0.35 + 0.65 * math.sin(math.pi * i / (n - 1))


def bar_length(value: float, i: int, n: int = BARS) -> float:
    """单个音柱的半长（px）。`value` 0–1，最小 1.5px 保证静止时也有呼吸感。"""
    v = max(0.0, min(1.0, value))
    return 1.5 + (WAVE_CY - 4.0) * envelope(i, n) * v


def blend(color: str, other: str, t: float) -> str:
    """线性混合两个 `#rrggbb`（t=0 取 color，t=1 取 other）。"""
    t = max(0.0, min(1.0, t))
    a = [int(color[k : k + 2], 16) for k in (1, 3, 5)]
    b = [int(other[k : k + 2], 16) for k in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def _debug() -> bool:
    import os

    return os.environ.get("IME_DEBUG") == "1"


class _BitmapInfo(ctypes.Structure):
    """BITMAPINFOHEADER：`GetDIBits` 要的 32 位 BGRA 描述（高度取负 = 自上而下）。"""

    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]

    def __init__(self, w: int, h: int) -> None:
        super().__init__(ctypes.sizeof(_BitmapInfo), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)


def _grab_screen(x: int, y: int, w: int, h: int):
    """BitBlt 抓屏幕一块（约 15ms，比 `ImageGrab` 快 4 倍）。

    坐标是 **Tk 逻辑像素**：本进程是 DPI 不感知的，GDI 拿到的也是同一套虚拟化坐标，
    所以 1 图 px = 1 逻辑 px，不用算缩放。失败返回 None。
    """
    from ctypes import wintypes

    from PIL import Image

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.BitBlt.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
        ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        hdc = user32.GetDC(None)
        if not hdc:
            return None
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mem, bmp)
        try:
            if not gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020):  # SRCCOPY
                return None
            info = _BitmapInfo(w, h)
            buf = ctypes.create_string_buffer(w * h * 4)
            if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0):
                return None
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, hdc)
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    except Exception:  # noqa: BLE001 - 抓不到就当没有毛玻璃
        return None


def _backdrop(x: int, y: int, theme: _Glass, *, rounded: bool):
    """截取浮窗背后的屏幕 → 模糊 → 上色 → 圆角，做成毛玻璃底图。

    为什么不用 DWM 的 `ACCENT_ENABLE_ACRYLICBLURBEHIND`：亚克力只对"没被画上去的
    像素"生效，Tk 画布是不透明矩形，盖上去以后背景根本糊不起来（实测调用返回成功、
    视觉无变化）。截图 + 高斯模糊不依赖系统版本，肉眼就是毛玻璃。

    失败返回 None —— 调用方退回半透明纯色面板。
    """
    raw = _grab_screen(x, y, W, H)
    if raw is None:
        return None
    from PIL import Image, ImageDraw, ImageFilter

    ss = 4                                        # 超采样，圆角和描边不锯齿
    w, h = W * ss, H * ss
    img = raw.convert("RGBA").resize((w, h), Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(ss * 3.0))
    img = Image.alpha_composite(img, Image.new("RGBA", (w, h), theme.tint))
    ImageDraw.Draw(img).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=RADIUS * ss, outline=theme.border, width=ss,
    )
    img = img.resize((W, H), Image.LANCZOS)
    if rounded:
        # Tk 窗口只有"整像素透明"，把边缘 alpha 归零，否则圆角会留一圈脏边
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=RADIUS * ss, fill=255,
        )
        mask = mask.resize((W, H), Image.LANCZOS).point(lambda v: 255 if v >= 128 else 0)
        img.putalpha(mask)
    return img


class RecordingPopup:
    """生命周期：show() → tick()×N（PTT 循环里调） → close()；退出前 dispose()。

    `mic` 传 `hotkey.live_mic()` 的返回值（可为 None）；enabled=False 时全部 no-op。
    `theme` 取主界面设置（`dark` / `light`），每次按键前由调用方重新赋值。

    窗口**只建一次**并在两次按键之间隐藏：Tk 初始化约 130ms，每次按下都重建会让
    指示条明显慢半拍。`close()` 只 withdraw，`dispose()` 才销毁。
    """

    def __init__(
        self,
        mic: object | None = None,
        *,
        enabled: bool = True,
        theme: str = "dark",
        color: str = "red",
    ) -> None:
        self._mic = mic
        self._enabled = enabled
        self._color = color
        self._armed = 0.0
        self._speech_seen = False
        self._theme = theme or "dark"
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._glass: _Glass | None = None
        self._bg_item: int | None = None
        self._photo: object | None = None
        self._bars: list[int] = []
        self._icon: object | None = None
        self._rounded = False            # 圆角（依赖 -transparentcolor）
        self._visible = False
        self._hist: deque[float] = deque([0.0] * BARS, maxlen=BARS)
        self._last_draw = 0.0
        self._started = 0.0
        self._level = 0.0
        self._peak = 0.0

    @property
    def theme(self) -> str:
        return self._theme

    @theme.setter
    def theme(self, name: str) -> None:
        self._theme = name or "dark"

    def bind(self, mic: object | None) -> None:
        """麦克风在按下后才创建；浮窗显示前再绑定，才能读到实时音量。"""
        self._mic = mic

    # -- 生命周期

    def arm(self) -> None:
        """按下瞬间只武装不显示；tick() 里「按住满 250ms 且检测到说话声」才真正出现。
        组合键（Ctrl+C 之类）没有说话声，中止前浮窗从头到尾没出现过；
        一直静音则超 SILENCE_GRACE_S 兜底显示（保证有反馈）。"""
        import time

        self._armed = time.monotonic() if self._enabled else 0.0
        self._speech_seen = False

    def show(self) -> None:
        if not self._enabled or self._visible:
            return
        try:
            import time

            started = time.monotonic()
            theme = _Glass(self._theme)
            root = self._ensure_window()
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            x = (sw - W) // 2
            y = sh - H - 96                     # 屏幕底部往上一点，避开任务栏
            root.geometry(f"{W}x{H}+{x}+{y}")

            # 截图前必须藏起来，否则底图里会拍到上一次的浮窗
            root.withdraw()
            self._glass = theme
            backdrop = _backdrop(x, y, theme, rounded=self._rounded)
            self._paint(theme, backdrop)

            self._hist = deque([0.0] * BARS, maxlen=BARS)
            self._accent = COLORS.get(self._color, COLORS["red"])["accent"]
            self._last_draw = 0.0
            self._started = time.monotonic()
            root.deiconify()
            root.update()
            self._visible = True
            if _debug():
                ms = round((time.monotonic() - started) * 1000, 1)
                print(
                    f"[popup] {W}x{H} 底图={'毛玻璃' if backdrop is not None else '纯色'}"
                    f" 圆角={'on' if self._rounded else 'off'} {ms}ms",
                    flush=True,
                )
        except Exception:  # noqa: BLE001 - 浮窗失败不能影响输入
            self._disable()

    def tick(self) -> None:
        """PTT 轮询里调用（~10ms 一次）；内部节流到 ~33ms。"""
        if not self._enabled:
            return
        if not self._visible:
            import time

            if self._armed:
                elapsed = time.monotonic() - self._armed
                if self._read_level() >= SPEECH_LEVEL:
                    self._speech_seen = True
                if self._speech_seen or elapsed >= SILENCE_GRACE_S:
                    self._armed = 0.0
                    self.show()      # 按住 + 听到说话（或长时间静音兜底）才显示
            return
        if self._root is None:
            return
        try:
            import time

            now = time.monotonic()
            if now - self._last_draw < 0.033:
                return
            self._last_draw = now
            self._hist.append(self._read_level())
            self._draw_bars(now)
            self._root.update()
        except Exception:  # noqa: BLE001
            self._disable()

    def close(self) -> None:
        """松开按键：收起来但保留窗口，下次按下几乎立刻出现。"""
        self._armed = 0.0
        if not self._visible:
            return
        self._visible = False
        try:
            if self._root is not None:
                self._root.withdraw()
        except Exception:  # noqa: BLE001
            self._disable()

    def dispose(self) -> None:
        """真正销毁（进程退出 / 浮窗出错时）。"""
        root, self._root = self._root, None
        self._canvas = None
        self._glass = None
        self._bg_item = None
        self._photo = None
        self._bars = []
        self._icon = None
        self._visible = False
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

    # -- 内部

    def _disable(self) -> None:
        """出过一次错就彻底停用：浮窗坏了不能拖累输入本身。"""
        self._enabled = False
        self.dispose()

    def _ensure_window(self) -> tk.Tk:
        """建窗口 + 画布 + 音柱（只做一次，之后复用）。"""
        if self._root is not None:
            return self._root

        root = tk.Tk()
        root.withdraw()
        root.title("VoiceIME")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", MAGIC)   # 圆角以外的像素透出桌面
            self._rounded = True
        except Exception:  # noqa: BLE001 - 远程桌面/旧系统不支持就方角
            self._rounded = False

        canvas = tk.Canvas(root, width=W, height=H, bd=0, highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        self._root = root
        self._canvas = canvas
        self._icon = self._draw_icon(canvas)
        step = WAVE_W / BARS
        self._bars = [
            canvas.create_line(
                WAVE_X + step * (i + 0.5), WAVE_CY - 1.5,
                WAVE_X + step * (i + 0.5), WAVE_CY + 1.5,
                width=2, fill=_Glass(self._theme).track, capstyle=tk.ROUND,
            )
            for i in range(BARS)
        ]
        return root

    def _paint(self, theme: _Glass, backdrop: object | None) -> None:
        """套上底图 / 兜底纯色面板，并把窗口调成对应的透明策略。"""
        root, canvas = self._root, self._canvas
        if root is None or canvas is None:
            return
        bg = MAGIC if backdrop is not None else theme.panel
        root.configure(bg=bg)
        canvas.configure(
            bg=bg,
            highlightthickness=0 if backdrop is not None else 1,
            highlightbackground=theme.border,
        )
        try:
            # 有毛玻璃底图就不透明；兜底纯色面板才用整体透明度
            root.attributes("-alpha", 1.0 if backdrop is not None else theme.alpha)
        except Exception:  # noqa: BLE001
            pass

        if backdrop is None:
            if self._bg_item is not None:
                canvas.itemconfigure(self._bg_item, state="hidden")
            return

        from PIL import ImageTk

        self._photo = ImageTk.PhotoImage(backdrop, master=canvas)
        if self._bg_item is None:
            self._bg_item = canvas.create_image(0, 0, anchor="nw", image=self._photo)
            canvas.tag_lower(self._bg_item)     # 压到音柱/图标下面
        else:
            canvas.itemconfigure(self._bg_item, image=self._photo, state="normal")

    def _draw_icon(self, canvas: tk.Canvas) -> object | None:
        """录音图标：红底圆角方块 + 白色麦克风（录音红，区别于托盘的蓝）。"""
        try:
            from PIL import Image, ImageDraw, ImageTk

            scale = 4
            size = ICON * scale
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            r = size * 0.24
            c = COLORS.get(self._color, COLORS['red'])['icon']
            d.rounded_rectangle((1, 1, size - 2, size - 2), radius=r, fill=c)   # 音效颜色
            u = size / 64.0  # 以托盘图标的 64px 画布为基准缩放
            d.rounded_rectangle((25 * u, 13 * u, 39 * u, 38 * u), radius=7 * u, fill=(255, 255, 255, 255))
            d.arc((17 * u, 24 * u, 47 * u, 50 * u), start=0, end=180, fill=(255, 255, 255, 255), width=int(4 * u))
            d.line((32 * u, 48 * u, 32 * u, 55 * u), fill=(255, 255, 255, 255), width=int(4 * u))
            d.line((23 * u, 55 * u, 41 * u, 55 * u), fill=(255, 255, 255, 255), width=int(4 * u))
            photo = ImageTk.PhotoImage(
                img.resize((ICON, ICON), Image.LANCZOS), master=canvas
            )
            canvas.create_image(PAD, (H - ICON) // 2, anchor="nw", image=photo)
            return photo
        except Exception as exc:  # noqa: BLE001 - 没有 Pillow 就画个纯色块，别让浮窗挂掉
            if _debug():
                print(f"[popup] 图标绘制失败：{type(exc).__name__}: {exc}", flush=True)
            top = (H - ICON) // 2
            return canvas.create_rectangle(
                PAD, top, PAD + ICON, top + ICON, fill=COLORS.get(self._color, COLORS["red"])["icon_fallback"], outline="",
            )

    def _read_level(self) -> float:
        """取最近一块的 RMS 并归一化；带慢峰值衰减，柱子不乱跳。"""
        mic = self._mic
        chunk = getattr(mic, "last_chunk", None)
        if chunk is None or chunk.size == 0:
            return 0.0
        x = chunk.astype(np.float64)
        rms = float(np.sqrt(np.mean(x * x))) / 32768.0
        level = min(1.0, rms * 6.0)          # 正常说话 RMS ≈ 0.02–0.2，放大到可视区间
        self._level = level
        self._peak = max(level, self._peak * 0.85)
        return self._peak

    def _draw_bars(self, now: float) -> None:
        canvas, bars, theme = self._canvas, self._bars, self._glass
        if canvas is None or not bars or theme is None:
            return
        values = list(self._hist)
        # 说话时是真实音量；安静时给一点匀速呼吸，避免"卡住了"的错觉
        breath = 0.10 + 0.06 * math.sin((now - self._started) * 4.2)
        for i, item in enumerate(bars):
            env = envelope(i, len(bars))
            v = max(values[i], breath * env)
            half = bar_length(v, i, len(bars))
            cx = canvas.coords(item)[0]
            canvas.coords(item, cx, WAVE_CY - half, cx, WAVE_CY + half)
            t = min(1.0, v * env * 1.35)
            if t > 0.7:
                # 大声段：电光青 → 冰白，双段渐变比单色更有质感
                fill = blend(getattr(self, "_accent", theme.accent), "#cffafe", (t - 0.7) / 0.3)
            else:
                fill = blend(theme.track, getattr(self, "_accent", theme.accent), t / 0.7)
            canvas.itemconfigure(item, fill=fill)
