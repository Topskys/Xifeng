"""WAV 读写与合成音频：bench 和手动验证都靠它，不引入 soundfile 依赖。

约定：内部一律 int16 单声道、16kHz。
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """读 PCM WAV，返回 (int16 单声道, sample_rate)。多声道取平均后转 i16。"""
    with wave.open(str(path), "rb") as f:
        channels = f.getnchannels()
        width = f.getsampwidth()
        rate = f.getframerate()
        frames = f.readframes(f.getnframes())

    if width == 2:
        data = np.frombuffer(frames, dtype=np.int16)
    elif width == 1:  # 8-bit 无符号
        data = ((np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128) << 8).astype(np.int16)
    elif width == 4:  # 32-bit int
        data = (np.frombuffer(frames, dtype=np.int32) >> 16).astype(np.int16)
    else:
        raise ValueError(f"不支持的采样宽度: {width} 字节")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return data.astype(np.int16), rate


def write_wav(path: str | Path, pcm: np.ndarray, sample_rate: int = 16000) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    return p


def synth_speech_like(seconds: float = 3.0, sample_rate: int = 16000, seed: int = 7) -> np.ndarray:
    """合成"像语音"的音频：基频 + 谐波 + 包络 + 底噪。

    用途只有一个：在没有麦克风/模型时让 VAD 和管线跑起来。
    它**不能**代表识别质量，数字只能当上界参考。
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate

    f0 = 180 + 20 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / sample_rate
    sig = np.zeros(n)
    for k, amp in enumerate((1.0, 0.5, 0.3, 0.15), start=1):
        sig += amp * np.sin(k * phase)

    # 音节包络：4Hz 起伏，模拟说话节奏
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)
    sig *= env
    sig += 0.01 * rng.standard_normal(n)
    sig = sig / max(1e-9, np.abs(sig).max()) * 0.6
    return (sig * 32767).astype(np.int16)
