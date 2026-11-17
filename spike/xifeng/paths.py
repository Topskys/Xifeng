"""路径解析：源码运行 vs PyInstaller 冻结运行。

为什么单独拿出来：源码里用 `__file__.parents[2]` 找模型，打包后 `__file__` 指向
`_MEIPASS` 里的临时目录，同一行代码会指向错误位置（G24）。所有"资源在哪"的判断
集中在这里，打包与源码共用同一套函数。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

MODEL_SUBDIR = Path("models") / "asr" / "sensevoice-small-int8"
VAD_SUBDIR = Path("models") / "vad" / "silero_vad.onnx"
LLM_SUBDIR = Path("models") / "llm" / "qwen2.5-0.5b-instruct-q4_k_m.gguf"

# 改名（D21，2026-11-16）：目录/库/日志从 VoiceIME 换成 Xifeng。
# 旧名常量只用于**迁移探测**，不要在新代码里拿它当默认值。
APP_DIR_NAME = "Xifeng"
LEGACY_APP_DIR_NAME = "VoiceIME"
DB_NAME = "xifeng.db"
LEGACY_DB_NAME = "voiceime.db"
LOG_NAME = "xifeng.log"
LEGACY_LOG_NAME = "voiceime.log"


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
    `%LOCALAPPDATA%\\Xifeng\\models\\asr\\sensevoice-small-int8`；
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
    2. `%LOCALAPPDATA%\\Xifeng\\models`——**统一位置，唯一存放点**。
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
        candidate = Path(local) / APP_DIR_NAME / "models"
        if _has_models(candidate):
            return candidate
        # 迁移失败时（旧库/旧目录被占用）仍能只读旧模型，不让用户重下 228MB
        legacy = Path(local) / LEGACY_APP_DIR_NAME / "models"
        if _has_models(legacy):
            return legacy
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
    2. `%LOCALAPPDATA%\\Xifeng`（可写时；源码与冻结形态共用）
    3. 冻结形态：`<exe 同级>/data`（便携版、安装到不可写目录时）
    4. 源码形态：`<spike>/.data`（Low 进程唯一可写的位置）

    为什么先 `%LOCALAPPDATA%` 而不是 exe 同级：安装到 `Program Files` 时 exe 目录
    不可写；而且升级时整个目录会被替换，历史会跟着丢。

    注意：这里只解析路径，**不做迁移**——迁移会改磁盘，必须由入口显式调用
    [`migrate_legacy_data`]，否则打包脚本/测试读一次路径就动用户数据。
    """
    env = os.environ.get("IME_DATA_DIR")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / APP_DIR_NAME
        if _usable(candidate):
            return candidate
    return app_dir() / ("data" if is_frozen() else ".data")


def db_path() -> Path:
    """默认数据库文件；`IME_DB` 可直接指定整个文件路径。"""
    env = os.environ.get("IME_DB")
    if env:
        return Path(env)
    directory = data_dir()
    for name in (DB_NAME, LEGACY_DB_NAME):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    # 迁移失败（旧目录被占用等）时直接继续用旧库，别让历史突然变空
    legacy = _legacy_data_dir()
    if legacy is not None and (legacy / LEGACY_DB_NAME).is_file():
        return legacy / LEGACY_DB_NAME
    return directory / DB_NAME


def _legacy_data_dir() -> Path | None:
    """旧名数据目录（仅迁移/兜底用）。显式 `IME_DATA_DIR` 时不参与。"""
    if os.environ.get("IME_DATA_DIR"):
        return None
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return Path(local) / LEGACY_APP_DIR_NAME


def migrate_legacy_data(localappdata: Path | None = None) -> list[str]:
    """把旧 `%LOCALAPPDATA%\\VoiceIME` 迁到 `Xifeng`（D21）。

    设计成**幂等、只增不删**：

    - `IME_DATA_DIR` 显式指定时完全不动（用户已经接管位置）
    - 新目录不存在：整目录 `rename`（同卷瞬时，228MB 模型也不复制）；
      跨卷/被占用导致 rename 失败时退回 `copytree`，并把旧目录留作备份
    - 新目录已存在：只补旧目录里**缺的**条目，绝不覆盖新目录里的同名文件
    - 最后把 `voiceime.db`（含 `-wal`/`-shm`）与 `voiceime.log` 改名为新名
    - 任何一步失败都只打印告警并继续：迁移失败绝不能拦住语音输入

    返回已执行的动作列表（给日志/测试断言用，不写用户文本）。
    """
    if os.environ.get("IME_DATA_DIR"):
        return []
    base = (
        Path(localappdata)
        if localappdata is not None
        else (Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else None)
    )
    if base is None:
        return []

    old = base / LEGACY_APP_DIR_NAME
    new = base / APP_DIR_NAME
    actions: list[str] = []

    if old.is_dir():
        # data_dir() 的 _usable() 可能已经把新目录建出来（空壳）：先删掉空壳，
        # 这样仍能走 rename（否则会退化成复制 228MB 模型）。
        if new.is_dir() and not any(new.iterdir()):
            try:
                new.rmdir()
                actions.append("removed-empty-target")
            except OSError:
                pass
        if not new.exists():
            try:
                old.rename(new)
                actions.append("renamed")
            except OSError:
                try:
                    shutil.copytree(old, new, dirs_exist_ok=True)
                    actions.append("copied")
                except OSError as exc:
                    print(f"[paths] ⚠ 旧数据迁移失败（{exc}），继续使用旧目录", file=sys.stderr)
                    return actions
        else:
            # 目标已存在（例如用户已经用过新版）：只补缺，不覆盖。
            # 旧库/旧日志不在这里复制——下面会按"新库不存在才接管"的规则处理，
            # 否则目标里会多出一份 voiceime.db 残影。
            skip = {
                LEGACY_DB_NAME,
                *(LEGACY_DB_NAME + s for s in ("-wal", "-shm")),
                LEGACY_LOG_NAME,
            }
            for child in old.iterdir():
                if child.name in skip:
                    continue
                dst = new / child.name
                if dst.exists():
                    continue
                try:
                    if child.is_dir():
                        shutil.copytree(child, dst)
                    else:
                        shutil.copy2(child, dst)
                    actions.append(f"copied:{child.name}")
                except OSError as exc:
                    print(f"[paths] ⚠ 迁移 {child.name} 失败：{exc}", file=sys.stderr)

    if new.is_dir():
        if not _rename_if_missing(new / LEGACY_DB_NAME, new / DB_NAME):
            # 旧库改名失败但新库也不存在时，复制一份（旧库保留）
            _copy_if_missing(new / LEGACY_DB_NAME, new / DB_NAME)
        for suffix in ("-wal", "-shm"):
            _rename_if_missing(new / (LEGACY_DB_NAME + suffix), new / (DB_NAME + suffix))
        _rename_if_missing(new / LEGACY_LOG_NAME, new / LOG_NAME)
    return actions


def _rename_if_missing(src: Path, dst: Path) -> bool:
    """`src` 存在且 `dst` 不存在时改名；返回是否真的改了。"""
    if not src.exists() or dst.exists():
        return False
    try:
        src.rename(dst)
        return True
    except OSError as exc:
        print(f"[paths] ⚠ 改名 {src.name} → {dst.name} 失败：{exc}", file=sys.stderr)
        return False


def _copy_if_missing(src: Path, dst: Path) -> bool:
    if not src.is_file() or dst.exists():
        return False
    try:
        shutil.copy2(src, dst)
        return True
    except OSError as exc:
        print(f"[paths] ⚠ 复制 {src.name} → {dst.name} 失败：{exc}", file=sys.stderr)
        return False


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
