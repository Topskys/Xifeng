"""路径解析：源码运行 vs PyInstaller 冻结运行。

为什么单独拿出来：源码里用 `__file__.parents[2]` 找模型，打包后 `__file__` 指向
`_MEIPASS` 里的临时目录，同一行代码会指向错误位置（G24）。所有"资源在哪"的判断
集中在这里，打包与源码共用同一套函数。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MODEL_SUBDIR = Path("models") / "asr" / "sensevoice-small-int8"
VAD_SUBDIR = Path("models") / "vad" / "silero_vad.onnx"
LLM_SUBDIR = Path("models") / "llm" / "qwen2.5-0.5b-instruct-q4_k_m.gguf"


def is_frozen() -> bool:
    """是否是 PyInstaller 打出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """exe 所在目录；源码运行时是 `spike/`。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def models_dir() -> Path:
    """模型目录，优先级：`IME_MODELS_DIR` > 随包目录 > 源码默认。

    - 打包形态：`<exe 同级>/models/asr/sensevoice-small-int8`（模型不进 exe，见 G24）
    - 源码形态：`<仓库根>/models/asr/sensevoice-small-int8`
    """
    env = os.environ.get("IME_MODELS_DIR")
    if env:
        return Path(env)
    if is_frozen():
        return app_dir() / MODEL_SUBDIR
    return Path(__file__).resolve().parents[2] / MODEL_SUBDIR


def _usable(directory: Path) -> bool:
    """能建目录、能写文件才算可用。

    Low 完整性进程对 `%LOCALAPPDATA%` 是 no-write-up，`mkdir` 会抛 PermissionError；
    直接用它会让历史/术语全丢，所以先探一次（G25）。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """SQLite 等可写数据的目录。

    优先级：

    1. `IME_DATA_DIR`
    2. `%LOCALAPPDATA%\\VoiceIME`（可写时；源码与冻结形态共用）
    3. 冻结形态：`<exe 同级>/data`（便携版、安装到不可写目录时）
    4. 源码形态：`<spike>/.data`（Low 进程唯一可写的位置）

    为什么先 `%LOCALAPPDATA%` 而不是 exe 同级：安装到 `Program Files` 时 exe 目录
    不可写；而且升级时整个目录会被替换，历史会跟着丢。
    """
    env = os.environ.get("IME_DATA_DIR")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "VoiceIME"
        if _usable(candidate):
            return candidate
    return app_dir() / ("data" if is_frozen() else ".data")


def db_path() -> Path:
    """默认数据库文件；`IME_DB` 可直接指定整个文件路径。"""
    env = os.environ.get("IME_DB")
    if env:
        return Path(env)
    return data_dir() / "voiceime.db"


def vad_model_path() -> Path:
    """Silero VAD 模型路径；`IME_VAD_MODEL` 可覆盖整个文件路径。"""
    env = os.environ.get("IME_VAD_MODEL")
    if env:
        return Path(env)
    if is_frozen():
        return app_dir() / VAD_SUBDIR
    return Path(__file__).resolve().parents[2] / VAD_SUBDIR


def llm_model_path() -> Path:
    """纠错模型（GGUF）路径；`IME_LLM_MODEL` 可覆盖整个文件路径。

    模型 **不进交付包**：默认放 `%LOCALAPPDATA%\\VoiceIME\\models\\llm`，与 SQLite 同处
    数据目录 —— 重建 dist、升级程序都不会把 491MB 再复制一遍，也不会被 dist 一起打包
    （G24/G25 同理）。兼容旧布局：`<exe 同级>/models/llm` 里已有文件时仍然用它。
    """
    env = os.environ.get("IME_LLM_MODEL")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2] / LLM_SUBDIR
    user = data_dir() / LLM_SUBDIR
    candidates = (user, app_dir() / LLM_SUBDIR) if is_frozen() else (repo, user)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]
