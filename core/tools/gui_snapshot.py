"""GUI 视觉快照：进程内渲染四页并截图，用于对齐 docs/reference/闲聊.md。

不是交付物，只在开发机跑。截图写到仓库外目录（AGENTS.md G24 精神）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xifeng import gui  # noqa: E402

OUT = Path(
    os.environ.get("XIFENG_SHOT_DIR")
    or os.environ.get("VOICEIME_SHOT_DIR")
    or Path(__file__).resolve().parents[2] / ".shots"
)
OUT.mkdir(parents=True, exist_ok=True)


def capture(hwnd: int, path: Path) -> None:
    """用 PrintWindow 抓窗口位图（不依赖屏幕是否可见）。"""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)

    # 2 = PW_RENDERFULLCONTENT
    ok = user32.PrintWindow(hwnd, mdc, 2)
    print(f"  PrintWindow -> {ok}")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h  # top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0  # BI_RGB

    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    print(f"  GetDIBits rows={got} size={w}x{h}")

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)

    from PIL import Image

    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    img.save(path)
    print(f"  saved {path} ({w}x{h})")


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    app = gui.App(db_path=None, tab="stats")
    app.deiconify()
    app.update_idletasks()
    app.update()

    for key in ("stats", "models", "config", "history"):
        if key not in app._pages:
            app._pages[key] = gui._PAGES[key](app)
        app._pages[key].pack(fill="both", expand=True)
        app.update_idletasks()
        app.update()
        hwnd = int(app.frame.winfo_id())
        # 取顶层窗口 HWND（frame 是子窗口）
        toplevel = ctypes.windll.user32.GetAncestor(hwnd, 2)  # GA_ROOT
        out = OUT / f"{key}.png"
        capture(toplevel, out)
        app._pages[key].pack_forget()

    app.destroy()
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
