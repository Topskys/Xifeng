"""shadcn/ui（zinc）风格的 tkinter 控件，深 / 浅两套主题。

为什么不用 ttk：默认主题是 Windows 浅色 3D 控件，和设计稿（1px 边框、
圆角胶囊）差得远。这里全部自绘，颜色与圆角都对齐
`docs/reference/闲聊.md` 的设计令牌。

主题机制：
- 所有颜色常量（BG/CARD/…）是**模块级变量**，由 `set_theme()` 整套切换；
- 控件绘制时读这些全局变量，但**构造参数里烧进去的底色不会自己变**，
  所以切主题后必须整窗重建（见 gui.App.apply_theme）；
- 因此所有把颜色当**默认参数**的函数（label、Card…）MUST 在函数体内解析，
  禁止 `def f(bg=CARD)` —— 那样默认值会在 import 时定格，切主题后失效。

约定：
- 所有组件都接受 `bg` 参数（父容器底色），用来画"透明"背景；
- 画布尺寸单位是像素，字体用 point（96dpi 下 9pt ≈ 12px）；
- 只在主线程创建/更新。
"""

from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from tkinter import font as tkfont

# ---------------------------------------------------------------- 主题


@dataclass(frozen=True)
class Theme:
    """一套完整的设计令牌。新增颜色 MUST 加在这里，禁止在控件里写死 hex。"""

    name: str
    BG: str               # 窗口底色
    CARD: str             # 卡片底色（dark 主题下与 BG 同色，只靠边框区分）
    BORDER: str
    MUTED: str            # 开关轨道 / 分段控件底 / 进度条底
    MUTED_FG: str         # 次要文字
    SIDEBAR: str
    ACCENT: str           # 选中项底色（NavItem 激活、分页按钮）
    PRIMARY: str          # 主文字 / 主按钮底色
    PRIMARY_FG: str       # 主按钮文字 / 开关滑块
    SUCCESS: str
    DESTRUCTIVE: str
    DIM: str              # 最淡的文字（页脚、路径）
    PRIMARY_HOVER: str
    ACCENT_HOVER: str
    GHOST_HOVER: str
    HOVER: str            # outline 按钮悬停底色
    DANGER_BG: str
    DANGER_BORDER: str
    DANGER_BORDER_HOVER: str
    BADGE_DANGER_BG: str
    BADGE_DANGER_FG: str
    BADGE_SUCCESS_FG: str
    DISABLED_TRACK: str   # 禁用开关的轨道
    BAR: str              # 滚动条
    NAV_HOVER: str
    INPUT_BG: str         # 详情页只读文本框底色
    WAVE: str = ""                    # 浮窗音柱主色（电光青）
    CHART: dict[str, str] = field(default_factory=dict)


DARK = Theme(
    name="dark",
    BG="#0a0a0b", CARD="#0a0a0b", BORDER="#26262a",
    MUTED="#26262a", MUTED_FG="#a1a1aa", SIDEBAR="#18181b",
    ACCENT="#26262a", PRIMARY="#fafafa", PRIMARY_FG="#18181b",
    SUCCESS="#10b981", DESTRUCTIVE="#ef4444", DIM="#71717a",
    PRIMARY_HOVER="#e4e4e7", ACCENT_HOVER="#303036", GHOST_HOVER="#18181b",
    HOVER="#141416",
    DANGER_BG="#2a1416", DANGER_BORDER="#7f1d1d", DANGER_BORDER_HOVER="#9f2a2e",
    BADGE_DANGER_BG="#3f1d1d", BADGE_DANGER_FG="#fecaca", BADGE_SUCCESS_FG="#04231a",
    DISABLED_TRACK="#1c1c1f", BAR="#3f3f46", NAV_HOVER="#1f1f23",
    WAVE="#22d3ee",
    INPUT_BG="#111113",
    CHART={
        "other": "#3b82f6",     # 采集/开销
        "vad": "#a855f7",
        "asr": "#f59e0b",
        "fix": "#10b981",
        "inject": "#ec4899",
    },
)

