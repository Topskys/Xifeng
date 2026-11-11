"""把 Python 核心打成 sidecar，供 Tauri 安装包内嵌。

和 `packaging/build.py` 的区别：
- `build.py` 打的是**完整可双击产品**（含 GUI/托盘，独立可用）；
- 本脚本打的是**只给 Tauri 壳调用的核心**（`--ipc` 模式），产物进
  `src-tauri/resources/core/`，随 `tauri build` 一起进安装包。

产物结构（Tauri 打包后，onedir 保持 PyInstaller 的 app 目录形态）：
    VoiceIME/
      voiceime-tauri.exe          ← Tauri 壳
      core/VoiceIME-Core/         ← 本脚本产出（onedir）
        VoiceIME-Core.exe         ← Python 解释器 + 全部依赖
        _internal/                ← 依赖目录（必须与 exe 同级）
        run_spike.py              ← py.rs 的 core_dir() 靠它认目录

用法：
    uv run python packaging/build_core.py
    uv run python packaging/build_core.py --clean
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parents[1]
REPO = SPIKE_DIR.parent
ENTRY = SPIKE_DIR / "run_spike.py"
OUT = SPIKE_DIR / "frontend" / "src-tauri" / "resources" / "core"
NAME = "VoiceIME-Core"


def build(work: Path, clean: bool) -> Path:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", NAME,
        "--onedir",
        "--console",
        "--noconfirm",
        "--paths", str(SPIKE_DIR),
        "--distpath", str(OUT),
        "--workpath", str(work),
        "--specpath", str(work),
        # 运行期 dlopen 的依赖，静态分析看不到
        "--collect-all", "sherpa_onnx",
        "--collect-all", "sounddevice",
        "--collect-all", "_sounddevice_data",
        "--collect-all", "pystray",
        "--collect-all", "PIL",
        "--collect-all", "llama_cpp",
        # IPC 模式用不到
        "--exclude-module", "pytest",
        "--exclude-module", "matplotlib",
        "--exclude-module", "PySide6",
        "--exclude-module", "IPython",
        # 注意：**不要**排除 tkinter。`popup.py` 在模块顶层就 `import tkinter`，
        # 而 `run_spike.py` 无条件导入 popup，排掉会让核心一启动就 ImportError。
        # tkinter 只占几 MB，不值得为它冒这个险。
        str(ENTRY),
    ]
    if clean:
        cmd.insert(3, "--clean")
    print("[core] " + " ".join(cmd))
    t0 = time.perf_counter()
    rc = subprocess.call(cmd, cwd=str(SPIKE_DIR))
    if rc != 0:
        raise SystemExit(f"[core] PyInstaller 失败，退出码 {rc}")
    print(f"[core] PyInstaller 完成，用时 {time.perf_counter() - t0:.1f}s")

    app = OUT / NAME
    if not (app / f"{NAME}.exe").is_file():
        raise SystemExit(f"[core] 找不到产物 {app / (NAME + '.exe')}")
    return app


def copy_entry(app: Path) -> None:
    """核心目录必须含 run_spike.py —— py.rs 的 core_dir() 用它认目录。"""
    dst = app / "run_spike.py"
    shutil.copy2(ENTRY, dst)
    print(f"[core] 放入判据文件 {dst.relative_to(OUT.parent)}")


def size_of(path: Path) -> float:
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return total / (1024 * 1024)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=str(REPO / ".tauri-work" / "pycore"))
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    if args.clean and OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)

    app = build(work, args.clean)
    copy_entry(app)
    print(f"[core] 产物 {app}（{size_of(app):.1f} MB）")
    print("[core] 下一步：cd spike/frontend && pnpm tauri build（会把它打进安装包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
