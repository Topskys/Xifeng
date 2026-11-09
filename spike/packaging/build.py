"""把 spike 打成可双击的 Windows exe（PyInstaller，onedir）。

为什么用 PyInstaller 而不是 Nuitka：Nuitka 要 C 编译器（MSVC 3.5–5GB 或 MinGW 0.5GB），
PyInstaller 纯 Python 就能跑。为什么 onedir 而不是 onefile：onefile 每次启动都要把
~25MB 的 onnxruntime 解压到临时目录，冷启动 +1–2s；onedir 直接就地加载。

为什么模型不进 exe：模型 228MB，onefile 会每次启动解压一遍；外部目录还能单独升级模型。
模型默认放 `<exe 同级>/models/asr/sensevoice-small-int8`（见 `voiceime_spike/paths.py`）。

产物默认写到 `%LOCALAPPDATA%\\VoiceIME-build\\dist`，**不要写在本仓库目录**：本工作区带
Low 完整性标签，从这里生成的 exe 也是 Low，全局键盘钩子会收不到硬件按键（G21/G24）。
要交付就把它复制到任何未打标的目录（如 `C:\\Users\\<你>\\AppData\\Local\\Programs\\VoiceIME`）。

用法：
    uv run python packaging/build.py                    # 只打包程序
    uv run python packaging/build.py --with-models      # 顺手把模型复制到 dist
    uv run python packaging/build.py --out D:\\vi-dist   # 指定输出目录
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

SPIKE_DIR = Path(__file__).resolve().parents[1]
ENTRY = SPIKE_DIR / "run_spike.py"

sys.path.insert(0, str(SPIKE_DIR))
from voiceime_spike import paths  # noqa: E402  复用模型路径解析（模型不进包）


def default_out() -> Path:
    """默认输出目录：`%LOCALAPPDATA%\\VoiceIME-build\\dist`。

    不能用 `tempfile.gettempdir()`：本工作区进程是 Low 完整性，`%TEMP%` 是 Medium
    目录，Low 进程写不进去，`tempfile` 会静默退回当前目录（=Low），exe 就打废了。
    """
    env = os.environ.get("VOICEIME_OUT")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "VoiceIME-build" / "dist"


def default_work() -> Path:
    env = os.environ.get("VOICEIME_WORK")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "VoiceIME-build" / "work"


def dir_is_low(path: Path) -> bool:
    """看目录是否带 Low 完整性标签（icacls 输出含 `Low Mandatory Level`）。"""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        proc = subprocess.run(
            ["icacls", str(probe)], capture_output=True, text=True, timeout=10
        )
        out = proc.stdout or ""
    except Exception:  # noqa: BLE001
        return False
    return "Low Mandatory Level" in out


def build(out: Path, work: Path, with_models: bool, clean: bool) -> Path:
    if not ENTRY.exists():
        raise SystemExit(f"入口不存在: {ENTRY}")

    if dir_is_low(out):
        print(f"[build] ⚠ 输出目录 {out} 带 Low 完整性标签 → exe 继承 Low，全局钩子失效（G21/G24）")
        print("[build]   换一个未打标的目录：--out %LOCALAPPDATA%\\VoiceIME-build\\dist")
        print("[build]   并且必须用 Medium 解释器运行本脚本（见 docs/02-guides/packaging.md）")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "VoiceIME",
        "--onedir",
        "--console",
        "--noconfirm",
        "--paths", str(SPIKE_DIR),
        "--distpath", str(out),
        "--workpath", str(work),
        "--specpath", str(work),
        # sherpa_onnx 的 .pyd + onnxruntime.dll/sherpa-onnx-c-api.dll，sounddevice 的 portaudio DLL
        "--collect-all", "sherpa_onnx",
        "--collect-all", "sounddevice",
        "--collect-all", "_sounddevice_data",
        # 托盘（pystray 的 Win32 后端是运行期动态导入，静态分析看不到）
        "--collect-all", "pystray",
        "--collect-all", "PIL",
        # 本地纠错模型运行时（llama.cpp 的 .pyd/.dll 是运行期 dlopen）
        "--collect-all", "llama_cpp",
        # 打包用不到的东西
        "--exclude-module", "pytest",
        "--exclude-module", "matplotlib",
        "--exclude-module", "PySide6",
        "--exclude-module", "IPython",
        str(ENTRY),
    ]
    if clean:
        cmd.insert(3, "--clean")

    print("[build] " + " ".join(cmd))
    t0 = time.perf_counter()
    rc = subprocess.call(cmd, cwd=str(SPIKE_DIR))
    if rc != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {rc}")
    print(f"[build] PyInstaller 完成，用时 {time.perf_counter() - t0:.1f}s")

    app_dir = out / "VoiceIME"
    if with_models:
        src = SPIKE_DIR.parent / "models" / "asr" / "sensevoice-small-int8"
        dst = app_dir / "models" / "asr" / "sensevoice-small-int8"
        if not src.exists():
            raise SystemExit(f"模型目录不存在: {src}（先跑 fetch_model.py）")
        print(f"[build] 复制模型 {src} → {dst}")
        shutil.copytree(src, dst, dirs_exist_ok=True)

    # Web GUI 前端（vite 构建产物；`--web` 运行时从 <exe 同级>/frontend 读取）
    front = SPIKE_DIR / "frontend" / "dist"
    if front.exists():
        dst_front = app_dir / "frontend"
        print(f"[build] 复制前端 {front} → {dst_front}")
        shutil.copytree(front, dst_front, dirs_exist_ok=True)

    write_launchers(app_dir)
    fix_msvc_runtime(app_dir)
    return app_dir


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
    """用系统里更新的 MSVC 运行库覆盖 PyInstaller 收进来的旧副本。

    为什么：PyInstaller 会从某个依赖里收进一份旧的 `msvcp140.dll`（实测 14.31）放进
    `_internal` 根目录。Windows 优先加载 exe 同目录的副本，于是 14.31 顶掉了系统的 14.42；
    而 llama-cpp-python 的 `ggml-cpu.dll`/`llama.dll` 是 14.40 工具链构建的，混用旧 msvcp
    会在 `llama_backend_init()` 直接 `access violation reading 0x0`（G33，源码形态复现不了）。
    """
    internal = app_dir / "_internal"
    if not internal.is_dir():
        return
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name in CRT_DLLS:
        dst = internal / name
        src = sys32 / name
        if not dst.is_file() or not src.is_file():
            continue
        if file_version(src) > file_version(dst):
            old = file_version(dst)
            shutil.copy2(src, dst)
            print(
                f"[build] 更新 {name}: "
                f"{'.'.join(map(str, old))} → "
                f"{'.'.join(map(str, file_version(src)))}"
            )


def write_launchers(app_dir: Path) -> None:
    """托盘启动器：省得用户记 `--tray`（双击后控制台会自动隐藏）。"""
    (app_dir / "VoiceIME-Tray.cmd").write_text(
        '@echo off\r\nstart "" "%~dp0VoiceIME.exe" --tray\r\n',
        encoding="ascii",
    )


def report(app_dir: Path) -> None:
    exe = app_dir / "VoiceIME.exe"
    files = [p for p in app_dir.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    models = [p for p in app_dir.rglob("*") if "models" in p.parts]

    def mb(n: int) -> str:
        return f"{n / 1024 / 1024:.1f} MB"

    print()
    print(f"[size] exe        {mb(exe.stat().st_size) if exe.exists() else '缺失'}")
    print(f"[size] dist 总计  {mb(total)}（{len(files)} 个文件）")
    if models:
        m = sum(p.stat().st_size for p in models if p.is_file())
        print(f"[size] 其中模型    {mb(m)}")
    top = sorted(
        ((p.stat().st_size, p) for p in files if p.suffix.lower() in {".dll", ".pyd", ".so"}),
        reverse=True,
    )[:5]
    for size, p in top:
        print(f"[size]   {p.name:42s} {mb(size)}")

    # PyInstaller --noconfirm 会把 dist/VoiceIME 整个重建，上次放的模型会没
    if not models:
        src = SPIKE_DIR.parent / "models" / "asr" / "sensevoice-small-int8"
        print("[size] 模型缺失 —— 重新构建会清空 dist，需重新复制：")
        print(f"[size]   xcopy /E /I \"{src}\" \"{app_dir / 'models' / 'asr' / 'sensevoice-small-int8'}\"")
        print("[size]   或下次加 --with-models")
    # 纠错模型不进包：放数据目录，重建 dist/升级程序都不会碰它
    llm = paths.llm_model_path()
    if not llm.is_file():
        print("[size] 纠错模型缺失（可选，不影响语音输入）：")
        print(f"[size]   把 qwen2.5-0.5b-instruct-q4_k_m.gguf 放到 {llm.parent}")
        print("[size]   或用 IME_LLM_MODEL 指定完整路径")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包 VoiceIME（PyInstaller onedir）")
    ap.add_argument("--out", type=Path, default=default_out(), help=f"输出目录（默认 {default_out()}）")
    ap.add_argument("--work", type=Path, default=default_work(), help="中间目录")
    ap.add_argument("--with-models", action="store_true", help="把模型复制到 dist（228MB）")
    ap.add_argument("--no-clean", action="store_true", help="不清理 PyInstaller 缓存（更快）")
    args = ap.parse_args(argv)

    app_dir = build(args.out, args.work, args.with_models, not args.no_clean)
    report(app_dir)
    print()
    print(f"[done] {app_dir / 'VoiceIME.exe'}")
    print("[done] 试跑：VoiceIME.exe --text \"呃 今天 嗯 开会\" --no-inject")
    if not args.with_models:
        print("[done] 模型未打包，运行时设置 IME_MODELS_DIR 指向模型目录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