LIGHT = Theme(
    name="light",
    BG="#ffffff", CARD="#ffffff", BORDER="#e4e4e7",
    MUTED="#e4e4e7", MUTED_FG="#71717a", SIDEBAR="#fafafa",
    ACCENT="#f4f4f5", PRIMARY="#18181b", PRIMARY_FG="#ffffff",
    SUCCESS="#059669", DESTRUCTIVE="#dc2626", DIM="#a1a1aa",
    PRIMARY_HOVER="#3f3f46", ACCENT_HOVER="#e4e4e7", GHOST_HOVER="#f4f4f5",
    HOVER="#f4f4f5",
    DANGER_BG="#fef2f2", DANGER_BORDER="#fecaca", DANGER_BORDER_HOVER="#fca5a5",
    BADGE_DANGER_BG="#fef2f2", BADGE_DANGER_FG="#b91c1c", BADGE_SUCCESS_FG="#04231a",
    DISABLED_TRACK="#e4e4e7", BAR="#d4d4d8", NAV_HOVER="#f4f4f5",
    WAVE="#0891b2",
    INPUT_BG="#fafafa",
    CHART={
        "other": "#2563eb",
        "vad": "#9333ea",
        "asr": "#d97706",
        "fix": "#059669",
        "inject": "#db2777",
    },
)

THEMES: dict[str, Theme] = {"dark": DARK, "light": LIGHT}

_current_theme = DARK

# 以下变量由 set_theme() 整套覆写；控件绘制时直接读它们
BG = DARK.BG
CARD = DARK.CARD
BORDER = DARK.BORDER
MUTED = DARK.MUTED
MUTED_FG = DARK.MUTED_FG
SIDEBAR = DARK.SIDEBAR
ACCENT = DARK.ACCENT
PRIMARY = DARK.PRIMARY
PRIMARY_FG = DARK.PRIMARY_FG
SUCCESS = DARK.SUCCESS
DESTRUCTIVE = DARK.DESTRUCTIVE
DIM = DARK.DIM
PRIMARY_HOVER = DARK.PRIMARY_HOVER
ACCENT_HOVER = DARK.ACCENT_HOVER
GHOST_HOVER = DARK.GHOST_HOVER
HOVER = DARK.HOVER
DANGER_BG = DARK.DANGER_BG
DANGER_BORDER = DARK.DANGER_BORDER
DANGER_BORDER_HOVER = DARK.DANGER_BORDER_HOVER
BADGE_DANGER_BG = DARK.BADGE_DANGER_BG
BADGE_DANGER_FG = DARK.BADGE_DANGER_FG
BADGE_SUCCESS_FG = DARK.BADGE_SUCCESS_FG
DISABLED_TRACK = DARK.DISABLED_TRACK
BAR = DARK.BAR
NAV_HOVER = DARK.NAV_HOVER
INPUT_BG = DARK.INPUT_BG
CHART = dict(DARK.CHART)
WAVE = DARK.WAVE

_THEME_FIELDS = tuple(f for f in Theme.__dataclass_fields__ if f != "name")


def set_theme(name: str) -> None:
    """整套切换设计令牌。只影响之后读取全局变量的绘制；已构造控件的底色
    不会变，切完 MUST 重建窗口。"""
    global _current_theme
    theme = THEMES.get(name, DARK)
    g = globals()
    for field_name in _THEME_FIELDS:
        value = getattr(theme, field_name)
        g[field_name] = dict(value) if field_name == "CHART" else value
    _current_theme = theme


def current_theme() -> str:
    return _current_theme.name

FAMILY = "Microsoft YaHei UI"
MONO = "Consolas"


def ui_font(size: int = 9, weight: str = "normal", family: str = FAMILY) -> tuple:
    return (family, size, weight)


