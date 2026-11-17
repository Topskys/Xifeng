"""Rust（xifeng_rs）与 Python（vad.py）VAD 实现的逐字节等价性。

与 test_postprocess_parity.py 同一思路：Python 侧是行为基准，Rust 是等价实现，
任何一边漏改都会红。没有扩展时自动 skip。
"""

from __future__ import annotations

import numpy as np
import pytest

from xifeng import vad

rs = pytest.importorskip("xifeng_rs", reason="未构建 Rust 扩展（tools/build_ext.py）")


def _cfg_kwargs(cfg: vad.VadConfig) -> dict:
    return dict(
        sample_rate=cfg.sample_rate,
        frame_ms=cfg.frame_ms,
        start_ratio=cfg.start_ratio,
        end_ratio=cfg.end_ratio,
        start_prob=cfg.start_prob,
        end_prob=cfg.end_prob,
        start_frames=cfg.start_frames,
        end_frames=cfg.end_frames,
        min_speech_ms=cfg.min_speech_ms,
        merge_gap_ms=cfg.merge_gap_ms,
        pad_ms=cfg.pad_ms,
        head_pad_ms=cfg.head_pad_ms,
        tail_pad_ms=cfg.tail_pad_ms,
    )


def _trim_kwargs(cfg: vad.TrimConfig) -> dict:
    return dict(
        frame_ms=cfg.frame_ms,
        peak_ratio=cfg.peak_ratio,
        noise_ratio=cfg.noise_ratio,
        abs_floor=float(cfg.abs_floor),
        lead_ms=cfg.lead_ms,
        tail_ms=cfg.tail_ms,
        min_speech_ms=cfg.min_speech_ms,
        min_saved_ratio=cfg.min_saved_ratio,
    )


def _speech_pcm(silent_ms: int, voiced_ms: int, sr: int = 16000, amp: int = 3000) -> np.ndarray:
    silent = int(sr * silent_ms / 1000)
    voiced = int(sr * voiced_ms / 1000)
    pcm = np.zeros(silent, dtype=np.int16)
    wave = np.where(np.arange(voiced) % 2 == 0, amp, -amp).astype(np.int16)
    pcm = np.concatenate([pcm, wave, np.zeros(silent, dtype=np.int16)])
    return pcm


# ---------------------------------------------------------------- 能量法切分

CASES = [
    ("纯静音", 0, 0),
    ("短促 100ms", 500, 100),
    ("标准 1s", 500, 1000),
    ("长静音 2s+3s", 2000, 3000),
    ("无前导静音", 0, 1000),
    ("无尾部静音", 1000, 0),
]


@pytest.mark.parametrize("name,silent,voiced", CASES)
def test_segment_parity(name: str, silent: int, voiced: int) -> None:
    pcm = _speech_pcm(silent, voiced) if (silent or voiced) else np.zeros(16000, np.int16)
    cfg = vad.VadConfig()
    py = vad.segment(pcm, cfg)
    rust = [tuple(x) for x in rs.vad_segment(pcm, **_cfg_kwargs(cfg))]
    assert rust == py, f"{name}: rust={rust} py={py}"


def test_segment_parity_on_real_fixture() -> None:
    from pathlib import Path

    wav = Path(__file__).resolve().parents[1] / "fixtures" / "quality" / "01.wav"
    if not wav.exists():
        pytest.skip("缺少 fixtures/quality/01.wav")
    from xifeng import audio

    pcm, rate = audio.read_wav(str(wav))
    assert rate == 16000
    cfg = vad.VadConfig()
    assert [tuple(x) for x in rs.vad_segment(pcm, **_cfg_kwargs(cfg))] == vad.segment(pcm, cfg)


# ---------------------------------------------------------------- 概率流切分

PROB_CASES = [
    ("全静音", [0.0] * 30),
    ("单段语音", [0.0] * 3 + [0.9] * 20 + [0.0] * 10),
    ("两帧毛刺", [0.0] * 3 + [0.9, 0.9] + [0.0] * 10),
    ("中间凹陷", [0.0] * 3 + [0.9] * 10 + [0.0] + [0.9] * 10 + [0.0] * 10),
    ("整段都是语音", [0.9] * 30),
    ("双阈值之间", [0.4] * 10 + [0.6] * 10 + [0.4] * 10),
]


