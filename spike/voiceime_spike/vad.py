"""VAD：Silero（正式）+ 能量法（替身/回退）。

M1 起主路径是 sherpa-onnx 的 Silero VAD：逐 512 样本窗出语音概率，再用
双阈值滞回（start_prob / end_prob）切分。能量法 `segment()` 保留作模型
缺失时的回退，二者共享同一套"丢弃过短 / 合并相近 / 加 padding"后处理。

参数与 docs/01-overview/latency-budget.md、spec 保持一致：
start_prob=0.5 / end_prob=0.35；G4：丢弃段两侧 MUST 留 60ms 缓冲。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------- 后端
#
# Rust 侧 crates/ime-vad 是同一套算法的实现，本文件的纯 Python 版本保留为
# **行为基准**：两边必须逐字节等价，由 tests/test_vad_parity.py 证明。
# Silero 模型前向仍留在 Python（sherpa-onnx），Rust 只接它吐出的概率流。

_REQUESTED_BACKEND = os.environ.get("VOICEIME_VAD_BACKEND", "auto").strip().lower()

_rs = None
if _REQUESTED_BACKEND != "py":
    try:  # pragma: no cover - 取决于是否构建过 Rust 扩展
        import voiceime_rs as _rs
    except ImportError:  # 没构建过扩展：静默回退，不改变行为
        _rs = None

#: 当前生效的后端："rust" 或 "python"
backend = "rust" if _rs is not None else "python"


def _cfg_kwargs(cfg: "VadConfig") -> dict:
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
    )


def _trim_kwargs(cfg: "TrimConfig") -> dict:
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


@dataclass(frozen=True)
class VadConfig:
    sample_rate: int = 16000
    frame_ms: int = 30
    start_ratio: float = 0.5    # 能量法：相对峰值能量
    end_ratio: float = 0.35
    start_prob: float = 0.5     # Silero：进入语音的概率阈值
    end_prob: float = 0.35      # Silero：退出语音的概率阈值（滞回）
    start_frames: int = 3       # 连续 3 帧确认进入说话
    end_frames: int = 8         # 连续 8 帧（240ms）确认静音
    min_speech_ms: int = 250    # 短于此的段整段丢弃
    merge_gap_ms: int = 200     # 间隔小于此合并
    pad_ms: int = 60            # 两侧保留缓冲（G4）


def _frames(pcm: np.ndarray, frame_len: int, hop: int) -> list[np.ndarray]:
    if len(pcm) < frame_len:
        return [pcm] if len(pcm) else []
    idx = range(0, len(pcm) - frame_len + 1, hop)
    return [pcm[i : i + frame_len] for i in idx]


def _rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def _finalize(
    raw: list[tuple[int, int]],
    pcm_len: int,
    cfg: VadConfig,
) -> list[tuple[int, int]]:
    """切分后处理（能量法与 Silero 共用）：丢弃过短 → 合并相近 → 加 padding。"""
    # 1) 丢弃过短段
    min_len = int(cfg.sample_rate * cfg.min_speech_ms / 1000)
    kept = [(s, e) for s, e in raw if e - s >= min_len]

    # 2) 合并相近段（防止一个字被拆成两段）
    gap = int(cfg.sample_rate * cfg.merge_gap_ms / 1000)
    merged: list[tuple[int, int]] = []
    for s, e in kept:
        if merged and s - merged[-1][1] < gap:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    # 3) 加 padding 并裁剪
    pad = int(cfg.sample_rate * cfg.pad_ms / 1000)
    return [(max(0, s - pad), min(pcm_len, e + pad)) for s, e in merged]


def segment(pcm: np.ndarray, cfg: VadConfig | None = None) -> list[tuple[int, int]]:
    """能量法切分，返回语音段 [(start_sample, end_sample), ...]。

    M1 起是 Silero 不可用时的回退路径。有 Rust 扩展时走 `ime-vad`。
    """
    cfg = cfg or VadConfig()
    pcm = np.asarray(pcm)
    if pcm.dtype != np.int16:
        pcm = pcm.astype(np.int16)

    if _rs is not None:
        return [(int(s), int(e)) for s, e in _rs.vad_segment(pcm, **_cfg_kwargs(cfg))]

    return _segment_py(pcm, cfg)


def _segment_py(pcm: np.ndarray, cfg: VadConfig) -> list[tuple[int, int]]:
    """纯 Python 基准实现（行为基准，勿改语义）。"""

    frame_len = int(cfg.sample_rate * cfg.frame_ms / 1000)
    frames = _frames(pcm, frame_len, frame_len)
    if not frames:
        return []

    energies = np.array([_rms(f) for f in frames])
    peak = float(energies.max()) if len(energies) else 0.0
    if peak <= 1e-6:
        return []

    start_th = peak * cfg.start_ratio
    end_th = peak * cfg.end_ratio

    raw: list[tuple[int, int]] = []
    speaking = False
    seg_start = 0
    above = 0
    below = 0
    last_active = 0

    for i, e in enumerate(energies):
        if not speaking:
            above = above + 1 if e >= start_th else 0
            if above >= cfg.start_frames:
                speaking = True
                seg_start = max(0, i - cfg.start_frames + 1) * frame_len
                below = 0
                last_active = i
        else:
            if e >= end_th:
                last_active = i
                below = 0
            else:
                below += 1
                if below >= cfg.end_frames:
                    speaking = False
                    # 段尾取"最后一个有声帧"，不含尾部 hangover，
                    # 否则 100ms 的咔哒声会被 240ms 尾巴抬到 min_speech 以上
                    seg_end = min(len(pcm), (last_active + 1) * frame_len)
                    raw.append((seg_start, seg_end))
                    above = 0

    if speaking:
        raw.append((seg_start, min(len(pcm), (last_active + 1) * frame_len)))

    return _finalize(raw, len(pcm), cfg)


# ---------------------------------------------------------------- Silero（M1 正式路径）

def probs_to_segments(
    probs: list[float],
    cfg: VadConfig,
    window: int = 512,
    total_samples: int | None = None,
) -> list[tuple[int, int]]:
    """概率流 → 原始语音段（双阈值滞回），纯函数、不依赖模型，单测直接覆盖。

    - 未开口：概率连续 `start_frames` 窗 >= start_prob 才进入（防单窗毛刺）
    - 已开口：概率连续 `end_frames` 窗 < end_prob 才退出（240ms 尾静音）
    - 段尾取最后一个 `p >= end_prob` 的窗，不含 hangover（同能量法的取舍）
    """
    total = total_samples if total_samples is not None else len(probs) * window
    if _rs is not None:
        # 用 pad/min_speech/merge_gap 全 0 的配置取"未 finalize"的原始段，
        # 与纯 Python 分支语义一致（调用方 SileroVad.segment 自己还会 finalize 一次）
        raw_cfg = VadConfig(
            sample_rate=cfg.sample_rate, frame_ms=cfg.frame_ms,
            start_ratio=cfg.start_ratio, end_ratio=cfg.end_ratio,
            start_prob=cfg.start_prob, end_prob=cfg.end_prob,
            start_frames=cfg.start_frames, end_frames=cfg.end_frames,
            min_speech_ms=0, merge_gap_ms=0, pad_ms=0,
        )
        return [(int(s), int(e)) for s, e in
                _rs.vad_segment_from_probs(list(probs), window, total,
                                           **_cfg_kwargs(raw_cfg))]

    raw: list[tuple[int, int]] = []
    speaking = False
    seg_start = 0
    above = 0
    below = 0
    last_active = 0

    for i, p in enumerate(probs):
        if not speaking:
            above = above + 1 if p >= cfg.start_prob else 0
            if above >= cfg.start_frames:
                speaking = True
                seg_start = max(0, i - cfg.start_frames + 1) * window
                below = 0
                last_active = i
        else:
            if p >= cfg.end_prob:
                last_active = i
                below = 0
            else:
                below += 1
                if below >= cfg.end_frames:
                    speaking = False
                    raw.append((seg_start, min(total, (last_active + 1) * window)))
                    above = 0

    if speaking:
        raw.append((seg_start, min(total, (last_active + 1) * window)))
    return raw


class SileroVad:
    """sherpa-onnx Silero VAD：模型常驻（进程内单例，见 `load_vad`）。"""

    def __init__(self, model_path: str | Path, cfg: VadConfig | None = None) -> None:
        import sherpa_onnx

        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(model_path)
        config.sample_rate = 16000
        if not config.validate():
            raise ValueError(f"Silero VAD 模型无效: {model_path}")
        self._model = sherpa_onnx.VadModel.create(config)
        self._window = self._model.window_size()
        self.cfg = cfg or VadConfig()

    def segment(
        self, pcm: np.ndarray, cfg: VadConfig | None = None
    ) -> list[tuple[int, int]]:
        cfg = cfg or self.cfg
        pcm = np.asarray(pcm)
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        if pcm.size < self._window:
            return []

        x = pcm.astype(np.float32) / 32768.0
        w = self._window
        self._model.reset()
        probs = [float(self._model.is_speech(x[i : i + w]))
                 for i in range(0, len(x) - w + 1, w)]
        raw = probs_to_segments(probs, cfg, window=w, total_samples=pcm.size)
        return _finalize(raw, pcm.size, cfg)


_SILERO: SileroVad | None = None


def load_vad(model_path: str | Path | None = None) -> SileroVad | None:
    """加载并缓存 Silero 模型；文件缺失返回 None（调用方回退能量法）。

    幂等：重复调用返回同一实例。模型 MUST 常驻，禁止按次加载。
    """
    global _SILERO
    if _SILERO is not None:
        return _SILERO
    from .paths import vad_model_path

    path = Path(model_path) if model_path else vad_model_path()
    if not path.exists():
        return None
    try:
        _SILERO = SileroVad(path)
        return _SILERO
    except Exception:
        return None


def rebuild(pcm: np.ndarray, segments: list[tuple[int, int]]) -> np.ndarray:
    """把多个语音段拼回单条 PCM。

    只做一次 ASR 是关键：既保持上下文，又不多付推理延迟。
    """
    if _rs is not None:
        return _rs.vad_rebuild(np.asarray(pcm, dtype=np.int16), list(segments))
    if not segments:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate([pcm[s:e] for s, e in segments]).astype(np.int16)


# ---------------------------------------------------------------- 首尾静音裁剪

@dataclass(frozen=True)
class TrimConfig:
    """裁掉首尾静音的参数。

    为什么需要它：PTT 是"按住到松开"，用户很可能说完还按着一两秒（甚至十几秒）。
    静音会被 SenseVoice 解码成幻觉文本（`喂喂喂喂…哎哎呜啊`），还会让 ASR 变慢
    （16.6s 音频 1190ms vs 7.4s 音频 567ms）。裁掉首尾静音同时解决质量和延迟。

    保守原则：**宁可少裁，不可裁掉字**。条件不满足就原样返回。
    """

    frame_ms: int = 30
    peak_ratio: float = 0.05     # 低于峰值 5%（≈ -26dB）视为静音
    noise_ratio: float = 3.0     # 低于噪声底（20 分位）3 倍也视为静音
    abs_floor: int = 200         # int16 绝对值下限，防"全静音也算有声"
    lead_ms: int = 200           # 句首额外保留
    tail_ms: int = 300           # 句尾额外保留（别切掉韵母尾音）
    min_speech_ms: int = 250     # 有声段短于此 → 不裁（可能是咳嗽/咔哒）
    min_saved_ratio: float = 0.10  # 省不到 10% 就不折腾


def trim(
    pcm: np.ndarray,
    cfg: TrimConfig | None = None,
    sample_rate: int = 16000,
) -> tuple[np.ndarray, int]:
    """裁掉首尾静音，返回 `(pcm, 省掉的毫秒数)`。

    只在能明确找到"有声区"时裁；否则原样返回（省 0ms）。
    """
    cfg = cfg or TrimConfig()
    x = np.asarray(pcm, dtype=np.int16)
    if _rs is not None:
        out, saved = _rs.vad_trim(x, sample_rate=sample_rate, **_trim_kwargs(cfg))
        return out, int(saved)

    frame = int(sample_rate * cfg.frame_ms / 1000)
    if x.size < frame * 2 or frame <= 0:
        return x, 0

    n = x.size // frame
    frames = x[: n * frame].reshape(n, frame).astype(np.float64)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    peak = float(rms.max())
    if peak <= 0.0:
        return x, 0

    noise = float(np.percentile(rms, 20))
    threshold = max(peak * cfg.peak_ratio, noise * cfg.noise_ratio, float(cfg.abs_floor))
    active = np.flatnonzero(rms >= threshold)
    if active.size == 0:
        return x, 0

    speech = (int(active[-1]) - int(active[0]) + 1) * frame
    if speech < sample_rate * cfg.min_speech_ms / 1000:
        return x, 0

    lead = int(sample_rate * cfg.lead_ms / 1000)
    tail = int(sample_rate * cfg.tail_ms / 1000)
    start = max(0, int(active[0]) * frame - lead)
    end = min(x.size, (int(active[-1]) + 1) * frame + tail)
    kept = end - start
    if kept >= x.size * (1.0 - cfg.min_saved_ratio):
        return x, 0
    return x[start:end].copy(), int(round((x.size - kept) * 1000 / sample_rate))