# ---------------------------------------------------------------- 基础绘制


def rrect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float = 8,
    **kwargs: object,
) -> int:
    """圆角矩形：用采样点画多边形，避免 Tk 只有直角矩形。"""
    r = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    if r <= 0.5:
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)
    steps = 5
    points: list[float] = []
    for cx, cy, start in (
        (x2 - r, y1 + r, -90),
        (x2 - r, y2 - r, 0),
        (x1 + r, y2 - r, 90),
        (x1 + r, y1 + r, 180),
    ):
        for i in range(steps + 1):
            angle = math.radians(start + 90 * i / steps)
            points.extend((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return canvas.create_polygon(
        points, joinstyle=tk.ROUND, smooth=False, **kwargs  # type: ignore[arg-type]
    )


def label(
    parent: tk.Misc,
    text: str,
    *,
    size: int = 9,
    color: str | None = None,
    weight: str = "normal",
    bg: str | None = None,
    family: str = FAMILY,
    anchor: str = "w",
    justify: str = "left",
    **kwargs: object,
) -> tk.Label:
    return tk.Label(
        parent,
        text=text,
        bg=bg or CARD,
        fg=color or PRIMARY,
        font=ui_font(size, weight, family),
        anchor=anchor,
        justify=justify,
        **kwargs,  # type: ignore[arg-type]
    )


class HLine(tk.Frame):
    def __init__(self, parent: tk.Misc, *, bg: str | None = None) -> None:
        super().__init__(parent, bg=bg or BORDER, height=1)


class Dot(tk.Canvas):
    """小圆点（状态指示）。"""

    def __init__(
        self, parent: tk.Misc, color: str | None = None, size: int = 7, bg: str | None = None
    ) -> None:
        super().__init__(parent, width=size, height=size, bg=bg or CARD,
                         highlightthickness=0, bd=0)
        color = color or SUCCESS
        self.create_oval(0, 0, size - 1, size - 1, fill=color, outline=color)


# ---------------------------------------------------------------- 按钮 / 标签


class Button(tk.Canvas):
    """胶囊按钮。variant: primary / outline / ghost / accent / danger。"""

    HEIGHT = 30

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        variant: str = "outline",
        width: int | None = None,
        height: int = HEIGHT,
        padx: int = 14,
        size: int = 9,
        weight: str = "normal",
        radius: int = 6,
        enabled: bool = True,
        bg: str | None = None,
    ) -> None:
        bg = bg or BG
        self._font = tkfont.Font(font=ui_font(size, weight))
        w = width or self._font.measure(text) + padx * 2
        super().__init__(
            parent, width=w, height=height, bg=bg, highlightthickness=0, bd=0,
            takefocus=0,
        )
        self._text = text
        self._variant = variant
        self._radius = radius
        self._height = height
        self._width = w
        self._bg = bg
        self._command = command
        self._enabled = enabled
        self._hover = False
        if enabled and command is not None:
            self.configure(cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self._draw()

    # -- 状态

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text: str) -> None:
        self._text = text
        w = self._font.measure(text) + 28
        if w > self._width:
            self._width = w
            self.configure(width=w)
        self._draw()

    def set_variant(self, variant: str) -> None:
        self._variant = variant
        self._draw()

    def _palette(self) -> tuple[str, str, str]:
        if not self._enabled:
            return ("", BORDER, DIM)
        if self._variant == "primary":
            return (PRIMARY_HOVER if self._hover else PRIMARY, PRIMARY, PRIMARY_FG)
        if self._variant == "accent":
            return (ACCENT_HOVER if self._hover else ACCENT, ACCENT, PRIMARY)
        if self._variant == "danger":
            return (
                DANGER_BG if self._hover else "",
                DANGER_BORDER_HOVER if self._hover else DANGER_BORDER,
                DESTRUCTIVE,
            )
        if self._variant == "ghost":
            return (
                GHOST_HOVER if self._hover else "", "",
                PRIMARY if self._hover else MUTED_FG,
            )
        return (HOVER if self._hover else "", BORDER, PRIMARY)

    def _draw(self) -> None:
        self.delete("all")
        fill, outline, fg = self._palette()
        rrect(
            self, 1, 1, self._width - 1, self._height - 1, self._radius,
            fill=fill, outline=outline, width=1,
        )
        self.create_text(
            self._width / 2, self._height / 2 + 1, text=self._text, fill=fg,
            font=self._font, anchor="center",
        )

    # -- 事件

    def _click(self, _event: tk.Event) -> None:
        if self._enabled and self._command is not None:
            self._command()

    def _enter(self, _event: tk.Event) -> None:
        if self._enabled:
            self._hover = True
            self._draw()

    def _leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._draw()


class Badge(tk.Canvas):
    """胶囊标签。variant: muted / success / outline / danger / primary。"""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        *,
        variant: str = "muted",
        bg: str | None = None,
        size: int = 8,
        height: int = 19,
    ) -> None:
        bg = bg or CARD
        self._font = tkfont.Font(font=ui_font(size))
        pad = 9
        w = self._font.measure(text) + pad * 2
        super().__init__(parent, width=w, height=height, bg=bg, highlightthickness=0, bd=0)
        fill, outline, fg = {
            "muted": (MUTED, MUTED, MUTED_FG),
            "success": (SUCCESS, SUCCESS, BADGE_SUCCESS_FG),
            "outline": ("", BORDER, MUTED_FG),
            "outline-success": ("", SUCCESS, SUCCESS),
            "danger": (BADGE_DANGER_BG, DANGER_BORDER, BADGE_DANGER_FG),
            "primary": (PRIMARY, PRIMARY, PRIMARY_FG),
        }.get(variant, (MUTED, MUTED, MUTED_FG))
        rrect(
            self, 1, 1, w - 1, height - 1, height / 2,
            fill=fill, outline=outline, width=1,
        )
        self.create_text(
            w / 2, height / 2 + 1, text=text, fill=fg, font=self._font, anchor="center"
        )


