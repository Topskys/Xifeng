"""主窗口：shadcn/ui（zinc dark）五页 —— 统计 / 模型 / 配置 / 历史 / 关于。

视觉基准是 `docs/reference/闲聊.md` 的 5 张 SVG：
左侧 192px 深色菜单、56px 顶栏、内容区 1px 边框卡片、无重阴影。

设计取舍：
- **tkinter + 自绘控件**（`ui.py`）：标准库、打包只多 ~10MB；ttk 的浅色 3D 主题
  和设计稿差太远，所以按钮/开关/分段控件/图表全部画在 Canvas 上。
- **独立进程**：托盘用 `CREATE_NO_WINDOW` 拉起 `--gui`，GUI 崩了不影响输入。
- **只读为主**：历史不提供编辑；可写的是术语表、设置、开机自启、模型目录。
- 历史三级对照（原文 / 清理 / 纠错）来自 v2 表结构；旧记录只有最终文本。
"""

from __future__ import annotations

import ctypes
import difflib
import math
import os
import platform
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

from . import __version__, applog, autostart, paths, settings as user_settings, tray
from . import ui
from .store import Store

NAV: tuple[tuple[str, str], ...] = (
    ("stats", "统计"),
    ("models", "模型"),
    ("config", "配置"),
    ("history", "历史"),
)
ALIASES = {"terms": "config"}
MODE_LABEL = {
    "ptt": "PTT 模式",
    "wav": "音频文件",
    "seconds": "定时录音",
    "interactive": "手动录音",
}
STRATEGY_LABEL = {
    "unicode": "键盘",
    "clipboard": "剪贴板",
    "auto": "自动",
    "none": "未注入",
}
HOTKEY_LABEL = {
    "ctrl": "Ctrl（左右均可）",
    "lctrl": "左 Ctrl",
    "rctrl": "右 Ctrl",
    "shift": "Shift（左右均可）",
    "lshift": "左 Shift",
    "rshift": "右 Shift",
}
HISTORY_LIMIT = 50      # 渲染上限：再多就卡；搜索仍在最近 300 条里做


# ---------------------------------------------------------------- 小工具


def _fmt_ms(ms: int | float) -> str:
    if not ms or ms <= 0:
        return "—"
    return f"{ms / 1000:.2f}"


def _fmt_hits(value: int | float) -> str:
    return f"{int(value):,}"


def _fmt_time(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return ts[:19].replace("T", " ")


def _fmt_size(num: int) -> str:
    if num <= 0:
        return "0 MB"
    if num >= 1024**3:
        return f"{num / 1024**3:.1f} GB"
    return f"{num / 1024**2:.0f} MB"


def _open_path(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 - 本地目录
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        messagebox.showerror("Xifeng", f"打不开目录：{exc}")


def _model_files(model_dir: Path) -> dict[str, bool]:
    return {
        "int8": (model_dir / "model.int8.onnx").exists(),
        "onnx": (model_dir / "model.onnx").exists(),
        "tokens": (model_dir / "tokens.txt").exists(),
    }


def _model_size(model_dir: Path) -> int:
    total = 0
    try:
        for entry in model_dir.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total


def _model_ready(model_dir: Path) -> bool:
    files = _model_files(model_dir)
    return files["tokens"] and (files["int8"] or files["onnx"])


def _model_quant(model_dir: Path) -> str:
    files = _model_files(model_dir)
    if files["int8"]:
        return "int8"
    return "fp32" if files["onnx"] else "—"


def _current_model_dir(app: "App") -> Path:
    raw = (app.settings.model_dir or "").strip()
    return Path(raw) if raw else paths.models_dir()


def _discover_models(app: "App") -> list[Path]:
    """当前模型目录 + 同级 models/asr/*，按目录名排序。"""
    current = _current_model_dir(app)
    found: dict[str, Path] = {str(current.resolve()): current} if current.exists() else {}
    for parent in {current.parent, paths.models_dir().parent}:
        try:
            if not parent.exists():
                continue
            for child in parent.iterdir():
                if child.is_dir() and _model_ready(child):
                    found.setdefault(str(child.resolve()), child)
        except OSError:
            continue
    return sorted(found.values(), key=lambda p: (not _model_ready(p), p.name.lower()))


def _strategy_text(value: str) -> str:
    return STRATEGY_LABEL.get(value or "", value or "—")


# ---------------------------------------------------------------- 系统信息


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _Runtime:
    """CPU / 内存读数：只用 Win32，不引入 psutil（打包体积 + 平台依赖）。"""

    def __init__(self) -> None:
        self._prev: tuple[int, int, int] | None = None
        self._k32 = None
        if sys.platform == "win32":
            try:
                self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                self._k32.GetSystemTimes.argtypes = (
                    ctypes.POINTER(_FILETIME),
                    ctypes.POINTER(_FILETIME),
                    ctypes.POINTER(_FILETIME),
                )
                self._k32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(_MEMORYSTATUSEX),)
            except Exception:  # noqa: BLE001
                self._k32 = None

    @staticmethod
    def _ticks(ft: _FILETIME) -> int:
        return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)

    def cpu_percent(self) -> int | None:
        if self._k32 is None:
            return None
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        try:
            if not self._k32.GetSystemTimes(idle, kernel, user):
                return None
        except Exception:  # noqa: BLE001
            return None
        idle_t, kernel_t, user_t = self._ticks(idle), self._ticks(kernel), self._ticks(user)
        total = kernel_t + user_t
        if self._prev is None:
            self._prev = (idle_t, total, total)
            return None
        prev_idle, prev_total, _ = self._prev
        self._prev = (idle_t, total, total)
        delta_total = total - prev_total
        if delta_total <= 0:
            return None
        busy = delta_total - (idle_t - prev_idle)
        return max(0, min(100, round(busy * 100 / delta_total)))

    def memory(self) -> tuple[float, float] | None:
        if self._k32 is None:
            return None
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        try:
            if not self._k32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
        except Exception:  # noqa: BLE001
            return None
        total = status.ullTotalPhys / 1024**3
        used = (status.ullTotalPhys - status.ullAvailPhys) / 1024**3
        return used, total


# ---------------------------------------------------------------- 侧边栏 / 顶栏


def draw_logo(canvas: tk.Canvas, size: float, *, fg: str | None = None, ink: str | None = None) -> None:
    """白色圆角方块 + 深色麦克风（和托盘图标同形，不依赖图片资源）。"""
    fg = fg or ui.PRIMARY
    ink = ink or ui.PRIMARY_FG
    ui.rrect(canvas, 0, 0, size, size, size * 0.25, fill=fg, outline=fg)
    left, right = size * 0.37, size * 0.63
    top, bottom = size * 0.21, size * 0.64
    ui.rrect(canvas, left, top, right, bottom, (right - left) / 2, fill=ink, outline=ink)
    canvas.create_arc(
        size * 0.27, size * 0.36, size * 0.73, size * 0.76,
        start=0, extent=180, style=tk.ARC, outline=ink, width=max(1.5, size * 0.055),
    )
    canvas.create_line(
        size * 0.5, size * 0.72, size * 0.5, size * 0.84,
        fill=ink, width=max(1.5, size * 0.055), capstyle=tk.ROUND,
    )
    canvas.create_line(
        size * 0.36, size * 0.84, size * 0.64, size * 0.84,
        fill=ink, width=max(1.5, size * 0.055), capstyle=tk.ROUND,
    )


