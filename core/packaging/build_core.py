"""把 Python 核心打成 sidecar，供 Tauri 安装包内嵌。

和 `packaging/build.py` 的区别：
- `build.py` 打的是**完整可双击产品**（含 GUI/托盘，独立可用）；
- 本脚本打的是**只给 Tauri 壳调用的核心**（`--ipc` 模式），产物进
  `src-tauri/resources/core/`，随 `tauri build` 一起进安装包。

产物结构（Tauri 打包后，onedir 保持 PyInstaller 的 app 目录形态）：
    Xifeng/
      xifeng.exe          ← Tauri 壳
      core/Xifeng-Core/         ← 本脚本产出（onedir）
        Xifeng-Core.exe         ← Python 解释器 + 全部依赖
        _internal/                ← 依赖目录（必须与 exe 同级）
        run_spike.py              ← py.rs 的 core_dir() 靠它认目录

用法：
    uv run python packaging/build_core.py
    uv run python packaging/build_core.py --clean
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1]
REPO = CORE_DIR.parent
sys.path.insert(0, str(CORE_DIR))
from xifeng import paths  # noqa: E402  模型统一根（D15）
ENTRY = CORE_DIR / "run_spike.py"
OUT = REPO / "apps" / "desktop" / "src-tauri" / "resources" / "core"
NAME = "Xifeng-Core"


def build(work: Path, clean: bool) -> Path:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", NAME,
        "--onedir",
        "--console",
        "--noconfirm",
        "--paths", str(CORE_DIR),
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
    rc = subprocess.call(cmd, cwd=str(CORE_DIR))
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


# ---------------------------------------------------------------- G33：CRT 版本覆写
# 与 build.py 同款（G33 的 sidecar 版移植）：PyInstaller 从依赖里收进旧
# `MSVCP140.dll`（实测 14.31），而 llama-cpp-python 的 `ggml-cpu.dll`/`llama.dll`
# 是 14.40 工具链构建的，混用旧 msvcp 会在 `llama_backend_init()` 直接
# `access violation reading 0x0`——**只发生在冻结形态**，源码态复现不了。

CRT_DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll")


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def file_version(path: Path) -> tuple[int, ...]:
    """读 exe/dll 的 FILEVERSION（不依赖 pywin32）。读不到返回 (0,)。"""
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return (0,)
    buf = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buf):
        return (0,)
    ptr = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not ctypes.windll.version.VerQueryValueW(
        buf, "\\", ctypes.byref(ptr), ctypes.byref(length)
    ):
        return (0,)
    if not ptr.value:
        return (0,)
    info = ctypes.cast(ptr, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
    ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def fix_msvc_runtime(app_dir: Path) -> None:
    """用系统里更新的 MSVC 运行库覆盖 PyInstaller 收进来的旧副本（G33）。"""
    internal = app_dir / "_internal"
    if not internal.is_dir():
        return
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name in CRT_DLLS:
        dst = internal / name
        src = sys32 / name
        if not dst.is_file() or not src.is_file():
            continue
        old, new = file_version(dst), file_version(src)
        if new > old:
            shutil.copy2(src, dst)
            print(f"[core] CRT 更新 {name}: {'.'.join(map(str, old))} → {'.'.join(map(str, new))}")
        else:
            print(f"[core] CRT 已够新，跳过 {name}（{'.'.join(map(str, old))}）")


MODELS_OUT = OUT.parent / "models"  # resources/models → 安装目录 models/


def copy_models() -> None:
    """把**模型统一根**（`%LOCALAPPDATA%\\Xifeng\\models`）复制进 Tauri 资源。

    （D15，2026-11-11）**默认不复制**：安装器不带模型（2026-11-11 用户决策），
    运行时从统一根 `%LOCALAPPDATA%\\Xifeng\\models` 读。只有要出
    「离线开箱即用」包时才传 `--with-models`，产物随 `tauri build` 进
    安装目录 `models/` 作就地兜底（安装目录只读也能用；
    用户在统一根放模型时优先级更高）。
    """
    src_root = paths.models_root()
    jobs = [
        (src_root / "asr", MODELS_OUT / "asr"),
        (src_root / "vad" / "silero_vad.onnx", MODELS_OUT / "vad" / "silero_vad.onnx"),
    ]
    gguf = sorted((src_root / "llm").glob("*.gguf"))
    if gguf:
        jobs.append((gguf[0], MODELS_OUT / "llm" / gguf[0].name))
    done = 0
    for src, dst in jobs:
        if not src.exists():
            print(f"[core] 跳过（源不存在）：{src}")
            continue
        if dst.exists() and dst.stat().st_size == (
            src.stat().st_size if src.is_file() else sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
        ):
            print(f"[core] 已是最新，跳过：{dst.relative_to(OUT.parent)}")
            done += 1
            continue
        t0 = time.perf_counter()
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            shutil.copytree(src, dst, dirs_exist_ok=True)
        print(f"[core] 复制 {src.name} -> {dst.relative_to(OUT.parent)}"
              f"（{time.perf_counter() - t0:.1f}s）")
        done += 1
    print(f"[core] 模型 {done}/{len(jobs)} 就绪（{size_of(MODELS_OUT):.1f} MB -> 安装目录 models/）")


def size_of(path: Path) -> float:
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return total / (1024 * 1024)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=str(REPO / ".tauri-work" / "pycore"))
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--with-models", action="store_true",
                    help="把统一根模型复制进 resources/models（离线开箱即用包；"
                         "默认不带，安装器不含 700MB 模型）")
    args = ap.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    if args.clean and OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)

    app = build(work, args.clean)
    copy_entry(app)
    fix_msvc_runtime(app)
    if args.with_models:
        copy_models()
    else:
        print("[core] 默认不带模型（统一根提供；--with-models 可打离线包）")
    print(f"[core] 产物 {app}（{size_of(app):.1f} MB）")
    print("[core] 下一步：仓库根跑 pnpm tauri:build（会把它打进安装包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
