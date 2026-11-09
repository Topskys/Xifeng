r"""抓取 Web GUI（Edge 应用模式窗口）到 PNG。

MUST 用 Medium 完整性的解释器运行（本机 `python` = D:/env/Python，或 %TEMP%\\vi-venv）：
- Low 进程对 Medium 的 Edge 窗口读不到标题、PrintWindow 被拒（G21 同源）
- Medium 同级则标题可读、PrintWindow 可用，且能代为启动 Edge

    python web_shots.py --launch        # 启动 Edge 并抓帧
    python web_shots.py                 # 只抓帧（窗口已存在时）
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "http://127.0.0.1:8642"

user32 = ctypes.windll.user32


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="")
    p.add_argument("--launch", action="store_true")
    p.add_argument("--hash", default="")
    p.add_argument("--close", action="store_true", help="抓完关掉窗口（便于逐页抓）")
    p.add_argument("--wait", type=float, default=40.0)
    args = p.parse_args()

    out = Path(args.out) if args.out else \
        Path(__file__).resolve().parents[1] / "temp" / "web-shots"
    out.mkdir(parents=True, exist_ok=True)

    if args.launch:
        url = URL + (f"#{args.hash}" if args.hash else "")
        subprocess.Popen([EDGE, f"--app={url}", "--window-size=1180,780"])

    from PIL import Image

    deadline = time.time() + args.wait
    hwnd = None
    while time.time() < deadline:
        hwnd = user32.FindWindowW(None, "VoiceIME")
        if hwnd and user32.IsWindowVisible(hwnd):
            break
        time.sleep(1)
    if not hwnd:
        print("[err] 等待 Edge 应用窗口超时")
        return 1
    # 统一窗口物理尺寸：Edge 的 --window-size 单位是 CSS 像素，dpr=1.5 时
    # 1180 CSS = 1770 物理。MUST 按物理像素 MoveWindow，否则截到的只是局部。
    user32.MoveWindow(hwnd, 60, 60, 1770, 1170, True)
    time.sleep(3)

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    gdi32 = ctypes.windll.gdi32
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    if not user32.PrintWindow(hwnd, mem, 2):  # PW_RENDERFULLCONTENT
        print("[err] PrintWindow 失败")
        return 1

    class BMIH(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    bi = BMIH()
    bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(BMIH), w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1).save(out / "shot-0.png")
    print(f"[shot] {out / 'shot-0.png'} ({w}x{h})")
    if args.close:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
