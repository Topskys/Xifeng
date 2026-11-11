"""路径解析单测：统一根（D14）与旧布局兼容各自指向正确位置。

模型统一根（2026-11-11）：`%LOCALAPPDATA%\VoiceIME\models`，仓库里不再放模型；
旧位置（exe 同级 / 仓库根）只在**已有模型**时作为兼容兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

from voiceime_spike import paths


def _freeze(monkeypatch, exe_dir: Path) -> None:
    """把当前进程伪装成 `<exe_dir>/VoiceIME.exe` 的冻结形态。"""
    exe_dir.mkdir(parents=True, exist_ok=True)
    exe = exe_dir / "VoiceIME.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))


# ---------------------------------------------------------------- 模型统一根

def test_models_root_env_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IME_MODELS_ROOT", str(tmp_path / "m"))
    assert paths.models_root() == tmp_path / "m"


def test_models_root_is_localappdata_when_it_has_models(monkeypatch, tmp_path: Path) -> None:
    """统一位置有模型就用它——只读探测，Low 进程（沙箱调试）也能用。"""
    local = tmp_path / "local"
    root = local / "VoiceIME" / "models"
    (root / "asr" / "sensevoice-small-int8").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("IME_MODELS_ROOT", raising=False)
    assert paths.models_root() == root


def test_models_root_falls_back_to_data_dir(monkeypatch, tmp_path: Path) -> None:
    """统一位置还没有模型（全新机器）时，落数据目录 models/。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("IME_MODELS_ROOT", raising=False)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    assert paths.models_root() == tmp_path / "data" / "models"


def test_has_models_accepts_llm_and_vad_too(monkeypatch, tmp_path: Path) -> None:
    """只放了纠错或 VAD 也算「统一根已有模型」（三类任意一类即可）。"""
    monkeypatch.delenv("IME_MODELS_ROOT", raising=False)
    root = tmp_path / "VoiceIME" / "models"
    (root / "llm").mkdir(parents=True)
    (root / "llm" / "q.gguf").write_bytes(b"x")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.models_root() == root

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "other"))
    root2 = tmp_path / "other" / "VoiceIME" / "models"
    (root2 / "vad").mkdir(parents=True)
    (root2 / "vad" / "silero_vad.onnx").write_bytes(b"x")
    assert paths.models_root() == root2


# ---------------------------------------------------------------- ASR

def test_asr_env_override_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IME_MODELS_DIR", str(tmp_path))
    assert paths.models_dir() == tmp_path


def test_asr_prefers_unified_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IME_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "models_root", lambda: tmp_path / "root")
    (tmp_path / "root" / "asr" / "sensevoice-small-int8").mkdir(parents=True)
    assert paths.models_dir() == tmp_path / "root" / "asr" / "sensevoice-small-int8"


def test_asr_falls_back_to_exe_side_when_frozen(monkeypatch, tmp_path: Path) -> None:
    """统一根没有、但安装目录 models/ 自带（打包兜底）时用安装目录的。"""
    monkeypatch.delenv("IME_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "models_root", lambda: tmp_path / "root")  # 空
    app = tmp_path / "app"
    _freeze(monkeypatch, app)
    legacy = app / "models" / "asr" / "sensevoice-small-int8"
    legacy.mkdir(parents=True)
    assert paths.models_dir() == legacy


def test_asr_default_points_to_unified_root_when_nothing_exists(monkeypatch, tmp_path: Path) -> None:
    """全新机器：统一根还没有 → 默认目标就是统一根（等模型下载/放进去）。"""
    monkeypatch.delenv("IME_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "models_root", lambda: tmp_path / "root")
    monkeypatch.setattr(paths, "_legacy_models_roots", lambda: ())
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.models_dir() == tmp_path / "root" / "asr" / "sensevoice-small-int8"


# ---------------------------------------------------------------- VAD / LLM

def test_vad_prefers_unified_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IME_VAD_MODEL", raising=False)
    monkeypatch.setattr(paths, "models_root", lambda: tmp_path / "root")
    vad = tmp_path / "root" / "vad" / "silero_vad.onnx"
    vad.parent.mkdir(parents=True)
    vad.write_bytes(b"x")
    assert paths.vad_model_path() == vad


def test_llm_prefers_unified_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IME_LLM_MODEL", raising=False)
    monkeypatch.setattr(paths, "models_root", lambda: tmp_path / "root")
    gguf = tmp_path / "root" / "llm" / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_bytes(b"gguf")
    assert paths.llm_model_path() == gguf


def test_llm_missing_points_to_unified_root(monkeypatch, tmp_path: Path) -> None:
    """缺失时也指向统一根（提示用户放这里）。"""
    monkeypatch.delenv("IME_LLM_MODEL", raising=False)
    monkeypatch.setattr(paths, "models_root", lambda: tmp_path / "root")
    monkeypatch.setattr(paths, "_legacy_models_roots", lambda: ())
    assert paths.llm_model_path() == tmp_path / "root" / "llm" / "qwen2.5-0.5b-instruct-q4_k_m.gguf"


def test_llm_falls_back_to_exe_dir(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """统一根没有、旧布局（exe 同级 models/llm）有 → 用旧布局。"""
    monkeypatch.delenv("IME_LLM_MODEL", raising=False)
    monkeypatch.setattr(paths, "models_root", lambda: tmp_path / "root")  # 空
    app = tmp_path / "app"
    _freeze(monkeypatch, app)
    legacy = app / paths.LLM_SUBDIR
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"gguf")
    assert paths.llm_model_path() == legacy


# ---------------------------------------------------------------- 数据目录

def test_app_dir_is_exe_dir_when_frozen(monkeypatch, tmp_path: Path) -> None:
    _freeze(monkeypatch, tmp_path)
    assert paths.app_dir() == tmp_path


def test_data_dir_env_overrides_everything(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IME_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("IME_DB", str(tmp_path / "x.db"))
    assert paths.data_dir() == tmp_path / "d"
    assert paths.db_path() == tmp_path / "x.db"


def test_data_dir_prefers_localappdata(monkeypatch, tmp_path: Path) -> None:
    """安装到 Program Files 时 exe 同级不可写，用户数据必须落 %LOCALAPPDATA%。"""
    monkeypatch.delenv("IME_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.data_dir() == tmp_path / "local" / "VoiceIME"
    assert paths.db_path().name == "voiceime.db"


def test_data_dir_falls_back_next_to_exe_when_frozen(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IME_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "_usable", lambda _d: False)
    app = tmp_path / "app"
    _freeze(monkeypatch, app)
    assert paths.data_dir() == tmp_path / "app" / "data"


def test_data_dir_falls_back_inside_spike_when_not_frozen(monkeypatch, tmp_path: Path) -> None:
    """Low 进程写不进 %LOCALAPPDATA% 时，落到仓库内的 `.data`（G25）。"""
    monkeypatch.delenv("IME_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "_usable", lambda _d: False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.data_dir() == paths.app_dir() / ".data"
