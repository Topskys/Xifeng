"""生成统一应用图标（exe / 任务栏 / favicon）：黑色圆角方块 + 白色麦克风。

设计基准是 `voiceime_spike/tray.py:_image()`（64px：黑圆角方块 + 1px 浅灰描边 +
白麦克风）。本脚本把同一几何参数放大 16 倍画在 1024 母版上，再缩小导出全套
`src-tauri/icons/` 与 `frontend/public/favicon.svg` 对应的位图。

两处必须保持一致（G 系风格：像 postprocess parity 一样，改一边必须改另一边）：
- tray.py:_image()        —— 托盘运行期画
- tools/gen_icons.py      —— 本脚本，静态导出
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

# 64px 基准几何 ×16 = 1024 母版。颜色与 tray._image() 相同。
SIZE = 1024
BG = (24, 24, 27, 255)        # zinc-900
OUTLINE = (212, 212, 216, 255)  # zinc-300
FG = (255, 255, 255, 255)


def master() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角方块：64px 基准 (2,2,61,61) r=14 描边2 → ×16
    d.rounded_rectangle((32, 32, 976, 976), radius=224, fill=BG, outline=OUTLINE, width=32)
    # 麦克风头：64px (25,13,39,38) r=7 → ×16
    d.rounded_rectangle((400, 208, 624, 608), radius=112, fill=FG)
    # 支架弧：64px bbox (17,24,47,50) 下半圆 width4 → ×16
    d.arc((272, 384, 752, 800), start=0, end=180, fill=FG, width=64)
    # 杆与底座：64px (32,48)-(32,55) / (23,55)-(41,55) → ×16
    d.line((512, 768, 512, 880), fill=FG, width=64)
    d.line((368, 880, 656, 880), fill=FG, width=64)
    return img


def export(img: Image.Image, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        "32x32.png": 32,
        "64x64.png": 64,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "256x256.png": 256,
        "512x512.png": 512,
        "icon.png": 512,
    }
    for name, size in targets.items():
        img.resize((size, size), Image.LANCZOS).save(out_dir / name)

    # Windows 任务栏/资源管理器用：多尺寸 ico
    img.resize((256, 256), Image.LANCZOS).save(
        out_dir / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    # favicon（SVG 版，几何与母版同参数，viewport 64 对齐 tray._image() 基准）
    public = out_dir.parent / "frontend" / "public"
    public.mkdir(parents=True, exist_ok=True)
    (public / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
        '<rect x="2" y="2" width="59" height="59" rx="14" fill="#18181B" stroke="#D4D4D8" stroke-width="2"/>'
        '<rect x="25" y="13" width="14" height="25" rx="7" fill="#fff"/>'
        '<path d="M17 37 A15 13 0 0 0 47 37" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"/>'
        '<line x1="32" y1="48" x2="32" y2="55" stroke="#fff" stroke-width="4" stroke-linecap="round"/>'
        '<line x1="23" y1="55" x2="41" y2="55" stroke="#fff" stroke-width="4" stroke-linecap="round"/>'
        "</svg>",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "frontend" / "src-tauri" / "icons",
    )
    args = ap.parse_args()
    export(master(), args.out)
    print(f"[icons] 已写入 {args.out}（7 个 png + icon.ico + favicon.svg）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
