r"""把 GUI 逐页渲染成 PNG，供视觉检视（不需要真实屏幕前台）。

    uv run python ui_shots.py --out %TEMP%\ui-shots

原理：PrintWindow(PW_RENDERFULLCONTENT) 把窗口内容画进内存位图，
不要求窗口在前台，也不依赖屏幕截图权限。默认用真实库的**副本**，
所以历史/统计页有真数据，又不弄脏用户数据。
"""

from __future__ import annotations

import argparse
import ctypes
import shutil
import sqlite3
import sys
from ctypes import wintypes
from pathlib import Path


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def capture_hwnd(hwnd: int, path: Path) -> None:
    """按 Win32 句柄抓帧（Edge 应用窗口等非 Tk 窗口也适用）。"""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"窗口尺寸异常 {w}x{h}：{path}")
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, 2) or user32.PrintWindow(hwnd, mem, 1)
    if not ok:
        raise RuntimeError(f"PrintWindow 失败：{path}")
    bi = BITMAPINFOHEADER()
    bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(BITMAPINFOHEADER), w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    from PIL import Image

    Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1).save(path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)


def capture_toplevel(win, path: Path) -> None:
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
    capture_hwnd(hwnd, path)


def snapshot_db(real: Path | None, workdir: Path) -> Path:
    """把真实库复制到 temp 目录，检视用副本，不碰用户数据。"""
    tmp = workdir / "snapshot.db"
    if tmp.exists():
        tmp.unlink()
    if real and real.exists():
        # 不要用 sqlite3 打开源库：WAL 库以只读打开仍要建 -shm，Low 进程会失败（G25）。
        # 直接复制文件（连同热 wal/shm），在副本上做备份合并。
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(real) + suffix)
            if f.exists():
                shutil.copyfile(f, Path(str(tmp) + suffix))
        src = sqlite3.connect(str(tmp))
        merged = sqlite3.connect(":memory:")
        src.backup(merged)
        merged.close(); src.close()
    return tmp


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="", help="默认 <仓库根>/temp/ui-shots")
    p.add_argument("--themes", default="dark,light")
    p.add_argument("--size", default="", help="如 1000x660；默认 1180x760")
    args = p.parse_args(argv)

    from voiceime_spike import gui, paths, ui
    from voiceime_spike import settings as user_settings

    # 临时产物一律进 <仓库根>/temp/
    out = Path(args.out) if args.out else \
        Path(__file__).resolve().parents[1] / "temp" / "ui-shots"
    out.mkdir(parents=True, exist_ok=True)
    db = snapshot_db(paths.db_path(), out)

    geometry = f"{args.size}+40+40" if args.size else "1180x760+40+40"
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    app = gui.App(str(db), tab="stats")
    app.geometry(geometry)
    app.update_idletasks()

    for theme in themes:
        app.settings = user_settings.update(app.settings, theme=theme)
        app.apply_theme()
        app.update_idletasks()
        for tab in ("stats", "models", "config", "history"):
            app._show(tab)
            app.update_idletasks()
            app.after(150, app.update)   # 让瀑布图/卡片完成一次布局
            app.update()
            capture_toplevel(app, out / f"{theme}-{tab}.png")
            print(f"[shot] {theme}-{tab}")
        app._show("history")
        app.update_idletasks()
        rows = app.store.recent(1)
        if rows:
            win = app._pages["history"]._detail(rows[0])
            win.update_idletasks()
            win.update()
            app.after(150, app.update)
            app.update()
            capture_toplevel(win, out / f"{theme}-detail.png")
            win.destroy()
            print(f"[shot] {theme}-detail")

    app.store.close()
    app.destroy()
    print(f"[done] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
