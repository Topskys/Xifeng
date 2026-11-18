"""按窗口标题截图（Win32 PrintWindow，不依赖屏幕是否可见）。

用法：
    uv run python tools/shot.py --title Xifeng --out ../.shots/tauri.png
    uv run python tools/shot.py --title Xifeng --out ../.shots/x.png --wait 30

为什么用 PrintWindow 而不是抓屏：本机是远程/无人值守环境，窗口可能被遮挡或不在
前台；PrintWindow 让窗口自己重绘到位图里，结果稳定。
"""

from __future__ import annotations

import argparse
import ctypes
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def find_window(title_part: str, timeout: float) -> int:
    """按标题子串找可见窗口，等 timeout 秒。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found: list[tuple[int, str]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(hwnd, _lparam):  # noqa: ANN001
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_part.lower() in buf.value.lower():
                found.append((hwnd, buf.value))
            return True

        user32.EnumWindows(enum_proc, 0)
        if found:
            hwnd, title = found[0]
            print(f"[shot] 找到窗口 hwnd={hwnd} title={title!r}")
            return hwnd
        time.sleep(0.4)
    raise SystemExit(f"[shot] {timeout}s 内没找到标题含 {title_part!r} 的可见窗口")


class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wintypes.DWORD * 3)]


def capture(hwnd: int, out: Path, settle: float = 2.0) -> None:
    """截窗口位图。settle 秒内多截几次，取内容最“重”的那张（首帧常是白屏）。

    注意 DPI：本机 dpr=1.5，`GetWindowRect` 给的是逻辑像素，而窗口实际按物理像素
    渲染。若按 GetWindowRect 的尺寸建位图，右侧/底部会少截一条（看起来像内容被
    “截断”，其实是位图不够大）。所以优先用 `DwmGetWindowAttribute` 的扩展边框
    真实尺寸，并用 `PW_RENDERFULLCONTENT` 让 WebView2 自绘内容进图。
    """
    from PIL import Image

    def _rect() -> tuple[int, int]:
        # DWMWA_EXTENDED_FRAME_BOUNDS = 9：拿到含 DPI 缩放的真实物理尺寸
        try:
            r = wintypes.RECT()
            hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)
            )
            if hr == 0 and r.right > r.left and r.bottom > r.top:
                return r.right - r.left, r.bottom - r.top
        except Exception:  # noqa: BLE001
            pass
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.right - r.left, r.bottom - r.top

    best: tuple[int, Image.Image] | None = None
    deadline = time.time() + settle
    while True:
        w, h = _rect()
        if w <= 0 or h <= 0:
            raise SystemExit(f"[shot] 窗口尺寸异常 {w}x{h}")

        hdc = user32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        user32.PrintWindow(hwnd, mdc, 2)

        bi = _BMI()
        bi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)

        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        colors = len(img.convert("P", palette=Image.ADAPTIVE, colors=64).getcolors(64 * 1024) or [])
        if best is None or colors > best[0]:
            best = (colors, img)
        if time.time() >= deadline:
            break
        time.sleep(0.35)

    assert best is not None
    colors, img = best
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"[shot] 已保存 {out}（{img.width}x{img.height}，{img.mode}，色块 {colors}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="Xifeng")
    ap.add_argument("--out", required=True)
    ap.add_argument("--wait", type=float, default=40.0, help="等窗口出现的秒数")
    ap.add_argument("--settle", type=float, default=3.0, help="等页面渲染的秒数")
    args = ap.parse_args()

    hwnd = find_window(args.title, args.wait)
    # 置前，保证 PrintWindow 拿到完整内容
    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    capture(hwnd, Path(args.out), settle=args.settle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