def draw_icon(canvas: tk.Canvas, kind: str, x: float, y: float, color: str) -> None:
    """16×16 线性图标。"""
    w = 1.5
    if kind == "stats":
        for i, h in enumerate((7, 11, 14)):
            x0 = x + i * 5
            canvas.create_rectangle(x0, y + 14 - h, x0 + 3, y + 14, fill=color, outline=color)
    elif kind == "models":
        canvas.create_rectangle(x + 1, y + 4, x + 15, y + 15, outline=color, width=w)
        canvas.create_line(x + 1, y + 8, x + 15, y + 8, fill=color, width=w)
        canvas.create_arc(
            x + 5, y + 1.5, x + 11, y + 6.5, start=0, extent=180,
            style=tk.ARC, outline=color, width=w,
        )
    elif kind == "config":
        for i, cx in enumerate((10, 5, 12)):
            yy = y + 3 + i * 5
            canvas.create_line(x + 1, yy, x + 15, yy, fill=color, width=w, capstyle=tk.ROUND)
            canvas.create_oval(cx - 2, yy - 2, cx + 2, yy + 2, fill=color, outline=color)
    elif kind == "history":
        canvas.create_oval(x + 1, y + 1, x + 15, y + 15, outline=color, width=w)
        canvas.create_line(x + 8, y + 4.5, x + 8, y + 8, fill=color, width=w, capstyle=tk.ROUND)
        canvas.create_line(x + 8, y + 8, x + 11, y + 10, fill=color, width=w, capstyle=tk.ROUND)
    else:  # about
        canvas.create_oval(x + 1, y + 1, x + 15, y + 15, outline=color, width=w)
        canvas.create_oval(x + 7, y + 3.5, x + 9, y + 5.5, fill=color, outline=color)
        canvas.create_line(x + 8, y + 7, x + 8, y + 12, fill=color, width=w, capstyle=tk.ROUND)


class NavItem(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        kind: str,
        text: str,
        command: Callable[[], None],
    ) -> None:
        super().__init__(
            parent, width=168, height=34, bg=ui.SIDEBAR, highlightthickness=0, bd=0,
            cursor="hand2", takefocus=0,
        )
        self._kind = kind
        self._text = text
        self._command = command
        self._active = False
        self._hover = False
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._draw()

    def _enter(self, _event: tk.Event) -> None:
        self._hover = True
        self._draw()

    def _leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = max(168, self.winfo_width())
        if self._active or self._hover:
            hover_bg = ui.ACCENT if self._active else ui.NAV_HOVER
            ui.rrect(self, 0, 0, width, 34, 6, fill=hover_bg, outline=hover_bg)
        color = ui.PRIMARY if self._active else ui.MUTED_FG
        draw_icon(self, self._kind, 12, 9, color)
        self.create_text(
            34, 18, text=self._text, fill=color, anchor="w",
            font=ui.ui_font(9, "bold" if self._active else "normal"),
        )


class Topbar(tk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=ui.BG, height=56)
        self.pack_propagate(False)
        self.title = ui.label(self, "", size=11, color=ui.PRIMARY, weight="bold", bg=ui.BG)
        self.title.place(x=24, y=28, anchor="w")
        self.right = tk.Frame(self, bg=ui.BG)
        self.right.pack(side="right", padx=24, pady=13)
        tk.Frame(self, bg=ui.BORDER, height=1).place(
            relx=0, rely=1, relwidth=1, anchor="sw"
        )

    def set(self, title: str) -> None:
        self.title.configure(text=title)
        for child in self.right.winfo_children():
            child.destroy()


# ---------------------------------------------------------------- 页面基类


class Page(tk.Frame):
    title = ""

    def __init__(self, app: "App") -> None:
        super().__init__(app.content, bg=ui.BG)
        self.app = app
        self.scroll = ui.ScrollFrame(self, bg=ui.BG)
        self.scroll.pack(fill="both", expand=True)
        self.body = tk.Frame(self.scroll.body, bg=ui.BG)
        self.body.pack(fill="both", expand=True, padx=24, pady=20)
        self.build()

    def build(self) -> None:  # pragma: no cover - 子类实现
        pass

    def build_topbar(self, topbar: Topbar) -> None:  # pragma: no cover - 子类实现
        pass

    def refresh(self) -> None:
        pass

    def on_show(self, *, terms: bool = False) -> None:
        self.refresh()

    # -- 通用小部件

    def card(self, parent: tk.Misc | None = None, *, pad: int = 16, pady: int = 12) -> ui.Card:
        card = ui.Card(parent or self.body, pad=pad)
        card.pack(fill="x", pady=(0, pady))
        return card

    @staticmethod
    def group(
        card: ui.Card, title: str, desc: str = ""
    ) -> tk.Frame:
        head = tk.Frame(card.body, bg=ui.CARD)
        head.pack(fill="x")
        ui.label(head, title, size=10, weight="bold").pack(anchor="w")
        if desc:
            ui.label(head, desc, size=8, color=ui.MUTED_FG).pack(anchor="w", pady=(3, 0))
        ui.HLine(card.body).pack(fill="x", pady=(10, 2))
        body = tk.Frame(card.body, bg=ui.CARD)
        body.pack(fill="x")
        return body

    @staticmethod
    def row(
        parent: tk.Misc,
        title: str,
        desc: str,
        control: tk.Misc,
        *,
        pady: int = 9,
    ) -> tk.Frame:
        row = tk.Frame(parent, bg=ui.CARD)
        row.pack(fill="x", pady=pady)
        left = tk.Frame(row, bg=ui.CARD)
        left.pack(side="left", fill="x", expand=True)
        ui.label(left, title, size=9).pack(anchor="w")
        if desc:
            # wraplength 必须给：数据库路径这类长文本会把右侧控件挤出卡片
            ui.label(
                left, desc, size=8, color=ui.MUTED_FG, wraplength=560, justify="left"
            ).pack(anchor="w", pady=(2, 0))
        if isinstance(control, tk.Widget):
            # 控件的 master 是 group 的 body；pack 必须用 in_=row 才会落在本行内，
            # 且 MUST 提到 row 之上 —— 否则后创建的 row 会用底色把它盖住。
            # 注意 lift()/tkraise() 在 Canvas 子类上都被重载成"提升画布元素"，
            # MUST 走基类的窗口级 tkraise
            control.pack(in_=row, side="right", padx=(16, 0))
            tk.Misc.tkraise(control, row)
        return row


# ---------------------------------------------------------------- 统计页


class _Metric(tk.Frame):
    def __init__(self, parent: tk.Misc, label: str) -> None:
        super().__init__(parent, bg=ui.CARD)
        self._label = ui.label(self, label, size=8, color=ui.MUTED_FG)
        self._label.pack(anchor="w")
        self._value = ui.label(self, "—", size=12, weight="bold")
        self._value.pack(anchor="w", pady=(3, 4))
        self._bar = tk.Canvas(self, width=110, height=4, bg=ui.CARD, highlightthickness=0, bd=0)
        self._bar.pack(anchor="w")
        self._ratio = 0.0
        self._color = ui.CHART["other"]
        self._bar.bind("<Configure>", lambda e: self._draw_bar())

    def set(self, value: str, ratio: float | None = None, color: str | None = None) -> None:
        self._value.configure(text=value)
        if ratio is not None:
            self._ratio = max(0.0, min(1.0, ratio))
        if color:
            self._color = color
        self._draw_bar()

    def _draw_bar(self) -> None:
        self._bar.delete("all")
        w = max(1, self._bar.winfo_width())
        ui.rrect(self._bar, 0, 0, w, 4, 2, fill=ui.MUTED, outline=ui.MUTED)
        if self._ratio > 0:
            ui.rrect(self._bar, 0, 0, max(3, w * self._ratio), 4, 2,
                     fill=self._color, outline=self._color)


