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
    """ASR 模型目录，优先级：`IME_MODELS_DIR` > 统一根 > 旧布局兼容探测。

    统一根（D15，2026-11-11）：`data_dir()/models`，即
    `%LOCALAPPDATA%\\VoiceIME\\models\\asr\\sensevoice-small-int8`；
    旧布局（打包态 exe 同级 / 源码态仓库根）**已有模型时仍优先于统一根缺失的情况**，
    保证升级不丢模型、不强制搬 228MB。
    """
    env = os.environ.get("IME_MODELS_DIR")
    if env:
        return Path(env)
    for cand in _asr_candidates():
        if cand.is_dir():
            return cand
    return _asr_candidates()[0]


def _asr_candidates() -> tuple[Path, ...]:
    return (
        models_root() / MODEL_SUBDIR.relative_to("models"),
        *(root / MODEL_SUBDIR.relative_to("models") for root in _legacy_models_roots()),
    )


def _legacy_models_roots() -> tuple[Path, ...]:
    """旧布局的 models 根（统一根之前的兼容探测位）。

    - 打包态：`<exe 同级>/models`（安装包自带，见 G24）
    - 源码态：`<仓库根>/models`
    两个位置相同时去重。
    """
    repo = Path(__file__).resolve().parents[2] / "models"
    exe = app_dir() / "models"
    return (exe, repo) if is_frozen() and exe != repo else ((repo, exe) if repo != exe else (repo,))


def models_root() -> Path:
    """ASR/VAD/LLM 统一的模型根目录（D15，2026-11-11）。

    优先级：

    1. `IME_MODELS_ROOT` 环境变量（便携 U 盘 / 自定义盘符）
    2. `%LOCALAPPDATA%\\VoiceIME\\models`——**统一位置，唯一存放点**。
       注意这里只做**只读探测**：Low 完整性进程（沙箱调试）写不进
       `%LOCALAPPDATA%` 但读得到（G25 是写数据的规矩），模型只用读，
       所以开发/调试/打包后都从这一处取，不走 `data_dir()` 的可写探针。
    3. `data_dir()/models`——统一根的兜底位（LOCALAPPDATA 不可用时模型随数据落
       exe 同级/`.data`，新下载的模型也落这里）。
    """
    env = os.environ.get("IME_MODELS_ROOT")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "VoiceIME" / "models"
        if _has_models(candidate):
            return candidate
    return data_dir() / "models"


def _has_models(root: Path) -> bool:
    """统一根里有没有任何一类模型（只读探测，不要求可写）。"""
    if not root.is_dir():
        return False
    if (root / "asr" / "sensevoice-small-int8").is_dir():
        return True
    if (root / "vad" / "silero_vad.onnx").is_file():
        return True
    return any((root / "llm").glob("*.gguf"))


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
    """Silero VAD 模型路径；`IME_VAD_MODEL` 可覆盖整个文件路径。

    优先级同 ASR：统一根（`data_dir()/models/vad`）→ 旧布局（exe 同级 / 仓库根）。
    """
    env = os.environ.get("IME_VAD_MODEL")
    if env:
        return Path(env)
    cands = (
        models_root() / VAD_SUBDIR.relative_to("models"),
        *(root / VAD_SUBDIR.relative_to("models") for root in _legacy_models_roots()),
    )
    for cand in cands:
        if cand.is_file():
            return cand
    return cands[0]


def llm_model_path() -> Path:
    """纠错模型（GGUF）路径；`IME_LLM_MODEL` 可覆盖整个文件路径。

    优先级同 ASR/VAD（D15 统一根）：`data_dir()/models/llm` → 安装包自带
    （`<exe 同级>/models/llm`，2026-11-11 起打包时统一复制）→ 源码态仓库根。
    模型在统一根下时重建 dist / 升级都不会被覆盖或丢失（G24/G25 同理）。
    """
    env = os.environ.get("IME_LLM_MODEL")
    if env:
        return Path(env)
    cands = (
        models_root() / LLM_SUBDIR.relative_to("models"),
        *(root / LLM_SUBDIR.relative_to("models") for root in _legacy_models_roots()),
    )
    for cand in cands:
        if cand.is_file():
            return cand
    return cands[0]