class Switch(tk.Canvas):
    """开关：36×20 胶囊 + 圆形滑块。"""

    W, H = 36, 20

    def __init__(
        self,
        parent: tk.Misc,
        value: bool = False,
        command: Callable[[bool], None] | None = None,
        *,
        bg: str | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(parent, width=self.W, height=self.H, bg=bg or CARD,
                         highlightthickness=0, bd=0)
        self._value = bool(value)
        self._command = command
        self._enabled = enabled
        if enabled:
            self.configure(cursor="hand2")
        self.bind("<Button-1>", self._toggle)
        self._draw()

    def get(self) -> bool:
        return self._value

    def set(self, value: bool, *, notify: bool = False) -> None:
        self._value = bool(value)
        self._draw()
        if notify and self._command is not None:
            self._command(self._value)

    def _toggle(self, _event: tk.Event) -> None:
        if not self._enabled:
            return
        self._value = not self._value
        self._draw()
        if self._command is not None:
            self._command(self._value)

    def _draw(self) -> None:
        self.delete("all")
        track = PRIMARY if self._value and self._enabled else MUTED
        if not self._enabled:
            track = DISABLED_TRACK
        knob = PRIMARY_FG if self._value and self._enabled else (DIM if not self._enabled else MUTED_FG)
        rrect(self, 1, 1, self.W - 1, self.H - 1, 9, fill=track, outline=track, width=1)
        cx = 26 if self._value else 10
        self.create_oval(cx - 7, 3, cx + 7, 17, fill=knob, outline="")


class Segmented(tk.Canvas):
    """分段控件（Tabs 风格）：外层 #26262a 容器 + 活动项 #0a0a0b。"""

    HEIGHT = 28

    def __init__(
        self,
        parent: tk.Misc,
        options: Sequence[str | tuple[str, str]],
        value: str | None = None,
        command: Callable[[str], None] | None = None,
        *,
        bg: str | None = None,
        width: int | None = None,
        height: int = HEIGHT,
        size: int = 8,
    ) -> None:
        bg = bg or BG
        self._pairs: list[tuple[str, str]] = [
            (opt, opt) if isinstance(opt, str) else opt for opt in options
        ]
        self._font = tkfont.Font(font=ui_font(size))
        self._seg_w = max(self._font.measure(p[0]) for p in self._pairs) + 22
        total = width or self._seg_w * len(self._pairs) + 4
        self._width = total
        super().__init__(parent, width=total, height=height, bg=bg, highlightthickness=0, bd=0)
        self._height = height
        self._command = command
        self._value = value if value is not None else self._pairs[0][1]
        if command is not None:
            self.configure(cursor="hand2")
        self.bind("<Button-1>", self._click)
        self._draw()

    def get(self) -> str:
        return self._value

    def set(self, value: str, *, notify: bool = False) -> None:
        if value not in [p[1] for p in self._pairs]:
            return
        self._value = value
        self._draw()
        if notify and self._command is not None:
            self._command(value)

    def _index_at(self, x: float) -> int | None:
        inner = self._width - 4
        idx = int(max(0, x - 2) // max(1, inner / len(self._pairs)))
        return idx if 0 <= idx < len(self._pairs) else None

    def _click(self, event: tk.Event) -> None:
        idx = self._index_at(event.x)
        if idx is None or self._command is None:
            return
        value = self._pairs[idx][1]
        if value != self._value:
            self._value = value
            self._draw()
            self._command(value)

    def _draw(self) -> None:
        self.delete("all")
        rrect(self, 1, 1, self._width - 1, self._height - 1, 6, fill=MUTED, outline=MUTED)
        inner = (self._width - 4) / len(self._pairs)
        for i, (text, value) in enumerate(self._pairs):
            x1 = 2 + inner * i
            x2 = x1 + inner
            active = value == self._value
            if active:
                rrect(self, x1 + 1, 3, x2 - 1, self._height - 3, 4, fill=BG, outline=BG)
            self.create_text(
                (x1 + x2) / 2, self._height / 2 + 1, text=text,
                fill=PRIMARY if active else MUTED_FG, font=self._font, anchor="center",
            )


class Select(tk.Canvas):
    """下拉选择：边框框 + 右侧 ▾，菜单深色。"""

    def __init__(
        self,
        parent: tk.Misc,
        options: Sequence[str | tuple[str, str]],
        value: str | None = None,
        command: Callable[[str], None] | None = None,
        *,
        width: int = 170,
        height: int = 30,
        bg: str | None = None,
        size: int = 8,
    ) -> None:
        bg = bg or BG
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        self._pairs = [(o, o) if isinstance(o, str) else o for o in options]
        self._command = command
        self._value = value if value is not None else (self._pairs[0][1] if self._pairs else "")
        self._width, self._h, self._size = width, height, size
        self._menu = tk.Menu(
            self, tearoff=0, bg=SIDEBAR, fg=PRIMARY, activebackground=ACCENT,
            activeforeground=PRIMARY, bd=0, relief="flat", font=ui_font(size),
        )
        if command is not None:
            self.configure(cursor="hand2")
        self.bind("<Button-1>", self._popup)
        self._draw()

    def get(self) -> str:
        return self._value

    def set(self, value: str, *, notify: bool = False) -> None:
        if value not in [p[1] for p in self._pairs]:
            return
        self._value = value
        self._draw()
        if notify and self._command is not None:
            self._command(value)

    def _caption(self) -> str:
        for text, value in self._pairs:
            if value == self._value:
                return text
        return self._value

    def _popup(self, _event: tk.Event) -> None:
        self._menu.delete(0, "end")
        for text, value in self._pairs:
            self._menu.add_command(label=text, command=lambda v=value: self._choose(v))
        try:
            self._menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self._h + 2)
        finally:
            self._menu.grab_release()

    def _choose(self, value: str) -> None:
        changed = value != self._value
        self._value = value
        self._draw()
        if changed and self._command is not None:
            self._command(value)

    def _draw(self) -> None:
        self.delete("all")
        rrect(self, 1, 1, self._width - 1, self._h - 1, 5, fill="", outline=BORDER, width=1)
        self.create_text(
            12, self._h / 2 + 1, text=self._caption(), fill=PRIMARY,
            font=ui_font(self._size), anchor="w",
        )
        self.create_text(
            self._width - 14, self._h / 2, text="▾", fill=MUTED_FG,
            font=ui_font(self._size), anchor="center",
        )


class Slider(tk.Canvas):
    """滑杆：轨道 #26262a + 已填充白色 + 圆形把手。"""

    def __init__(
        self,
        parent: tk.Misc,
        minimum: int = 0,
        maximum: int = 100,
        value: int = 0,
        command: Callable[[int], None] | None = None,
        *,
        width: int = 170,
        bg: str | None = None,
        step: int = 1,
    ) -> None:
        bg = bg or CARD
        super().__init__(parent, width=width, height=24, bg=bg, highlightthickness=0, bd=0)
        self._min, self._max, self._step = minimum, maximum, max(1, step)
        self._value = self._clamp(value)
        self._width = width
        self._command = command
        self.configure(cursor="hand2")
        self.bind("<Button-1>", self._jump)
        self.bind("<B1-Motion>", self._jump)
        self._draw()

    def _clamp(self, value: int) -> int:
        stepped = round(value / self._step) * self._step
        return int(max(self._min, min(self._max, stepped)))

    def get(self) -> int:
        return self._value

    def set(self, value: int, *, notify: bool = False) -> None:
        new = self._clamp(value)
        if new == self._value:
            return
        self._value = new
        self._draw()
        if notify and self._command is not None:
            self._command(new)

    def _jump(self, event: tk.Event) -> None:
        ratio = min(1.0, max(0.0, (event.x - 8) / max(1, self._width - 16)))
        new = self._clamp(int(self._min + ratio * (self._max - self._min)))
        if new != self._value:
            self._value = new
            self._draw()
            if self._command is not None:
                self._command(new)

    def _draw(self) -> None:
        self.delete("all")
        y = 12
        x1, x2 = 8, self._width - 8
        rrect(self, x1, y - 2, x2, y + 2, 2, fill=MUTED, outline=MUTED)
        span = (self._value - self._min) / max(1, self._max - self._min)
        cx = x1 + span * (x2 - x1)
        if cx > x1 + 2:
            rrect(self, x1, y - 2, cx, y + 2, 2, fill=PRIMARY, outline=PRIMARY)
        self.create_oval(cx - 7, y - 7, cx + 7, y + 7, fill=PRIMARY, outline="")


# ---------------------------------------------------------------- 输入框


class Input(tk.Frame):
    """带 1px 边框的输入框（Tk Entry 无法直接画圆角边框）。

    占位符用覆盖在 Entry 上的 Label 显示，MUST NOT 写进 `textvariable`：
    外部代码（历史页搜索、术语表添加）直接读 var，写进去就会被当成用户输入。
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        width: int = 180,
        placeholder: str = "",
        bg: str | None = None,
        textvariable: tk.StringVar | None = None,
        show: str | None = None,
    ) -> None:
        bg = bg or BG
        super().__init__(
            parent, bg=bg, width=width, height=32,
            highlightbackground=BORDER, highlightthickness=1, bd=0,
        )
        self.pack_propagate(False)
        self._var = textvariable or tk.StringVar()
        self.entry = tk.Entry(
            self, textvariable=self._var, width=1, bg=bg, fg=PRIMARY,
            insertbackground=PRIMARY, relief="flat", bd=0, highlightthickness=0,
            font=ui_font(9), show=show or "",
        )
        self.entry.pack(fill="both", expand=True, padx=10, pady=6)
        self._placeholder: tk.Label | None = None
        if placeholder:
            self._placeholder = tk.Label(
                self, text=placeholder, bg=bg, fg=DIM, font=ui_font(9)
            )
            self._placeholder.bind("<Button-1>", lambda _e: self.entry.focus_set())
            self._var.trace_add("write", lambda *_: self._sync_placeholder())
            self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        if self._placeholder is None:
            return
        if self._var.get():
            self._placeholder.place_forget()
        else:
            self._placeholder.place(x=10, rely=0.5, anchor="w")

    def get(self) -> str:
        return self._var.get()

    def set(self, text: str) -> None:
        self._var.set(text)


# ---------------------------------------------------------------- 卡片 / 滚动


class Card(tk.Frame):
    """卡片：bg 与页面同色，只靠 1px 边框区分（shadcn dark 的做法）。"""

    def __init__(self, parent: tk.Misc, *, pad: int = 16, bg: str | None = None) -> None:
        bg = bg or CARD
        super().__init__(
            parent, bg=bg, highlightbackground=BORDER, highlightthickness=1, bd=0
        )
        self.body = tk.Frame(self, bg=bg)
        self.body.pack(fill="both", expand=True, padx=pad, pady=pad)
        self.pad = pad


class _SlimBar(tk.Canvas):
    """6px 细滚动条：原生 Scrollbar 是浅色 3D 的，和深色主题冲突。"""

    WIDTH = 6

    def __init__(self, parent: tk.Misc, target: tk.Canvas, *, bg: str | None = None) -> None:
        bg = bg or BG
        super().__init__(parent, width=self.WIDTH, bg=bg, highlightthickness=0, bd=0)
        self._target = target
        self._first = 0.0
        self._last = 1.0
        self._drag_y = 0.0
        self._drag_first = 0.0
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._move)

    def set(self, first: str | float, last: str | float) -> None:
        self._first, self._last = float(first), float(last)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        h = max(1, self.winfo_height())
        w = self.winfo_width() or self.WIDTH
        if self._last - self._first >= 0.999:
            return
        y1 = self._first * h
        y2 = self._last * h
        if y2 - y1 < 24:
            y1 = min(y1, h - 24)
            y2 = y1 + 24
        rrect(self, 1, y1, w - 1, y2, (w - 2) / 2, fill=BAR, outline="")

    def _press(self, event: tk.Event) -> None:
        self._drag_y = event.y
        self._drag_first = self._first

    def _move(self, event: tk.Event) -> None:
        h = max(1, self.winfo_height())
        span = max(0.0001, self._last - self._first)
        delta = (event.y - self._drag_y) / h
        self._target.yview_moveto(min(1.0 - span, max(0.0, self._drag_first + delta)))


class ScrollFrame(tk.Frame):
    """可滚动内容区：Canvas + 内嵌 Frame + 细滚动条 + 滚轮。"""

    def __init__(self, parent: tk.Misc, *, bg: str | None = None) -> None:
        bg = bg or BG
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.bar = _SlimBar(self, self.canvas, bg=bg)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y", padx=(6, 4))
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)

    def _on_body(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0))

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)
        self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0))

    def scroll_by(self, delta: int) -> None:
        """由 App 统一分发滚轮：按 delta 滚动（Windows 一格 120）。"""
        self.canvas.yview_scroll(int(-delta / 120), "units")

    def scroll_to(self, widget: tk.Misc) -> None:
        self.update_idletasks()
        total = max(1, self.body.winfo_height())
        self.canvas.yview_moveto(max(0.0, min(1.0, widget.winfo_y() / total)))

    def to_top(self) -> None:
        self.canvas.yview_moveto(0)