class StatsPage(Page):
    title = "统计"

    def __init__(self, app: "App") -> None:
        self._period = "today"
        self._rows: list[dict[str, Any]] = []
        super().__init__(app)

    def build(self) -> None:
        # KPI 五张卡（闲聊.md 统计页：P50/P95、字数、次数、超时次数）
        kpis = tk.Frame(self.body, bg=ui.BG)
        kpis.pack(fill="x", pady=(0, 12))
        for i in range(5):
            kpis.columnconfigure(i, weight=1, uniform="kpi")
        self._kpi: dict[str, dict[str, tk.Label]] = {}
        specs = (
            ("p50", "端到端延迟 · P50"),
            ("p95", "端到端延迟 · P95"),
            ("chars", "输入字数"),
            ("count", "识别次数"),
            ("timeout", "超时次数"),
        )
        for i, (key, title) in enumerate(specs):
            card = ui.Card(kpis, pad=14)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            ui.label(card.body, title, size=8, color=ui.MUTED_FG).pack(anchor="w")
            value_row = tk.Frame(card.body, bg=ui.CARD)
            value_row.pack(anchor="w", pady=(4, 2))
            value = tk.Label(value_row, text="—", bg=ui.CARD, fg=ui.PRIMARY,
                             font=ui.ui_font(17, "bold"))
            value.pack(side="left")
            unit = tk.Label(value_row, text="", bg=ui.CARD, fg=ui.MUTED_FG,
                            font=ui.ui_font(8))
            unit.pack(side="left", anchor="s", pady=(0, 3), padx=(2, 0))
            hint = ui.label(card.body, "", size=8, color=ui.MUTED_FG)
            hint.pack(anchor="w")
            self._kpi[key] = {"value": value, "unit": unit, "hint": hint}

        # 阶段耗时瀑布
        waterfall = self.card(pady=12)
        head = tk.Frame(waterfall.body, bg=ui.CARD)
        head.pack(fill="x")
        ui.label(head, "最近请求阶段耗时", size=10, weight="bold").pack(side="left")
        self._waterfall_note = ui.label(head, "", size=8, color=ui.MUTED_FG)
        self._waterfall_note.pack(side="right")
        self._waterfall = tk.Canvas(
            waterfall.body, height=212, bg=ui.CARD, highlightthickness=0, bd=0
        )
        self._waterfall.pack(fill="x", pady=(10, 0))
        self._waterfall.bind("<Configure>", lambda e: self._draw_waterfall())

        # 运行状态
        system = self.card(pady=0)
        head2 = tk.Frame(system.body, bg=ui.CARD)
        head2.pack(fill="x")
        ui.label(head2, "运行状态", size=10, weight="bold").pack(side="left")
        self._system_note = ui.label(head2, "", size=8, color=ui.MUTED_FG)
        self._system_note.pack(side="right")
        grid = tk.Frame(system.body, bg=ui.CARD)
        grid.pack(fill="x", pady=(12, 0))
        for i in range(5):
            grid.columnconfigure(i, weight=1, uniform="sys")
        self._m_cpu = _Metric(grid, "CPU")
        self._m_mem = _Metric(grid, "内存")
        self._m_gpu = _Metric(grid, "GPU 占用")
        self._m_threads = _Metric(grid, "推理线程")
        self._m_model = _Metric(grid, "当前模型")
        for i, metric in enumerate((self._m_cpu, self._m_mem, self._m_gpu,
                                    self._m_threads, self._m_model)):
            metric.grid(row=0, column=i, sticky="w", padx=(0 if i == 0 else 12, 0))

    def build_topbar(self, topbar: Topbar) -> None:
        ui.Segmented(
            topbar.right,
            [("今天", "today"), ("本周", "week"), ("全部", "all")],
            value=self._period,
            command=self._change_period,
            bg=ui.BG,
            height=30,
            size=8,
        ).pack(side="left")
        ui.Button(
            topbar.right, "导出", self._export, variant="outline", bg=ui.BG, height=30
        ).pack(side="left", padx=(10, 0))

    def _change_period(self, value: str) -> None:
        self._period = value
        self.refresh()

    def _days(self) -> int | None:
        return {"today": 1, "week": 7, "all": None}[self._period]

    def refresh(self) -> None:
        s = self.app.store.stats(self._days())
        total = int(s["total"])
        injected = int(s["injected"])
        e2e = s["e2e"]
        self._kpi["p50"]["value"].configure(text=_fmt_ms(e2e["p50"]))
        self._kpi["p50"]["unit"].configure(text="s" if e2e["p50"] else "")
        self._kpi["p50"]["hint"].configure(
            text=f"基于 {total} 次" if total else "暂无数据",
            fg=ui.MUTED_FG,
        )
        p95 = int(e2e["p95"])
        self._kpi["p95"]["value"].configure(text=_fmt_ms(p95))
        self._kpi["p95"]["unit"].configure(text="s" if p95 else "")
        ok = 0 < p95 <= 1500
        self._kpi["p95"]["hint"].configure(
            text=("✓ 目标 <1.5s" if ok else "目标 <1.5s") if p95 else "暂无数据",
            fg=ui.SUCCESS if ok else ui.MUTED_FG,
        )
        today = self.app.store.stats(1)
        all_chars = self.app.store.stats(None)["chars"]
        self._kpi["chars"]["value"].configure(text=_fmt_hits(today["chars"]))
        self._kpi["chars"]["unit"].configure(text="")
        self._kpi["chars"]["hint"].configure(
            text=f"今日 · 累计 {_fmt_hits(all_chars)} 字", fg=ui.MUTED_FG
        )
        self._kpi["count"]["value"].configure(text=str(total))
        self._kpi["count"]["unit"].configure(text="")
        by = s["by_outcome"]
        failed = by.get("Failed", 0)
        dropped = by.get("Dropped", 0)
        self._kpi["count"]["hint"].configure(
            text=(f"成功率 {injected / total:.0%} · 失败 {failed} · 丢弃 {dropped}"
                  if total else "按住 Ctrl 说一句话试试"),
            fg=ui.MUTED_FG,
        )
        timeouts = int(s.get("timeouts", 0))
        self._kpi["timeout"]["value"].configure(text=str(timeouts))
        self._kpi["timeout"]["unit"].configure(text="")
        self._kpi["timeout"]["hint"].configure(
            text="e2e > 5s 视为超时" if total else "暂无数据",
            fg=ui.MUTED_FG,
        )

        self._rows = [r for r in self.app.store.recent(12) if int(r["e2e_ms"]) > 0]
        self._waterfall_note.configure(text=f"最近 {len(self._rows)} 次" if self._rows else "")
        self._draw_waterfall()

        cpu = self.app.runtime.cpu_percent()
        mem = self.app.runtime.memory()
        self._m_cpu.set(f"{cpu}%" if cpu is not None else "—",
                        (cpu or 0) / 100, ui.CHART["other"])
        if mem:
            used, total_mem = mem
            self._m_mem.set(f"{used:.1f} GB", used / max(0.1, total_mem), ui.CHART["vad"])
        else:
            self._m_mem.set("—")
        asr_threads = self.app.settings.threads
        cores = os.cpu_count() or asr_threads
        self._m_threads.set(f"{asr_threads} / {cores}", asr_threads / max(1, cores),
                            ui.CHART["asr"])
        self._m_gpu.set("—")   # 本版本纯 CPU，无 GPU 占用；保留位置给后续 M3
        model = _current_model_dir(self.app)
        name = model.name or "未配置"
        ready = _model_ready(model)
        self._m_model.set(
            (f"SenseVoice·{_model_quant(model)}" if ready else "未找到模型"),
            0.7 if ready else 0.0,
            ui.CHART["fix"],
        )
        self._system_note.configure(text=name)

    # -- 图表

    def _draw_waterfall(self) -> None:
        c = self._waterfall
        c.delete("all")
        width = max(320, c.winfo_width())
        rows = self._rows
        row_h, bar_h = 13, 10
        top = 6
        axis_y = top + max(1, len(rows)) * row_h + 6
        legend_y = axis_y + 24
        right_pad = 54
        chart_w = max(80, width - right_pad)
        max_ms = max([2000] + [int(r["e2e_ms"]) for r in rows])

        def x_of(ms: float) -> float:
            return min(chart_w, chart_w * ms / max_ms)

        if not rows:
            c.create_text(
                width / 2, 90, text="暂无耗时数据 —— 按住 Ctrl 说一句话",
                fill=ui.MUTED_FG, font=ui.ui_font(9), anchor="center",
            )
            return

        for i, row in enumerate(rows):
            y = top + i * row_h
            asr = max(0, int(row["ms_asr"]))
            post = max(0, int(row["ms_post"]))
            inject = max(0, int(row["ms_inject"]))
            e2e = max(0, int(row["e2e_ms"]))
            other = max(0, e2e - asr - post - inject)
            x = 0.0
            for key, ms in (("other", other), ("asr", asr), ("fix", post), ("inject", inject)):
                if ms <= 0:
                    continue
                span = x_of(ms)
                if span >= 1.5:
                    ui.rrect(c, x, y, x + span, y + bar_h, 2,
                             fill=ui.CHART[key], outline=ui.CHART[key])
                x += span
            c.create_text(
                width - 2, y + bar_h / 2, text=f"{e2e} ms", fill=ui.DIM,
                font=ui.ui_font(7), anchor="e",
            )

        c.create_line(0, axis_y, chart_w, axis_y, fill=ui.BORDER)
        for ms, text, color in (
            (0, "0", ui.MUTED_FG),
            (500, "500ms", ui.MUTED_FG),
            (1000, "1000ms", ui.MUTED_FG),
            (1500, "1.5s", ui.DESTRUCTIVE),
            (max_ms, f"{max_ms}ms", ui.MUTED_FG),
        ):
            axis_x = x_of(ms)
            c.create_text(
                axis_x, axis_y + 9, text=text, fill=color, font=ui.ui_font(7),
                anchor="n" if ms < max_ms else "ne",
            )
        if max_ms >= 1500:
            line_x = x_of(1500)
            y = top
            while y < axis_y:
                c.create_line(line_x, y, line_x, min(y + 3, axis_y),
                              fill=ui.DESTRUCTIVE, dash=(2, 2))
                y += 6

        lx = 0
        for key, text in (("other", "开销"), ("asr", "ASR"), ("fix", "清理"), ("inject", "注入")):
            ui.rrect(c, lx, legend_y, lx + 9, legend_y + 9, 2,
                     fill=ui.CHART[key], outline=ui.CHART[key])
            c.create_text(lx + 14, legend_y + 4, text=text, fill=ui.MUTED_FG,
                          font=ui.ui_font(8), anchor="w")
            lx += 14 + 4 * len(text) + 22

    def _export(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出历史记录",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="xifeng-history.csv",
        )
        if not path:
            return
        try:
            import csv

            rows = self.app.store.recent(10000)
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "id", "时间", "模式", "结果", "原文", "清理后", "最终文本",
                    "字数", "e2e_ms", "asr_ms", "post_ms", "inject_ms", "策略", "错误",
                ])
                for r in rows:
                    writer.writerow([
                        r["id"], r["ts"], r["mode"], r["outcome"],
                        r["raw_text"] or "", r["clean_text"] or "", r["text"] or "",
                        r["chars"], r["e2e_ms"], r["ms_asr"], r["ms_post"],
                        r["ms_inject"], r["strategy"], r["error"],
                    ])
            self.app.toast(f"已导出 {len(rows)} 条到 {Path(path).name}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))


# ---------------------------------------------------------------- 模型页


class ModelsPage(Page):
    title = "模型"

    def build(self) -> None:
        # 「模型目录」说明在前，模型卡片列表在后
        self._list = tk.Frame(self.body, bg=ui.BG)

        info = self.card(pady=0)
        body = self.group(info, "模型目录", "模型体积 228MB，外置在 exe 同级目录（G24）")
        ui.label(
            body,
            "本版本只支持 SenseVoice 系列（sherpa-onnx）。目录内需要 "
            "model.int8.onnx（或 model.onnx）与 tokens.txt；换模型后托盘重启生效。",
            size=8, color=ui.MUTED_FG, wraplength=820, justify="left",
        ).pack(anchor="w", pady=(2, 8))
        actions = tk.Frame(body, bg=ui.CARD)
        actions.pack(anchor="w")
        ui.Button(actions, "打开模型目录", lambda: _open_path(_current_model_dir(self.app)),
                  variant="outline", bg=ui.CARD, height=28).pack(side="left")
        ui.Button(actions, "刷新", self.refresh, variant="ghost", bg=ui.CARD,
                  height=28).pack(side="left", padx=(8, 0))

        self._list.pack(fill="x")

    def build_topbar(self, topbar: Topbar) -> None:
        ui.Button(topbar.right, "刷新", self.refresh, variant="outline", bg=ui.BG,
                  height=30).pack(side="left")
        ui.Button(topbar.right, "＋ 添加模型", self._add_model, variant="primary",
                  bg=ui.BG, height=30).pack(side="left", padx=(10, 0))

    def refresh(self) -> None:
        for child in self._list.winfo_children():
            child.destroy()
        current = _current_model_dir(self.app)
        models = _discover_models(self.app)
        if not models:
            self._warning(current)
            return
        for path in models:
            self._model_card(path, path.resolve() == current.resolve())

    def _warning(self, model_dir: Path) -> None:
        # MUST 打进 _list：refresh 只清空它，打进 body 会每次刷新都追加一套
        card = self.card(self._list, pady=0)
        head = tk.Frame(card.body, bg=ui.CARD)
        head.pack(fill="x")
        ui.label(head, "未找到可用模型", size=10, weight="bold",
                 color=ui.DESTRUCTIVE).pack(side="left")
        ui.label(card.body, str(model_dir), size=8, color=ui.MUTED_FG).pack(
            anchor="w", pady=(6, 0)
        )
        ui.label(
            card.body,
            "把 sensevoice-small-int8 目录复制到该路径，或点右上角「添加模型」选中已有目录。",
            size=8, color=ui.MUTED_FG,
        ).pack(anchor="w", pady=(4, 0))

    def _model_card(self, path: Path, current: bool) -> None:
        card = self.card(self._list, pady=10)
        row = tk.Frame(card.body, bg=ui.CARD)
        row.pack(fill="x")

        avatar = tk.Canvas(row, width=40, height=40, bg=ui.CARD, highlightthickness=0, bd=0)
        ui.rrect(avatar, 0, 0, 40, 40, 8, fill=ui.MUTED, outline=ui.MUTED)
        initials = "".join(part[0].upper() for part in path.name.replace("-", " ").split()[:2])
        avatar.create_text(20, 21, text=initials or "SV", fill=ui.PRIMARY,
                           font=ui.ui_font(12, "bold"), anchor="center")
        avatar.pack(side="left", anchor="n")

        info = tk.Frame(row, bg=ui.CARD)
        info.pack(side="left", fill="x", expand=True, padx=(12, 12))

        title_row = tk.Frame(info, bg=ui.CARD)
        title_row.pack(fill="x")
        name = "SenseVoice-Small" if "sensevoice" in path.name.lower() else path.name
        ui.label(title_row, name, size=10, weight="bold").pack(side="left")
        if current:
            ui.Badge(title_row, "当前使用", variant="success", bg=ui.CARD).pack(
                side="left", padx=(8, 0)
            )
        for text in ("中文", _model_quant(path)):
            ui.Badge(title_row, text, variant="muted", bg=ui.CARD).pack(
                side="left", padx=(6, 0)
            )

        size = _model_size(path)
        ready = _model_ready(path)
        detail = (
            f"{_fmt_size(size)} · {path.name} · "
            + ("模型文件完整" if ready else "缺少 model.onnx 或 tokens.txt")
        )
        ui.label(info, detail, size=8, color=ui.MUTED_FG).pack(anchor="w", pady=(5, 0))
        ui.label(info, str(path), size=8, color=ui.DIM).pack(anchor="w", pady=(2, 0))

        actions = tk.Frame(row, bg=ui.CARD)
        actions.pack(side="right", anchor="n")
        if not current:
            ui.Button(
                actions, "设为当前",
                lambda p=path: self._use_model(p),
                variant="primary", bg=ui.CARD, height=28,
            ).pack(side="left")
        ui.Button(
            actions, "打开目录", lambda p=path: _open_path(p),
            variant="outline", bg=ui.CARD, height=28,
        ).pack(side="left", padx=(8, 0))

    def _use_model(self, path: Path) -> None:
        if not _model_ready(path):
            messagebox.showwarning("Xifeng", "该目录缺少 model.onnx（或 int8）与 tokens.txt")
            return
        s = user_settings.update(self.app.settings, model_dir=str(path))
        user_settings.save(self.app.store, s)
        self.app.settings = s
        self.refresh()
        self.app.toast("已切换模型 · 托盘重启后生效")

    def _add_model(self) -> None:
        chosen = filedialog.askdirectory(title="选择 SenseVoice 模型目录")
        if not chosen:
            return
        path = Path(chosen)
        if not _model_ready(path):
            messagebox.showwarning(
                "Xifeng",
                "这个目录里没有可用的模型。\n需要 model.int8.onnx（或 model.onnx）和 tokens.txt。",
            )
            return
        self._use_model(path)


# ---------------------------------------------------------------- 配置页


class ConfigPage(Page):
    title = "配置"

    def __init__(self, app: "App") -> None:
        self.dl: ui.Select | None = None
        self.threads: ui.Select | None = None
        super().__init__(app)

    def build(self) -> None:
        # 语音输入
        card = self.card()
        body = self.group(card, "语音输入", "按住说话（PTT），松开识别并注入光标位置")
        self.hotkey = ui.Select(
            body, [(label, key) for key, label in HOTKEY_LABEL.items()],
            value=self.app.settings.hotkey, bg=ui.CARD, width=170,
        )
        self.row(body, "触发热键", "全局键，左右两侧可分别指定", self.hotkey)
        self.mode = ui.Segmented(
            body, [("关闭", "ptt"), ("开启", "vad")],
            value="vad" if self.app.settings.use_vad else "ptt",
            command=lambda v: None, bg=ui.CARD, size=8, height=26,
        )
        self.row(body, "录音段切分", "对按住说话的录音做 VAD 双阈值切分；仍需按住热键", self.mode)

        # 识别与纠错
        card = self.card()
        body = self.group(card, "识别与纠错", "ASR 固定中文、关闭 ITN（见 G22 / metrics.md）")
        engine = tk.Frame(body, bg=ui.CARD)
        ui.Badge(engine, "SenseVoice-Small", variant="muted", bg=ui.CARD, height=20).pack(
            side="left"
        )
        ui.Badge(engine, "sherpa-onnx", variant="muted", bg=ui.CARD, height=20).pack(
            side="left", padx=(6, 0)
        )
        self.row(body, "ASR 模型", "在「模型」页切换；语言与 ITN 在 Web 配置页设置", engine)
        self.dl = ui.Select(
            body,
            [("ModelScope", "modelscope"), ("HuggingFace 镜像", "hf-mirror"),
             ("GitHub Release", "github"), ("本地目录", "local")],
            value=self.app.settings.download_source, bg=ui.CARD, width=190,
        )
        self.row(body, "模型下载源", "模型文件需手动放置；此项记录偏好，M3 做真实下载", self.dl)
        self.threads = ui.Select(
            body, [(str(i), str(i)) for i in (1, 2, 4, 8, 16, 32)],
            value=str(self.app.settings.threads), bg=ui.CARD, width=170,
        )
        self.row(body, "ASR 推理线程", "8 核 CPU 实测 8 线程最快，与 CPU 核数持平即可", self.threads)
        self.filler = ui.Segmented(
            body, [("关闭", "off"), ("标准", "standard"), ("深度", "deep")],
            value=self.app.settings.filler_level, bg=ui.CARD, size=8, height=26,
        )
        self.row(body, "语气词清理", "标准剔除句首/句中填充词；深度连句尾助词一起删", self.filler)
        llm_wrap = tk.Frame(body, bg=ui.CARD)
        self.llm = ui.Switch(
            llm_wrap, self.app.settings.model_correct, bg=ui.CARD
        )
        self.llm.pack(side="left")
        ui.Badge(llm_wrap, "默认关 · +0.5–1.5s", variant="muted", bg=ui.CARD).pack(
            side="left", padx=(8, 0)
        )
        self.row(body, "模型纠错", "本地 Qwen2.5-0.5B 自动改同音字/专有名词，改坏会回退原文", llm_wrap)

        self.popup_color = ui.Select(
            body,
            [("录音红", "red"), ("电光青", "cyan"), ("森林绿", "green"), ("月光白", "white")],
            value=self.app.settings.popup_color, bg=ui.CARD, width=170,
        )
        self.row(body, "浮窗音效颜色", "录音指示浮窗的图标底色与音柱颜色", self.popup_color)

        # 输出与清理
        card = self.card()
        body = self.group(card, "输出与清理", "这些设置保存在本机 SQLite，托盘重启后生效")
        self.inject = ui.Select(
            body,
            [("自动（键盘 / 剪贴板）", "auto"), ("仅键盘事件", "unicode"), ("仅剪贴板", "clipboard")],
            value=self.app.settings.inject_strategy, bg=ui.CARD, width=190,
        )
        self.row(body, "注入方式", "自动：先发按键，确认没打进去才走剪贴板", self.inject)
        self.restore = ui.Switch(
            body, self.app.settings.restore_focus, bg=ui.CARD
        )
        self.row(body, "焦点恢复", "注入前发现焦点被切走时，尝试抢回原窗口", self.restore)
        slider_box = tk.Frame(body, bg=ui.CARD)
        self.clipboard_value = ui.label(slider_box, "", size=9, weight="bold")
        self.clipboard_value.pack(side="right", padx=(10, 0))
        self.clipboard_ms = ui.Slider(
            slider_box, 100, 2000, max(100, self.app.settings.clipboard_restore_ms),
            command=self._on_clipboard_ms, bg=ui.CARD, width=150, step=100,
        )
        self.clipboard_ms.pack(side="right")
        self._on_clipboard_ms(self.clipboard_ms.get())
        self.row(body, "剪贴板恢复", "剪贴板兜底后多久恢复原内容（太短会粘出旧内容）", slider_box)
        self.punct = ui.Segmented(
            body, [("保留", "keep"), ("去掉", "strip")],
            value=self.app.settings.final_punct, bg=ui.CARD, size=8, height=26,
        )
        self.row(body, "句末标点", "是否需要句号/问号等句末标点", self.punct)
        self.space = ui.Switch(
            body, self.app.settings.space_between_cjk_latin, bg=ui.CARD
        )
        self.row(body, "中英文间距", "在中文与英文/数字之间补一个空格", self.space)
        self.trim = ui.Switch(body, self.app.settings.trim_silence, bg=ui.CARD)
        self.row(body, "首尾静音裁剪", "去掉句首句尾静音，减少幻觉与 ASR 耗时", self.trim)

        # 系统
        card = self.card(pady=0)
        body = self.group(card, "系统", "主题、开机自启与数据维护")
        self.theme_sel = ui.Segmented(
            body, [("深色", "dark"), ("浅色", "light")],
            value=self.app.settings.theme, bg=ui.CARD, size=8, height=26,
        )
        self.row(body, "界面主题", "保存后立即切换，无需重启", self.theme_sel)
        self.popup = ui.Switch(body, self.app.settings.show_popup, bg=ui.CARD)
        self.row(body, "录音指示浮窗", "按住说话时屏幕下方显示录音状态与音量", self.popup)
        try:
            autostart_on = autostart.is_enabled()
        except Exception:  # noqa: BLE001
            autostart_on = False
        self.autostart = ui.Switch(body, autostart_on, self._toggle_autostart, bg=ui.CARD)
        self.row(body, "开机自启", "登录后自动以托盘模式运行", self.autostart)

        data_row = tk.Frame(body, bg=ui.CARD)
        ui.Button(data_row, "打开", lambda: _open_path(self.app.store.path.parent),
                  variant="outline", bg=ui.CARD, height=26, padx=10).pack(side="left")
        ui.Button(data_row, "清理 30 天前", self._prune, variant="danger", bg=ui.CARD,
                  height=26, padx=10).pack(side="left", padx=(8, 0))
        self.row(body, "本地数据", str(self.app.store.path), data_row)

        ui.label(
            self.body,
            "提示：注入方式、热键、清理规则等保存在本机数据库；模型目录与这些设置都在"
            "托盘进程下次启动时读取。开机自启立即生效。",
            size=8, color=ui.DIM, wraplength=860, justify="left",
        ).pack(anchor="w", pady=(0, 8))

    def build_topbar(self, topbar: Topbar) -> None:
        ui.Button(topbar.right, "恢复默认", self._reset, variant="outline", bg=ui.BG,
                  height=30).pack(side="left")
        ui.Button(topbar.right, "保存", self._save, variant="primary", bg=ui.BG,
                  height=30).pack(side="left", padx=(10, 0))

    def _on_clipboard_ms(self, value: int) -> None:
        self.clipboard_value.configure(text=f"{value} ms")

    def _save(self) -> None:
        s = user_settings.update(
            self.app.settings,
            inject_strategy=self.inject.get(),
            restore_focus=self.restore.get(),
            clipboard_restore_ms=self.clipboard_ms.get(),
            filler_level=self.filler.get(),
            final_punct=self.punct.get(),
            space_between_cjk_latin=self.space.get(),
            trim_silence=self.trim.get(),
            use_vad=self.mode.get() == "vad",
            hotkey=self.hotkey.get(),
            threads=int(self.threads.get()),
            download_source=self.dl.get(),
            theme=self.theme_sel.get(),
            show_popup=self.popup.get(),
            model_correct=self.llm.get(),
            popup_color=self.popup_color.get(),
        )
        user_settings.save(self.app.store, s)
        theme_changed = s.theme != self.app.settings.theme
        self.app.settings = s
        if theme_changed:
            # 重建会销毁当前页面（含本回调所属的控件），放到 after 里做
            self.app.after(10, lambda: (self.app.apply_theme(),
                                        self.app.toast("已保存 · 主题已切换")))
        else:
            self.app.toast("已保存 · 托盘重启后生效")

    def _reset(self) -> None:
        if not messagebox.askyesno("恢复默认", "把设置恢复为默认值？"):
            return
        s = user_settings.update(user_settings.DEFAULTS)
        user_settings.save(self.app.store, s)
        self.app.settings = s
        self.hotkey.set(s.hotkey)
        self.mode.set("vad" if s.use_vad else "ptt")
        self.filler.set(s.filler_level)
        self.inject.set(s.inject_strategy)
        self.restore.set(s.restore_focus)
        self.clipboard_ms.set(max(100, s.clipboard_restore_ms))
        self._on_clipboard_ms(self.clipboard_ms.get())
        self.punct.set(s.final_punct)
        self.space.set(s.space_between_cjk_latin)
        self.trim.set(s.trim_silence)
        self.dl.set(s.download_source)
        self.threads.set(str(s.threads))
        self.theme_sel.set(s.theme)
        self.popup.set(s.show_popup)
        self.llm.set(s.model_correct)
        self.popup_color.set(s.popup_color)
        self.app.toast("已恢复默认值 · 托盘重启后生效")

    def _toggle_autostart(self, enabled: bool) -> None:
        try:
            if enabled:
                autostart.enable()
            else:
                autostart.disable()
            self.app.toast("已开启开机自启" if enabled else "已关闭开机自启")
        except Exception as exc:  # noqa: BLE001
            self.autostart.set(not enabled)
            messagebox.showerror("开机自启", f"设置失败：{exc}")

    def _prune(self) -> None:
        if not messagebox.askyesno("清理历史", "删除 30 天前的历史记录？"):
            return
        removed = self.app.store.prune(30)
        self.app.toast(f"已删除 {removed} 条历史")

    def on_show(self, *, terms: bool = False) -> None:
        self.refresh()
        self.scroll.to_top()


# ---------------------------------------------------------------- 历史页


class HistoryPage(Page):
    title = "历史"

    def __init__(self, app: "App") -> None:
        self._query = tk.StringVar()
        self._filter = "all"
        self._last_signature: tuple[int, str, str] | None = None
        self._count = 0
        self._corrected = 0
        self._all_btn: ui.Button | None = None
        self._fix_btn: ui.Button | None = None
        super().__init__(app)

    def build(self) -> None:
        self._cards = tk.Frame(self.body, bg=ui.BG)
        self._cards.pack(fill="x")
        self._footer = ui.label(self.body, "", size=8, color=ui.DIM)
        self._footer.pack(anchor="w", pady=(4, 0))

    def build_topbar(self, topbar: Topbar) -> None:
        search = ui.Input(
            topbar.right, width=210, placeholder="搜索文本…", bg=ui.BG,
            textvariable=self._query,
        )
        search.pack(side="left")
        search.entry.bind("<KeyRelease>", lambda e: self.app.after(250, self.refresh))
        self._fix_btn = ui.Button(
            topbar.right, "已纠错 0", lambda: self._set_filter("corrected"),
            variant="outline", bg=ui.BG, height=30,
        )
        self._fix_btn.pack(side="left", padx=(10, 0))
        self._all_btn = ui.Button(
            topbar.right, "全部 0", lambda: self._set_filter("all"),
            variant="accent", bg=ui.BG, height=30,
        )
        self._all_btn.pack(side="left", padx=(10, 0))
        self._sync_filter_buttons()

    def _set_filter(self, value: str) -> None:
        self._filter = value
        self._sync_filter_buttons()
        self.refresh()

    def _sync_filter_buttons(self) -> None:
        # 顶栏换页时按钮会被销毁重建；定时刷新可能比重建更早触发，必须守卫
        if not (self._all_btn and self._fix_btn):
            return
        try:
            if self._all_btn.winfo_exists() and self._fix_btn.winfo_exists():
                self._all_btn.set_variant("accent" if self._filter == "all" else "outline")
            self._fix_btn.set_variant("accent" if self._filter == "corrected" else "outline")
            self._all_btn.set_text(f"全部 {self._count}")
            self._fix_btn.set_text(f"已纠错 {self._corrected}")
        except tk.TclError:  # 窗口已销毁，忽略
            pass

    def refresh(self) -> None:
        rows = self.app.store.recent(300)
        query = self._query.get().strip().lower()
        self._count = len(rows)
        self._corrected = sum(
            1 for r in rows
            if int(r["fixes"] or 0) > 0
            or (r["raw_text"] and r["text"] and r["raw_text"] != r["text"])
        )
        self._sync_filter_buttons()
        if query:
            rows = [
                r for r in rows
                if query in (r["raw_text"] or "").lower()
                or query in (r["clean_text"] or "").lower()
                or query in (r["text"] or "").lower()
            ]
        if self._filter == "corrected":
            rows = [
                r for r in rows
                if int(r["fixes"] or 0) > 0
                or (r["raw_text"] and r["text"] and r["raw_text"] != r["text"])
            ]
        signature = (self.app.store.count(), query, self._filter)
        if signature == self._last_signature and self._cards.winfo_children():
            return
        self._last_signature = signature
        for child in self._cards.winfo_children():
            child.destroy()
        shown = rows[:HISTORY_LIMIT]
        if not shown:
            ui.label(
                self._cards, "没有匹配的记录。" if query else "还没有记录 —— 按住 Ctrl 说一句话。",
                size=9, color=ui.MUTED_FG,
            ).pack(anchor="w", pady=20)
        for row in shown:
            self._history_card(row)
        self._footer.configure(
            text=f"共 {self._count} 条记录 · 本地 SQLite · "
                 + (f"显示最近 {len(shown)} 条" if len(rows) > len(shown) else "已全部显示")
        )

    def _history_card(self, row: dict[str, Any]) -> None:
        card = ui.Card(self._cards, pad=14)
        card.pack(fill="x", pady=(0, 10))
        head = tk.Frame(card.body, bg=ui.CARD)
        head.pack(fill="x", pady=(0, 8))
        ui.label(head, _fmt_time(row["ts"]), size=8, color=ui.MUTED_FG).pack(side="left")
        ui.Badge(head, MODE_LABEL.get(row["mode"], row["mode"]), variant="muted",
                 bg=ui.CARD).pack(side="left", padx=(8, 0))
        ui.Badge(head, f"注入 · {_strategy_text(row['strategy'])}", variant="muted",
                 bg=ui.CARD).pack(side="left", padx=(6, 0))
        if row["outcome"] != "Injected":
            variant = "danger" if row["outcome"] == "Failed" else "outline"
            ui.Badge(head, row["outcome"], variant=variant, bg=ui.CARD).pack(
                side="left", padx=(6, 0)
            )
        ui.Button(
            head, "⋯", lambda r=row: self._menu(r), variant="outline", bg=ui.CARD,
            width=28, height=24, padx=0,
        ).pack(side="right")
        ui.Button(
            head, "复制", lambda r=row: self._copy(r), variant="outline", bg=ui.CARD,
            height=24, padx=10, size=8,
        ).pack(side="right", padx=(0, 6))

        raw = row["raw_text"] or ""
        clean = row["clean_text"] or ""
        final = row["text"] or ""
        if not raw and not clean and final:
            raw = final     # 旧库只有最终文本，不伪造前两级
            clean = final
        levels = tk.Frame(card.body, bg=ui.CARD)
        levels.pack(fill="x")
        levels.columnconfigure(1, weight=1)
        self._level(levels, 0, "原文", raw, clean, "before")
        self._level(levels, 1, "清理", clean, final, "clean")
        self._level(levels, 2, "纠错", clean, final, "after", strong=True)

        fixes = int(row["fixes"] or 0)
        detail = (
            f"端到端 {_fmt_ms(row['e2e_ms'])}s · ASR {row['ms_asr']}ms · "
            f"清理 {row['ms_post']}ms · 注入 {row['ms_inject']}ms · "
            f"纠正 {fixes} 处 · 注入：{_strategy_text(row['strategy'])}"
        )
        if row["error"]:
            detail += f" · 错误：{row['error'][:80]}"
        ui.label(card.body, detail, size=7, color=ui.DIM).pack(anchor="w", pady=(7, 0))

    def _level(
        self,
        parent: tk.Frame,
        index: int,
        title: str,
        before: str,
        after: str,
        mode: str,
        *,
        strong: bool = False,
    ) -> None:
        ui.label(parent, title, size=8, color=ui.DIM).grid(
            row=index, column=0, sticky="nw", padx=(0, 10), pady=(2, 0)
        )
        height = _text_height(before)
        widget = tk.Text(
            parent, height=height, width=1, wrap="word", bg=ui.CARD, fg=ui.MUTED_FG,
            font=ui.ui_font(9, "bold" if strong else "normal"), bd=0,
            highlightthickness=0, padx=0, pady=0, spacing1=0, spacing3=1,
            cursor="arrow", takefocus=0,
        )
        widget.grid(row=index, column=1, sticky="ew", pady=(0, 3))
        widget.tag_configure("warn", foreground=ui.DESTRUCTIVE)
        widget.tag_configure("del", foreground=ui.DESTRUCTIVE, overstrike=True)
        widget.tag_configure("good", foreground=ui.SUCCESS)
        widget.tag_configure("base", foreground=ui.MUTED_FG)
        widget.tag_configure("strong", foreground=ui.PRIMARY)
        widget.configure(state="normal")
        if not before:
            widget.insert("end", "—", "base")
        else:
            for piece, kind in _diff_pieces(before, after, mode):
                tag = {
                    "before": "warn" if kind in ("delete", "replace") else "base",
                    "clean": "del" if kind in ("delete", "replace") else "base",
                    "after": "good" if kind in ("insert", "replace") else "strong",
                }[mode]
                widget.insert("end", piece, tag)
        widget.configure(state="disabled")

    def _copy(self, row: dict[str, Any]) -> None:
        text = row["text"] or row["clean_text"] or row["raw_text"] or ""
        if not text:
            self.app.toast("这条记录没有保存文本")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast("已复制最终文本")

    def _menu(self, row: dict[str, Any]) -> None:
        menu = tk.Menu(
            self, tearoff=0, bg=ui.SIDEBAR, fg=ui.PRIMARY, activebackground=ui.ACCENT,
            activeforeground=ui.PRIMARY, bd=0, font=ui.ui_font(9),
        )
        menu.add_command(label="查看详情", command=lambda: self._detail(row))
        menu.add_command(label="复制原文", command=lambda: self._copy_text(row["raw_text"]))
        menu.add_command(label="复制最终文本", command=lambda: self._copy_text(row["text"]))
        menu.add_separator()
        menu.add_command(label="删除这条记录", command=lambda: self._delete(row))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _copy_text(self, text: str | None) -> None:
        if not text:
            self.app.toast("这条记录没有保存文本")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast("已复制")

    def _delete(self, row: dict[str, Any]) -> None:
        if not messagebox.askyesno("删除记录", "删除这条历史记录？（不影响识别与术语表）"):
            return
        self.app.store.delete_history(int(row["id"]))
        self.refresh()
        self.app.toast("已删除")

    def _detail(self, row: dict[str, Any]) -> None:
        win = tk.Toplevel(self.app)
        win.title(f"记录 #{row['id']}")
        win.configure(bg=ui.BG)
        win.geometry("760x520")
        win.transient(self.app)
        head = tk.Frame(win, bg=ui.BG)
        head.pack(fill="x", padx=24, pady=(20, 8))
        ui.label(head, f"记录 #{row['id']} · {_fmt_time(row['ts'])}", size=11,
                 color=ui.PRIMARY, weight="bold", bg=ui.BG).pack(side="left")
        body = tk.Frame(win, bg=ui.BG)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        meta = (
            f"{row['outcome']} · {MODE_LABEL.get(row['mode'], row['mode'])} · "
            f"e2e {row['e2e_ms']}ms · ASR {row['ms_asr']}ms · "
            f"清理 {row['ms_post']}ms · 注入 {row['ms_inject']}ms · "
            f"策略 {_strategy_text(row['strategy'])}"
        )
        ui.label(body, meta, size=8, color=ui.MUTED_FG, bg=ui.BG).pack(anchor="w")
        if row["error"]:
            ui.label(body, f"错误：{row['error']}", size=8, color=ui.DESTRUCTIVE,
                     bg=ui.BG).pack(anchor="w", pady=(4, 0))
        for title, text in (
            ("原文", row["raw_text"]),
            ("清理后", row["clean_text"]),
            ("最终注入", row["text"]),
        ):
            ui.label(body, title, size=9, color=ui.MUTED_FG, bg=ui.BG).pack(
                anchor="w", pady=(12, 4)
            )
            box = tk.Text(
                body, height=5, wrap="word", bg=ui.INPUT_BG, fg=ui.PRIMARY,
                font=ui.ui_font(9), bd=0, highlightthickness=1,
                highlightbackground=ui.BORDER, padx=10, pady=8,
            )
            box.pack(fill="both", expand=True)
            box.insert("1.0", text or "（未保存）")
            box.configure(state="disabled")
        return win


def _text_height(text: str, chars_per_line: int = 68) -> int:
    if not text:
        return 1
    lines = sum(max(1, math.ceil(len(part) / chars_per_line)) for part in text.splitlines())
    return min(4, max(1, lines))


def _diff_pieces(before: str, after: str, mode: str) -> list[tuple[str, str]]:
    """把 before/after 拆成 (片段, opcode) 列表，用于三级对照着色。"""
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    pieces: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        piece = before[i1:i2] if mode in ("before", "clean") else after[j1:j2]
        if piece:
            pieces.append((piece, tag))
    return pieces


# ---------------------------------------------------------------- 应用


class App(tk.Tk):
    def __init__(self, db_path: str | None = None, tab: str = "history") -> None:
        super().__init__()
        self.title("Xifeng")
        self.configure(bg=ui.BG)
        self.geometry("1180x760")
        self.minsize(1000, 660)
        self._set_icon()
        self.store = Store(db_path)
        self.settings = user_settings.load(self.store)
        ui.set_theme(self.settings.theme)
        self.runtime = _Runtime()
        self._pages: dict[str, Page] = {}
        self._page: Page | None = None
        self._toast: tk.Widget | None = None
        self._current_tab = tab
        self._build_shell()
        self._show(tab)
        self._apply_titlebar()
        self.after(5000, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # -- 外壳

    def _set_icon(self) -> None:
        try:
            from PIL import ImageTk

            self._icon = ImageTk.PhotoImage(tray._image())
            self.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001 - 没图标不影响使用
            self._icon = None

    def _build_shell(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        sidebar = tk.Frame(self, bg=ui.SIDEBAR, width=192)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.pack_propagate(False)
        tk.Frame(self, bg=ui.BORDER, width=1).grid(row=0, column=0, sticky="nse")

        brand = tk.Frame(sidebar, bg=ui.SIDEBAR, height=68)
        brand.pack(fill="x")
        brand.pack_propagate(False)
        logo = tk.Canvas(brand, width=28, height=28, bg=ui.SIDEBAR,
                         highlightthickness=0, bd=0)
        logo.place(x=20, y=20)
        draw_logo(logo, 28)
        tk.Label(brand, text="Xifeng", bg=ui.SIDEBAR, fg=ui.PRIMARY,
                 font=ui.ui_font(10, "bold"), anchor="w").place(x=58, y=17)
        tk.Label(brand, text=f"v{__version__} · 本地语音输入", bg=ui.SIDEBAR, fg=ui.DIM,
                 font=ui.ui_font(7), anchor="w").place(x=58, y=35)

        nav = tk.Frame(sidebar, bg=ui.SIDEBAR)
        nav.pack(fill="x", padx=12, pady=(20, 0))
        self._nav: dict[str, NavItem] = {}
        for key, text in NAV:
            item = NavItem(nav, key, text, lambda k=key: self._show(k))
            item.pack(fill="x", pady=(0, 6))
            self._nav[key] = item

        self.main = tk.Frame(self, bg=ui.BG)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.topbar = Topbar(self.main)
        self.topbar.pack(fill="x")
        self.content = tk.Frame(self.main, bg=ui.BG)
        self.content.pack(fill="both", expand=True)

        # tkinter 不会把滚轮事件路由到 Canvas：统一在 App 分发到当前页的 ScrollFrame
        self.bind_all("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event: tk.Event) -> str | None:
        page = self._page
        if page is None:
            return None
        # 内嵌 Text/Listbox 自己会滚：别抢它们的滚轮
        under = self.winfo_containing(event.x_root, event.y_root)
        if isinstance(under, (tk.Text, tk.Listbox)):
            return None
        page.scroll.scroll_by(event.delta)
        return "break"

    # -- 页面切换

    def _show(self, key: str) -> None:
        original = key
        key = ALIASES.get(key, key)
        if key not in dict(NAV):
            key = "history"
        self._current_tab = original
        page = self._pages.get(key)
        if page is None:
            page = _PAGES[key](self)
            self._pages[key] = page
        if self._page is not None and self._page is not page:
            self._page.pack_forget()
        page.pack(fill="both", expand=True)
        self._page = page
        self.topbar.set(page.title)
        page.build_topbar(self.topbar)
        for nav_key, item in self._nav.items():
            item.set_active(nav_key == key)
        page.on_show(terms=(original == "terms"))

    def _tick(self) -> None:
        try:
            if self._page is not None:
                self._page.refresh()
        except Exception as exc:  # noqa: BLE001 - 刷新失败不能关窗口
            print(f"[gui] 刷新失败：{exc}", flush=True)
        self.after(5000, self._tick)

    # -- 主题

    def _apply_titlebar(self) -> None:
        """标题栏跟随主题（Win10 1809+ 的 DWM 暗色属性；失败就保持系统默认）。"""
        if sys.platform != "win32":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1 if ui.current_theme() == "dark" else 0)
            for attribute in (20, 19):     # DWMWA_USE_IMMERSIVE_DARK_MODE（旧版本是 19）
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                ) == 0:
                    break
        except Exception:  # noqa: BLE001 - 标题栏颜色不影响功能
            pass

    def apply_theme(self) -> None:
        """切主题后原地重建整个窗口。

        控件在构造时把底色烧进了 Tk，改全局令牌救不回来，只能全部重建。
        Store / settings / runtime 保留，不碰托盘进程。
        """
        tab = self._current_tab
        ui.set_theme(self.settings.theme)
        for child in self.winfo_children():
            child.destroy()
        self._pages.clear()
        self._page = None
        self._toast = None
        self.configure(bg=ui.BG)     # 根窗口的底色是 __init__ 配的，重建时必须重配
        self._build_shell()
        self._show(tab)
        self._apply_titlebar()

    # -- 提示

    def toast(self, text: str, *, color: str | None = None) -> None:
        color = color or ui.SUCCESS
        if self._toast is not None:
            self._toast.destroy()
        frame = tk.Frame(self, bg=ui.SIDEBAR, highlightbackground=ui.BORDER,
                         highlightthickness=1, bd=0)
        ui.Dot(frame, color=color, bg=ui.SIDEBAR).pack(side="left", padx=(12, 8), pady=10)
        tk.Label(frame, text=text, bg=ui.SIDEBAR, fg=ui.PRIMARY,
                 font=ui.ui_font(8)).pack(side="left", padx=(0, 14), pady=9)
        frame.place(relx=1.0, rely=1.0, x=-24, y=-18, anchor="se")
        self._toast = frame
        self.after(4000, lambda: self._dismiss(frame))

    def _dismiss(self, frame: tk.Widget) -> None:
        if self._toast is frame:
            self._toast = None
        try:
            frame.destroy()
        except tk.TclError:
            pass

    # -- 生命周期

    def _close(self) -> None:
        try:
            self.store.close()
        finally:
            self.destroy()


_PAGES: dict[str, type[Page]] = {
    "stats": StatsPage,
    "models": ModelsPage,
    "config": ConfigPage,
    "history": HistoryPage,
}


# ---------------------------------------------------------------- 入口


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


def run(db_path: str | None = None, tab: str = "history") -> int:
    _enable_dpi_awareness()
    # GUI 可能由托盘的 CREATE_NO_WINDOW 子进程启动（stdout 为 None），也可能被重定向到
    # gbk 管道；不先修好流，Store 的一句 ⚠ 警告就会让窗口直接打不开。
    old_out, old_err = sys.stdout, sys.stderr
    log = applog.setup()
    try:
        try:
            app = App(db_path, tab)
        except Exception as exc:  # noqa: BLE001
            try:
                messagebox.showerror("Xifeng", f"打不开主窗口：{exc}")
            except Exception:  # noqa: BLE001
                print(f"[gui] 打不开主窗口：{exc}", file=sys.stderr)
            return 1
        app.mainloop()
        return 0
    finally:
        applog.restore(log, old_out, old_err)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