@pytest.mark.parametrize("name,probs", PROB_CASES)
def test_probs_parity(name: str, probs: list[float]) -> None:
    cfg = vad.VadConfig()
    window = 512
    total = len(probs) * window
    py = vad.probs_to_segments(probs, cfg, window=window, total_samples=total)
    rust = [tuple(x) for x in rs.vad_segment_from_probs(
        probs, window, total, **_cfg_kwargs(cfg))]
    # probs_to_segments 不含 finalize，Rust 的 segment_from_probs 含；
    # 这里对比"未 finalize"的语义：Rust 传 pad_ms=0 且 min_speech=0 才等价
    cfg2 = vad.VadConfig(pad_ms=0, min_speech_ms=0, merge_gap_ms=0,
                         head_pad_ms=0, tail_pad_ms=0)
    rust_raw = [tuple(x) for x in rs.vad_segment_from_probs(
        probs, window, total, **_cfg_kwargs(cfg2))]
    assert rust_raw == py, f"{name}: rust={rust_raw} py={py}"
    assert isinstance(rust, list)  # 带 finalize 的版本不崩


# ---------------------------------------------------------------- 段重组

def test_rebuild_parity() -> None:
    pcm = np.arange(1000, dtype=np.int16)
    for segs in [[], [(0, 10)], [(10, 20), (50, 55)], [(999, 1000)], [(0, 1000)]]:
        py = vad.rebuild(pcm, segs)
        rust = rs.vad_rebuild(pcm, segs)
        assert np.array_equal(rust, py), f"segs={segs}"
        assert rust.dtype == py.dtype


# ---------------------------------------------------------------- 首尾裁剪

TRIM_CASES = [
    ("长静音+1s语音", 2000, 1000),
    ("几乎全是语音", 200, 5000),
    ("短促 100ms", 500, 100),
    ("无静音", 0, 1000),
]


@pytest.mark.parametrize("name,silent,voiced", TRIM_CASES)
def test_trim_parity(name: str, silent: int, voiced: int) -> None:
    pcm = _speech_pcm(silent, voiced) if (silent or voiced) else np.zeros(16000, np.int16)
    cfg = vad.TrimConfig()
    py_pcm, py_saved = vad.trim(pcm, cfg)
    rust_pcm, rust_saved = rs.vad_trim(pcm, **_trim_kwargs(cfg))
    assert rust_saved == py_saved, f"{name}: saved rust={rust_saved} py={py_saved}"
    assert np.array_equal(rust_pcm, py_pcm), f"{name}: pcm 不一致"
    assert rust_pcm.dtype == py_pcm.dtype


def test_trim_parity_on_real_fixture() -> None:
    from pathlib import Path

    wav = Path(__file__).resolve().parents[1] / "fixtures" / "quality" / "03.wav"
    if not wav.exists():
        pytest.skip("缺少 fixtures/quality/03.wav")
    from xifeng import audio

    pcm, _ = audio.read_wav(str(wav))
    cfg = vad.TrimConfig()
    py_pcm, py_saved = vad.trim(pcm, cfg)
    rust_pcm, rust_saved = rs.vad_trim(pcm, **_trim_kwargs(cfg))
    assert rust_saved == py_saved
    assert np.array_equal(rust_pcm, py_pcm)


# ---------------------------------------------------------------- 随机对照

def test_random_pcm_parity() -> None:
    """确定性随机串：覆盖能量分布、边界、长度不整除帧等情形。"""
    rng = np.random.default_rng(20261109)
    cfg = vad.VadConfig()
    for i in range(20):
        n = int(rng.integers(1600, 40000))
        pcm = rng.integers(-30000, 30000, size=n, dtype=np.int16)
        # 一半用例插入静音块，制造真实的"语音+静音"结构
        if i % 2 == 0:
            pcm[:: max(1, n // 5)] = 0
        py = vad.segment(pcm, cfg)
        rust = [tuple(x) for x in rs.vad_segment(pcm, **_cfg_kwargs(cfg))]
        assert rust == py, f"case {i} n={n}: rust={rust} py={py}"
