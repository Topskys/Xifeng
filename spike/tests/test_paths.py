"""路径解析单测：源码形态与冻结形态（PyInstaller）必须各自指向正确位置。"""

from __future__ import annotations

import sys
from pathlib import Path

from voiceime_spike import paths


def test_env_override_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IME_MODELS_DIR", str(tmp_path))
    assert paths.models_dir() == tmp_path


def test_dev_default_points_into_repo(monkeypatch) -> None:
    monkeypatch.delenv("IME_MODELS_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    expected = Path(paths.__file__).resolve().parents[2] / paths.MODEL_SUBDIR
    assert paths.models_dir() == expected
    assert not paths.is_frozen()


def test_frozen_default_sits_next_to_exe(monkeypatch, tmp_path: Path) -> None:
    """打包后模型 MUST 从 exe 同级目录找，不能用 `__file__`（它在 _MEIPASS 里）。"""
    monkeypatch.delenv("IME_MODELS_DIR", raising=False)
    exe = tmp_path / "VoiceIME.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths.is_frozen()
    assert paths.models_dir() == tmp_path / paths.MODEL_SUBDIR


def test_app_dir_is_exe_dir_when_frozen(monkeypatch, tmp_path: Path) -> None:
    exe = tmp_path / "VoiceIME.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths.app_dir() == tmp_path


# ---------------------------------------------------------------- 数据目录

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
    exe = tmp_path / "app" / "VoiceIME.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths.data_dir() == tmp_path / "app" / "data"


def test_data_dir_falls_back_inside_spike_when_not_frozen(monkeypatch, tmp_path: Path) -> None:
    """Low 进程写不进 %LOCALAPPDATA% 时，落到仓库内的 `.data`（G25）。"""
    monkeypatch.delenv("IME_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "_usable", lambda _d: False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.data_dir() == paths.app_dir() / ".data"
